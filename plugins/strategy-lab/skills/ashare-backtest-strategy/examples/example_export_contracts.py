from __future__ import annotations

from types import SimpleNamespace
import unittest

import pandas as pd

from ashare_backtest_strategy import AShareAccount, AShareClient, AShareExchange, AShareLedger
from ashare_backtest_strategy.ledger import _records_to_frame


class ExportContractTests(unittest.TestCase):
    def test_alias_fallback_is_lazy_for_object_records(self):
        class Record:
            primary = None

            @property
            def secondary(self):
                raise AssertionError("Explicit null must not evaluate the fallback")

        frame = _records_to_frame(
            (record for record in [Record(), SimpleNamespace(secondary=7), SimpleNamespace()]),
            schema=[("value", ("primary", "secondary"))],
        )
        self.assertTrue(pd.isna(frame.at[0, "value"]))
        self.assertEqual(frame.at[1, "value"], 7)
        self.assertTrue(pd.isna(frame.at[2, "value"]))

    def make_client(self):
        account = AShareAccount(account_id="account", init_cash=100_000)
        ledger = AShareLedger()
        exchange = AShareExchange()
        exchange.register(account, ledger)
        client = AShareClient()
        client.connect(exchange, account_id=account.account_id)
        return client, ledger

    def test_order_timestamp_precedence_and_explicit_null(self):
        client, _ = self.make_client()
        timestamp = pd.Timestamp("2025-01-02 10:00", tz="Asia/Shanghai")
        for kwargs, expected in (
            ({"created_at": timestamp, "trade_time": "invalid"}, timestamp),
            ({"trade_time": timestamp}, timestamp),
            ({"created_at": "2025-01-02"}, pd.Timestamp("2025-01-02")),
            ({"created_at": None, "trade_time": timestamp}, None),
            ({"trade_time": None}, None),
        ):
            with self.subTest(kwargs=kwargs):
                order = client.buy("A", 100, **kwargs)
                if expected is None:
                    self.assertTrue(pd.isna(order.created_at))
                else:
                    self.assertEqual(order.created_at, expected)
        before = pd.Timestamp.now()
        order = client.buy("A", 100)
        self.assertLessEqual(before, order.created_at)
        self.assertLessEqual(order.created_at, pd.Timestamp.now())

    def test_aliases_nested_paths_and_explicit_null(self):
        schema = [("value", ("primary", "secondary")), ("nested", "meta.value"), "absent"]
        records = [
            SimpleNamespace(primary=None, secondary=10, meta={"value": 3}),
            SimpleNamespace(secondary=20, meta={"value": 4}),
        ]
        objects = _records_to_frame(records, schema=schema)
        mixed = _records_to_frame([vars(records[0]), records[1]], schema=schema)
        pd.testing.assert_frame_equal(objects, mixed)
        self.assertTrue(pd.isna(objects.at[0, "value"]))
        self.assertEqual(objects.at[1, "value"], 20)
        self.assertEqual(objects["nested"].tolist(), [3, 4])
        self.assertTrue(objects["absent"].isna().all())
        self.assertEqual(_records_to_frame([], schema=schema).columns.tolist(), ["value", "nested", "absent"])

    def test_exports_filter_dates_and_isolate_cached_values(self):
        client, ledger = self.make_client()
        for code, created_at in (("A", None), ("B", pd.Timestamp("2025-01-02"))):
            order = client.buy(code, 100, created_at=created_at, meta={"tag": code})
            client.cancel(order)
        exported = ledger.orders_frame()
        self.assertEqual(len(exported), 2)
        exported.loc[:, "status"] = "CHANGED"
        self.assertEqual(ledger.orders_frame()["status"].tolist(), ["CANCELED", "CANCELED"])
        self.assertEqual(len(ledger.orders_frame(status="CANCELED")), 2)
        self.assertEqual(ledger.orders_frame(start="2025-01-02")["code"].tolist(), ["B"])
        self.assertEqual(ledger.orders_frame(end="2025-01-01").shape[0], 0)
        extra = ledger.orders_frame(extra_schema=[("tag", "meta.tag")])
        self.assertEqual(extra["tag"].tolist(), ["A", "B"])
        self.assertNotIn("tag", ledger.orders_frame())

    def test_duplicate_quote_rows_custom_slippage_and_partial_fills(self):
        observed = []

        def slippage(order, row):
            observed.append(row["custom"])
            return row["base_px"]

        account = AShareAccount(account_id="partial", init_cash=100_000)
        ledger = AShareLedger()
        exchange = AShareExchange(slippage=slippage, commission=lambda *args: 1.0)
        exchange.register(account, ledger)
        client = AShareClient()
        client.connect(exchange, account_id=account.account_id)
        order = client.buy("A", 200, created_at="2025-01-01")
        missing = client.buy("MISSING", 100, created_at="2025-01-01")
        bar = pd.DataFrame(
            {"open": [9.0, 10.0, 20.0], "volume": [100, 100, 100], "custom": [1, 2, 3]},
            index=pd.Index(["A", "A", "B"], dtype="string"),
        )
        terminal, executions = exchange.advance(bar, timestamp="2025-01-02")
        self.assertEqual(terminal, [])
        self.assertEqual(order.status, "PARTIAL")
        self.assertEqual(executions[0].price, 10.0)
        terminal, _ = exchange.advance(bar, timestamp="2025-01-03")
        self.assertEqual(terminal, [order])
        self.assertEqual(observed, [2, 2])
        self.assertEqual(order.comm, 2.0)
        self.assertEqual(exchange.order_queue, [missing])
        frame = ledger.executions_frame()
        self.assertEqual(len(frame), 2)
        self.assertEqual(len(ledger.executions_frame(start="2025-01-03", code="A")), 1)
        frame.loc[:, "price"] = 1.0
        self.assertEqual(ledger.executions_frame()["price"].tolist(), [10.0, 10.0])
        self.assertEqual(ledger.snapshot()["cash"], account.cash)


if __name__ == "__main__":
    unittest.main()
