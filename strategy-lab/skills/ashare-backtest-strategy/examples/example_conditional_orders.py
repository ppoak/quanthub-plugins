"""Exercise conditional orders and validity windows through the strategy loop."""

from __future__ import annotations

import unittest

import pandas as pd
from ashare_backtest_strategy import (
    AShareAccount,
    AShareClient,
    AShareExchange,
    AShareExecution,
    AShareLedger,
    AShareOrder,
)


def make_market():
    account = AShareAccount(account_id="conditional-orders", init_cash=100_000)
    ledger = AShareLedger()
    exchange = AShareExchange(
        slippage=lambda order, row: row["base_px"],
        commission=lambda order, price, quantity: 0.0,
    )
    exchange.register(account, ledger)
    client = AShareClient(client_id="conditional-orders")
    client.connect(exchange, account_id=account.account_id)

    # Establish a settled position for sell-order examples.
    execution = AShareExecution(
        execution_id="initial-position",
        account_id=account.account_id,
        datetime=pd.Timestamp("2026-09-25"),
        side=AShareExecution.BUY,
        code="AAA",
        quantity=1000,
        price=10.0,
    )
    account.apply_execution(execution)
    ledger.record_execution(execution)
    return account, client, exchange, ledger


def make_bar(*, low=9.0, high=11.0, opening=10.0, volume=10_000):
    return pd.DataFrame(
        {
            "open": [opening],
            "high": [high],
            "low": [low],
            "close": [opening],
            "volume": [volume],
        },
        index=["AAA"],
    )


