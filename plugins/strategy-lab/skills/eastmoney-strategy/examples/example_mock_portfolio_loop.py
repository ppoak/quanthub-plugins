"""Show a strategy-to-portfolio flow using only fixed EastMoney responses."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from eastmoney_strategy import EMAccount, EMClient, EMExchange, EMLedger


def run_example() -> dict[str, int | float | str]:
    account = EMAccount(account_id="demo-portfolio", user_id="mock-user", ut_token="mock-token")
    ledger = EMLedger(user_id="mock-user", ut_token="mock-token")
    exchange = EMExchange(user_id="mock-user", ut_token="mock-token")
    exchange.register(account, ledger=ledger)
    client = EMClient(
        client_id="moving-average-demo",
        user_id="mock-user",
        ut_token="mock-token",
        ct_token="mock-ct-token",
        live=False,
    )
    client.connect(exchange, account_id=account.account_id)

    with (
        patch.object(
            account,
            "_get_account_info",
            return_value={"data": [{"kyye": "9000", "zzc": "10000", "mktVal": "1000"}]},
        ),
        patch.object(
            account,
            "_get_holdings",
            return_value={"data": [{"stockCode": "000001", "zqsl": "100", "kysl": "100", "cbj": "9", "mktVal": "1000"}]},
        ),
    ):
        account_snapshot = exchange.account(account.account_id)

    # A normal signal handler computes this target from the refreshed snapshot.
    preview = client.order_target_value("000001", target_value=2000, price=10, sync=False)
    assert preview is not None and preview.status == "CREATED"
    assert not ledger.orders and not ledger.executions

    confirmed_order_response = {
        "data": [{
            "wth": "remote-order-1", "mmbz": "买入", "__code": "000001",
            "wtsl": "100", "wtjg": "10", "ztflag": "1", "cjsl": "100",
            "wtrq": "20260105", "wtsj": "10:00:00",
        }]
    }
    confirmed_execution_response = {
        "data": [{
            "cjlsh": "remote-fill-1", "wth": "remote-order-1", "mmbz": "买入",
            "__code": "000001", "cjsl": "100", "cjjg": "10",
            "cjrq": "20260105", "cjsj": "10:01:00",
        }]
    }
    with patch.object(
        exchange, "request_web_oper",
        side_effect=[confirmed_order_response, {"data": []}],
    ) as order_request, patch.object(
        exchange, "request_trade",
        side_effect=[confirmed_execution_response, {"data": []}],
    ) as execution_request:
        confirmed_orders = exchange.fetch_orders(account_id=account.account_id, rec_cnt=1)
        confirmed_fills = exchange.fetch_executions(account_id=account.account_id, rec_cnt=1)
    assert [call.args[0]["recIdx"] for call in order_request.call_args_list] == ["0", "1"]
    assert [call.args[0]["recIdx"] for call in execution_request.call_args_list] == ["0", "1"]
    assert all(call.args[0]["type"] == "deals" for call in execution_request.call_args_list)

    for order in confirmed_orders:
        exchange.record_order(order)
    for fill in confirmed_fills:
        ledger.record_execution(fill)

    assert confirmed_orders[0].status == "FILLED"
    assert len(ledger.orders) == len(ledger.executions) == 1
    assert account_snapshot.net_asset_value == 10_000
    return {
        "portfolio": account.account_id,
        "preview_status": preview.status,
        "confirmed_orders": len(ledger.orders),
        "confirmed_executions": len(ledger.executions),
    }


class MockPortfolioLoopExample(unittest.TestCase):
    def test_dry_run_waits_for_remote_confirmation(self) -> None:
        result = run_example()
        self.assertEqual(result["preview_status"], "CREATED")
        self.assertEqual(result["confirmed_orders"], 1)
        self.assertEqual(result["confirmed_executions"], 1)


if __name__ == "__main__":
    print(run_example())
