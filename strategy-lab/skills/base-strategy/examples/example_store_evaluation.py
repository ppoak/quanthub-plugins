"""Exercise the core order, execution, store, and evaluation flow."""

import unittest

import pandas as pd

from base_strategy import Execution, Order, Store, evaluate_executions


def run_example() -> dict[str, float | int]:
    store = Store()
    orders = [
        Order(
            order_id="order-buy",
            created_at="2026-01-01",
            code="AAA",
            side="BUY",
            quantity=100,
            status="FILLED",
            filled_quantity=100,
            avg_price=10,
        ),
        Order(
            order_id="order-sell",
            created_at="2026-01-02",
            code="AAA",
            side="SELL",
            quantity=50,
            status="FILLED",
            filled_quantity=50,
            avg_price=12,
        ),
    ]
    executions = [
        Execution(
            execution_id="buy-1",
            order_id="order-buy",
            timestamp="2026-01-01",
            code="AAA",
            side="BUY",
            quantity=100,
            price=10,
            comm=1,
        ),
        Execution(
            execution_id="sell-1",
            order_id="order-sell",
            timestamp="2026-01-02",
            code="AAA",
            side="SELL",
            quantity=50,
            price=12,
            comm=1,
        ),
    ]
    for order in orders:
        store.record_order(order)
    for execution in executions:
        store.record_execution(execution)

    prices = pd.DataFrame(
        {"AAA": [10.0, 12.0]},
        index=pd.to_datetime(["2026-01-01", "2026-01-02"]),
    )
    metrics = evaluate_executions(store.executions, prices=prices, initial_cash=2_000)
    assert len(store.orders) == 2
    assert len(store.executions) == 2
    assert float(metrics["final_nav"]) == 2_198.0
    return {
        "orders": len(store.orders),
        "executions": len(store.executions),
        "final_nav": float(metrics["final_nav"]),
    }


class CoreStrategyExample(unittest.TestCase):
    def test_order_execution_and_evaluation_flow(self) -> None:
        self.assertEqual(
            run_example(),
            {"orders": 2, "executions": 2, "final_nav": 2_198.0},
        )

    def test_store_deduplicates_confirmed_execution_ids(self) -> None:
        execution = Execution(execution_id="fill-1", order_id="order-1")
        store = Store()

        store.record_execution(execution)
        store.record_execution(Execution(execution_id="fill-1", order_id="order-1"))

        self.assertEqual(store.executions, [execution])


if __name__ == "__main__":
    unittest.main()