class ConditionalOrderExamples(unittest.TestCase):
    def test_stop_and_target_use_prices_available_after_trigger(self):
        for execution_type in (AShareOrder.STOP, AShareOrder.TARGET):
            for side, trigger, opening, expected in (
                ("BUY", 11.0, 10.0, 11.0),
                ("BUY", 11.0, 12.0, 12.0),
                ("SELL", 9.0, 10.0, 9.0),
                ("SELL", 9.0, 8.0, 8.0),
            ):
                with self.subTest(
                    execution_type=execution_type, side=side, opening=opening
                ):
                    _, client, exchange, _ = make_market()
                    submit = client.buy if side == "BUY" else client.sell
                    submit(
                        "AAA",
                        100,
                        exectype=execution_type,
                        trigger=trigger,
                        created_at="2026-09-25",
                    )
                    _, fills = exchange.advance(
                        make_bar(low=7.0, high=13.0, opening=opening),
                        timestamp="2026-09-28",
                    )
                    self.assertEqual(len(fills), 1)
                    self.assertEqual(fills[0].price, expected)

    def test_explicit_invalid_volume_does_not_fill(self):
        for volume in (0.0, -1.0, float("nan"), float("inf"), None):
            with self.subTest(volume=volume):
                account, client, exchange, ledger = make_market()
                before_cash = account.cash
                before_count = len(ledger.executions)
                order = client.buy("AAA", 100, created_at="2026-09-25")
                _, fills = exchange.advance(
                    make_bar(volume=volume), timestamp="2026-09-28"
                )
                self.assertEqual(fills, [])
                self.assertEqual(account.cash, before_cash)
                self.assertEqual(len(ledger.executions), before_count)
                self.assertEqual(exchange.order_queue, [order])

    def test_omitted_volume_supports_price_only_examples(self):
        _, client, exchange, _ = make_market()
        client.buy("AAA", 100, created_at="2026-09-25")
        _, fills = exchange.advance(
            make_bar().drop(columns="volume"), timestamp="2026-09-28"
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].quantity, 100)

    def test_stoplimit_waits_for_trigger_then_retains_limit_state(self):
        for side, trigger, trigger_bar in (
            ("BUY", 12.0, make_bar(low=11.0, high=13.0, opening=12.0)),
            ("SELL", 8.0, make_bar(low=7.0, high=9.0, opening=8.0)),
        ):
            with self.subTest(side=side):
                _, client, exchange, ledger = make_market()
                submit = client.buy if side == "BUY" else client.sell
                order = submit(
                    "AAA",
                    100,
                    exectype=AShareOrder.STOPLIMIT,
                    trigger=trigger,
                    limit=10.0,
                    created_at="2026-09-25",
                )
                _, fills = exchange.advance(make_bar(), timestamp="2026-09-28")
                self.assertEqual(fills, [])
                self.assertEqual(order.trigger_price, trigger)

                _, fills = exchange.advance(trigger_bar, timestamp="2026-09-29")
                self.assertEqual(fills, [])
                self.assertIsNone(order.trigger_price)
                self.assertEqual(exchange.order_queue, [order])

                terminal, fills = exchange.advance(make_bar(), timestamp="2026-09-30")
                self.assertEqual(terminal, [order])
                self.assertEqual(order.status, AShareOrder.FILLED)
                self.assertEqual(fills[0].order_id, order.order_id)
                self.assertEqual(ledger.orders, [order])
                self.assertEqual(exchange.order_queue, [])

    def test_stoplimit_can_fill_on_the_trigger_bar(self):
        for side, trigger in (("BUY", 12.0), ("SELL", 8.0)):
            with self.subTest(side=side):
                _, client, exchange, _ = make_market()
                submit = client.buy if side == "BUY" else client.sell
                order = submit(
                    "AAA",
                    100,
                    exectype=AShareOrder.STOPLIMIT,
                    trigger=trigger,
                    limit=10.0,
                )
                _, fills = exchange.advance(
                    make_bar(low=7.0, high=13.0),
                    timestamp="2026-09-28",
                )
                self.assertEqual(order.status, AShareOrder.FILLED)
                self.assertEqual(len(fills), 1)
                self.assertEqual(fills[0].price, 10.0)

    def test_triggered_stoplimit_retains_state_after_partial_fill(self):
        _, client, exchange, ledger = make_market()
        order = client.buy(
            "AAA",
            200,
            exectype=AShareOrder.STOPLIMIT,
            trigger=12.0,
            limit=10.0,
        )
        exchange.advance(
            make_bar(low=9.0, high=13.0, volume=100),
            timestamp="2026-09-28",
        )
        self.assertEqual(order.status, AShareOrder.PARTIAL)
        self.assertIsNone(order.trigger_price)
        terminal, fills = exchange.advance(make_bar(), timestamp="2026-09-29")
        self.assertEqual(terminal, [order])
        self.assertEqual(fills[0].quantity, 100)
        self.assertEqual(order.filled, 200)
        self.assertEqual(len(ledger.executions), 3)

    def test_targetlimit_starts_limit_matching_on_the_next_bar(self):
        for side, trigger in (("BUY", 12.0), ("SELL", 8.0)):
            with self.subTest(side=side):
                _, client, exchange, _ = make_market()
                submit = client.buy if side == "BUY" else client.sell
                order = submit(
                    "AAA",
                    100,
                    exectype=AShareOrder.TARGETLIMIT,
                    trigger=trigger,
                    limit=10.0,
                )
                _, fills = exchange.advance(
                    make_bar(low=7.0, high=13.0),
                    timestamp="2026-09-28",
                )
                self.assertEqual(fills, [])
                self.assertIsNone(order.trigger_price)
                _, fills = exchange.advance(make_bar(), timestamp="2026-09-29")
                self.assertEqual(len(fills), 1)
                self.assertEqual(order.status, AShareOrder.FILLED)

    def test_expiration_precedes_matching_and_preserves_account_state(self):
        for quote_present in (True, False):
            with self.subTest(quote_present=quote_present):
                account, client, exchange, ledger = make_market()
                order = client.buy(
                    "AAA",
                    100,
                    valid_until=pd.Timestamp("2026-09-28"),
                )
                initial_cash = account.cash
                initial_quantity = account.position("AAA").quantity
                bar = make_bar() if quote_present else make_bar().iloc[:0]
                terminal, fills = exchange.advance(bar, timestamp="2026-09-29")
                self.assertEqual(terminal, [order])
                self.assertEqual(fills, [])
                self.assertEqual(order.status, AShareOrder.EXPIRED)
                self.assertEqual(account.cash, initial_cash)
                self.assertEqual(account.position("AAA").quantity, initial_quantity)
                self.assertEqual(ledger.orders, [order])
                self.assertEqual(exchange.order_queue, [])

    def test_invalid_quotes_preserve_cash_and_positions(self):
        for field in ("open", "high", "low"):
            for value in (float("nan"), float("inf"), -1.0, 0.0):
                with self.subTest(field=field, value=value):
                    account, client, exchange, ledger = make_market()
                    order = client.buy("AAA", 100)
                    cash = account.cash
                    quantity = account.position("AAA").quantity
                    bar = make_bar()
                    bar.loc["AAA", field] = value
                    _, fills = exchange.advance(bar, timestamp="2026-09-28")
                    self.assertEqual(fills, [])
                    self.assertEqual(account.cash, cash)
                    self.assertEqual(account.position("AAA").quantity, quantity)
                    self.assertEqual(exchange.order_queue, [order])
                    self.assertEqual(len(ledger.executions), 1)

    def test_limit_prices_bound_slippage_for_both_sides(self):
        for execution_type in (
            AShareOrder.LIMIT,
            AShareOrder.STOPLIMIT,
            AShareOrder.TARGETLIMIT,
        ):
            for side in ("BUY", "SELL"):
                with self.subTest(execution_type=execution_type, side=side):
                    _, client, exchange, _ = make_market()
                    exchange.slippage_fn = lambda order, row: (
                        row["base_px"] + 0.1
                        if order.side == "BUY"
                        else row["base_px"] - 0.1
                    )
                    submit = client.buy if side == "BUY" else client.sell
                    order = submit(
                        "AAA",
                        100,
                        exectype=execution_type,
                        limit=10.0,
                        trigger=10.0,
                    )
                    _, fills = exchange.advance(make_bar(), timestamp="2026-09-28")
                    if execution_type == AShareOrder.TARGETLIMIT:
                        self.assertEqual(fills, [])
                        _, fills = exchange.advance(make_bar(), timestamp="2026-09-29")
                    self.assertEqual(order.status, AShareOrder.FILLED)
                    self.assertEqual(len(fills), 1)
                    self.assertEqual(fills[0].price, 10.0)

    def test_invalid_commission_cannot_mutate_the_account(self):
        account, client, exchange, ledger = make_market()
        exchange.commission_fn = lambda order, price, quantity: float("nan")
        cash = account.cash
        quantity = account.position("AAA").quantity
        order = client.buy("AAA", 100)
        _, fills = exchange.advance(make_bar(), timestamp="2026-09-28")
        self.assertEqual(order.status, AShareOrder.REJECTED)
        self.assertEqual(fills, [])
        self.assertEqual(account.cash, cash)
        self.assertEqual(account.position("AAA").quantity, quantity)
        self.assertEqual(len(ledger.executions), 1)

    def test_validity_deadline_is_inclusive(self):
        _, client, exchange, _ = make_market()
        order = client.buy("AAA", 100, valid_until=pd.Timestamp("2026-09-28"))
        _, fills = exchange.advance(make_bar(), timestamp="2026-09-28")
        self.assertEqual(order.status, AShareOrder.FILLED)
        self.assertEqual(len(fills), 1)

    def test_partial_order_expires_without_filling_the_remainder(self):
        account, client, exchange, ledger = make_market()
        order = client.buy("AAA", 200, valid_until=pd.Timestamp("2026-09-28"))
        exchange.advance(make_bar(volume=100), timestamp="2026-09-28")
        self.assertEqual(order.status, AShareOrder.PARTIAL)
        cash_after_fill = account.cash
        terminal, fills = exchange.advance(make_bar(), timestamp="2026-09-29")
        self.assertEqual(terminal, [order])
        self.assertEqual(fills, [])
        self.assertEqual(order.status, AShareOrder.EXPIRED)
        self.assertEqual(order.filled, 100)
        self.assertEqual(account.cash, cash_after_fill)
        self.assertEqual(len(ledger.executions), 2)

    def test_volume_is_shared_across_accounts_and_resets_each_bar(self):
        _, first, exchange, _ = make_market()
        second_account = AShareAccount(account_id="second", init_cash=100_000)
        exchange.register(second_account)
        second = AShareClient(client_id="second")
        second.connect(exchange, account_id=second_account.account_id)
        earlier = first.buy("AAA", 100, created_at="2026-09-25")
        later = second.buy("AAA", 100, created_at="2026-09-25")
        _, fills = exchange.advance(make_bar(volume=150), timestamp="2026-09-28")
        self.assertEqual([fill.quantity for fill in fills], [100, 50])
        self.assertEqual(earlier.status, AShareOrder.FILLED)
        self.assertEqual(later.status, AShareOrder.PARTIAL)
        self.assertEqual(exchange.order_queue, [later])
        _, fills = exchange.advance(make_bar(volume=100), timestamp="2026-09-29")
        self.assertEqual([fill.quantity for fill in fills], [50])
        self.assertEqual(later.status, AShareOrder.FILLED)

    def test_rejected_order_leaves_volume_for_later_orders(self):
        _, client, exchange, _ = make_market()
        poor = AShareAccount(account_id="poor", init_cash=1)
        exchange.register(poor)
        poor_client = AShareClient(client_id="poor")
        poor_client.connect(exchange, account_id=poor.account_id)
        rejected = poor_client.buy("AAA", 100, created_at="2026-09-25")
        accepted = client.buy("AAA", 100, created_at="2026-09-25")
        _, fills = exchange.advance(make_bar(volume=100), timestamp="2026-09-28")
        self.assertEqual(rejected.status, AShareOrder.MARGIN)
        self.assertEqual(accepted.status, AShareOrder.FILLED)
        self.assertEqual([fill.quantity for fill in fills], [100])

    def test_exhausted_volume_preserves_pending_orders(self):
        _, client, exchange, _ = make_market()
        earlier = client.buy("AAA", 100, created_at="2026-09-25")
        later = client.buy("AAA", 100, created_at="2026-09-25")
        _, fills = exchange.advance(make_bar(volume=100), timestamp="2026-09-28")
        self.assertEqual([fill.quantity for fill in fills], [100])
        self.assertEqual(earlier.status, AShareOrder.FILLED)
        self.assertEqual(later.status, AShareOrder.ACCEPTED)
        self.assertEqual(exchange.order_queue, [later])


if __name__ == "__main__":
    unittest.main()
