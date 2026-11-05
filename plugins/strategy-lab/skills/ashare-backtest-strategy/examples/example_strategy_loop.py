"""Run a small long-only moving-average strategy through the A-share backend."""

from __future__ import annotations

import unittest

import pandas as pd

from ashare_backtest_strategy import (
    AShareAccount,
    AShareClient,
    AShareExchange,
    AShareLedger,
    FixedRateCommission,
)
from base_strategy import evaluate_executions


def run_example() -> dict[str, float | int]:
    closes = [10.0, 10.4, 11.0, 12.0, 11.5, 9.8, 8.8, 9.5, 11.0]
    dates = pd.bdate_range("2026-01-05", periods=len(closes))
    bars = [
        (
            date,
            pd.DataFrame(
                {
                    "open": [close],
                    "high": [close * 1.01],
                    "low": [close * 0.99],
                    "close": [close],
                    "volume": [10_000_000.0],
                },
                index=pd.Index(["000001.SZ"], dtype="string", name="code"),
            ),
        )
        for date, close in zip(dates, closes)
    ]

    account = AShareAccount(account_id="moving-average-demo", init_cash=1_000_000)
    ledger = AShareLedger()
    exchange = AShareExchange(
        commission=FixedRateCommission(rate=0.0003, min_commission=5.0, stamp_duty=0.001)
    )
    exchange.register(account, ledger=ledger)
    client = AShareClient(client_id="moving-average-demo")
    client.connect(exchange, account_id=account.account_id)

    close_history: list[float] = []
    target_weight = 0.0
    for timestamp, bar in bars:
        # The exchange fills yesterday's orders at today's bar before signals run.
        exchange.advance(bar, timestamp=timestamp)
        close = float(bar.at["000001.SZ", "close"])
        account.update_prices(bar["close"])
        close_history.append(close)

        if len(close_history) < 3:
            continue
        next_target = 0.5 if close > sum(close_history[-3:]) / 3 else 0.0
        if next_target != target_weight:
            client.order_target_percent(
                "000001.SZ",
                percent=next_target,
                price=close,
                created_at=timestamp,
            )
            target_weight = next_target

    executions = ledger.executions
    assert executions
    assert all(execution.order_id for execution in executions)
    prices = pd.DataFrame({"000001.SZ": closes}, index=dates)
    metrics = evaluate_executions(executions, prices=prices, initial_cash=account.init_cash)
    assert abs(float(metrics["final_nav"]) - account.net_asset_value) < 1e-6
    assert len(ledger.orders) == len(executions)
    return {
        "orders": len(ledger.orders),
        "executions": len(executions),
        "final_cash": account.cash,
        "final_nav": account.net_asset_value,
    }


class StrategyLoopExample(unittest.TestCase):
    def test_next_bar_strategy_loop_matches_execution_replay(self) -> None:
        result = run_example()
        self.assertGreater(result["executions"], 0)
        self.assertGreater(result["final_nav"], 0)


if __name__ == "__main__":
    print(run_example())
