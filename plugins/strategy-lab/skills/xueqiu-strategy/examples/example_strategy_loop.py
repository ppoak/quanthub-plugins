"""Run a local signal loop; publish remote records only when explicitly enabled."""

from __future__ import annotations

import argparse
import json
import os

import pandas as pd

from base_strategy import evaluate_executions
from xueqiu_strategy import XueqiuAccount, XueqiuClient, XueqiuExchange, XueqiuLedger


def run_example(*, token: str = "offline", gid: str = "offline", sync_transactions: bool = False) -> dict:
    client = XueqiuClient(token=token)
    account = XueqiuAccount(account_id=gid, init_cash=10_000)
    ledger = XueqiuLedger(client=client, gid=gid)
    exchange = XueqiuExchange(client, gid=gid, sync_transactions=sync_transactions)
    exchange.register(account, ledger=ledger)
    client.connect(exchange, account_id=account.account_id)

    initial_cash = account.cash
    initial_positions = {code: position.quantity for code, position in account.positions.items()}

    code = "000001.SZ"
    dates = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"])
    prices = pd.DataFrame({code: [10.0, 11.0, 12.0]}, index=dates)
    for index, timestamp in enumerate(dates):
        price = prices.loc[timestamp, code]
        bar = pd.DataFrame(
            [{"open": price, "high": price, "low": price, "close": price, "volume": 10000}],
            index=[code],
        )
        exchange.advance(bar, timestamp=timestamp)
        if index == 0:
            client.buy(code, 100, created_at=timestamp)
        elif index == 1:
            client.sell(code, 40, created_at=timestamp)

    metrics = evaluate_executions(
        ledger.executions, prices=prices, initial_cash=initial_cash, initial_positions=initial_positions,
    )
    return {
        "account_id": account.account_id,
        "orders": len(ledger.orders),
        "executions": len(ledger.executions),
        "cash": account.cash,
        "net_asset_value": account.net_asset_value,
        "evaluated_nav": float(metrics["final_nav"]),
        "sync_transactions": sync_transactions,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sync-transactions", action="store_true", help="Write sample trades to Xueqiu")
    args = parser.parse_args()
    if args.sync_transactions:
        token, gid = os.getenv("XUEQIU_TOKEN", ""), os.getenv("XUEQIU_GID", "")
        if not token or not gid:
            parser.error("XUEQIU_TOKEN and XUEQIU_GID are required for remote writes")
    else:
        token = gid = "offline"
    print(json.dumps(run_example(token=token, gid=gid, sync_transactions=args.sync_transactions)))


if __name__ == "__main__":
    main()
