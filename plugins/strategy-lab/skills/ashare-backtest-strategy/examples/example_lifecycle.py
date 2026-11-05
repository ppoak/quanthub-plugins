from __future__ import annotations

import unittest

import pandas as pd

from ashare_backtest_strategy import (
    AShareAccount,
    AShareClient,
    AShareExecution,
    AShareExchange,
    AShareLedger,
    FixedRateCommission,
)
from base_strategy import evaluate_executions


class AShareLifecycleTests(unittest.TestCase):
    def test_bulk_price_update_matches_single_updates_for_open_positions(self) -> None:
        account = AShareAccount(account_id="bulk-price", init_cash=100_000)
        for code in ("000001.SZ", "600000.SH"):
            account.apply_execution(
                AShareExecution(
                    execution_id=f"buy-{code}",
                    account_id=account.account_id,
                    datetime=pd.Timestamp("2026-01-05"),
                    side=AShareExecution.BUY,
                    code=code,
                    quantity=100,
                    price=10.0,
                )
            )
        account.position("000002.SZ")

        account.update_prices(
            pd.Series({"000001.SZ": 12.0, "600000.SH": 9.0, "000002.SZ": 50.0})
        )

        self.assertEqual(account.market_value, 2100.0)
        self.assertEqual(account.position("000001.SZ").price, 12.0)
        self.assertEqual(account.position("600000.SH").price, 9.0)
        self.assertEqual(account.position("000002.SZ").price, 0.0)
        self.assertNotIn("000003.SZ", account.positions)

    def test_non_finite_price_updates_are_ignored(self) -> None:
        code = "000001.SZ"
        account = AShareAccount(account_id="invalid-price", init_cash=100_000)
        account.apply_execution(
            AShareExecution(
                execution_id="buy-invalid-price",
                account_id=account.account_id,
                datetime=pd.Timestamp("2026-01-05"),
                side=AShareExecution.BUY,
                code=code,
                quantity=100,
                price=10.0,
            )
        )

        account.update_price(code, float("nan"))
        account.update_prices(pd.Series({code: float("inf")}))

        self.assertEqual(account.position(code).price, 10.0)
        self.assertEqual(account.market_value, 1000.0)
        self.assertEqual(account.net_asset_value, 100_000.0)

    def test_next_bar_fill_t1_ledger_replay_and_nav(self) -> None:
        code = "000001.SZ"
        day_one = pd.Timestamp("2026-01-05 15:00")
        day_two = pd.Timestamp("2026-01-06 10:00")
        day_three = pd.Timestamp("2026-01-07 10:00")
        bar = pd.DataFrame(
            [{"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 10000}],
            index=[code],
        )
        account = AShareAccount(account_id="test", init_cash=100_000)
        ledger = AShareLedger()
        exchange = AShareExchange(commission=FixedRateCommission(rate=0, min_commission=0))
        exchange.register(account, ledger=ledger)
        client = AShareClient(client_id="test-client")
        client.connect(exchange, account_id="test")

        exchange.advance(bar, timestamp=day_one)
        order = client.buy(code, 100, created_at=day_one)
        self.assertEqual(order.status, order.ACCEPTED)

        terminal, executions = exchange.advance(bar, timestamp=day_two)
        self.assertEqual(terminal, [order])
        self.assertEqual(len(executions), 1)
        execution = executions[0]
        self.assertEqual(execution.order_id, order.order_id)
        self.assertEqual(order.executions[0].execution_id, execution.execution_id)
        self.assertEqual(len(ledger.orders), 1)
        self.assertEqual(len(ledger.executions), 1)
        ledger.record_order(order)
        ledger.record_execution(execution)
        self.assertEqual(len(ledger.orders), 1)
        self.assertEqual(len(ledger.executions), 1)
        self.assertEqual(account.position(code).quantity, 100)
        self.assertEqual(account.available_quantity(code, day_two), 0)
        self.assertEqual(account.available_quantity(code, day_three), 100)

        replay = ledger.snapshot(timestamp=day_two, prices={code: 10.0})
        self.assertAlmostEqual(replay["net_asset_value"], account.net_asset_value)
        prices = pd.DataFrame({code: [10.0, 10.0]}, index=[day_one, day_two])
        metrics = evaluate_executions(ledger.executions, prices=prices, initial_cash=100_000)
        self.assertAlmostEqual(float(metrics["final_nav"]), account.net_asset_value)


if __name__ == "__main__":
    unittest.main()
