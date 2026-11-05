"""Canonical trader store helpers and in-memory store middleware.

Stores persist the raw replayable records: orders and executions. Position,
NAV, market value, book value, and trades are derived by broker state or the
evaluator and are not canonical store data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

import pandas as pd

__all__ = ["Store", "to_excel"]


@dataclass
class Store:
    """Middleware store for terminal orders and completed executions.

    Brokers should keep only still-alive orders in memory for live routing.
    Once an order is terminal or an execution is confirmed, the broker hands it
    to this store. The store can then expose frames or persist records.
    """

    orders: list[Any] = field(default_factory=list)
    executions: list[Any] = field(default_factory=list)

    def record_order(self, order: Any) -> Any:
        """Store one terminal order if it is not already present."""
        if not _contains_record(self.orders, order, ("order_id", "oid")):
            self.orders.append(order)
        return order

    def record_execution(self, execution: Any) -> Any:
        """Store one confirmed execution if it is not already present."""
        if not _contains_record(self.executions, execution, ("execution_id", "did")):
            self.executions.append(execution)
        return execution

    def to_excel(self, path: str) -> None:
        """Persist stored orders and executions to an Excel workbook."""
        to_excel(orders=self.orders, executions=self.executions, path=path)


def _orders_to_frame(orders: Iterable[Any]) -> pd.DataFrame:
    """Normalize order-like records to a DataFrame."""
    return pd.DataFrame([_order_row(order) for order in orders])


def _executions_to_frame(executions: Iterable[Any]) -> pd.DataFrame:
    """Normalize execution-like records to a DataFrame."""
    return pd.DataFrame([_execution_row(execution) for execution in executions])


def to_excel(*, orders: Iterable[Any], executions: Iterable[Any], path: str) -> None:
    """Write canonical orders and executions to an Excel workbook."""
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        _orders_to_frame(orders).to_excel(writer, sheet_name="Order", index=False)
        _executions_to_frame(executions).to_excel(writer, sheet_name="Execution", index=False)


def _order_row(order: Any) -> dict[str, Any]:
    return {
        "order_id": _first_attr(order, "order_id", "oid"),
        "account_id": _first_attr(order, "account_id"),
        "client_id": _first_attr(order, "client_id"),
        "created_at": _first_attr(order, "created_at"),
        "code": _first_attr(order, "code"),
        "side": _first_attr(order, "side"),
        "quantity": _first_attr(order, "quantity"),
        "order_type": _first_attr(order, "order_type", "execution_type"),
        "limit_price": _first_attr(order, "limit_price"),
        "status": _first_attr(order, "status"),
        "filled_quantity": _first_attr(order, "filled_quantity", "filled"),
        "avg_price": _first_attr(order, "avg_price", "executed_price"),
        "comm": _first_attr(order, "comm"),
        "source": _first_attr(order, "source"),
        "meta": _first_attr(order, "meta"),
    }


def _execution_row(execution: Any) -> dict[str, Any]:
    return {
        "execution_id": _first_attr(execution, "execution_id", "did"),
        "order_id": _first_attr(execution, "order_id"),
        "account_id": _first_attr(execution, "account_id"),
        "client_id": _first_attr(execution, "client_id"),
        "timestamp": _first_attr(execution, "timestamp", "datetime"),
        "code": _first_attr(execution, "code"),
        "side": _first_attr(execution, "side"),
        "quantity": _first_attr(execution, "quantity"),
        "price": _first_attr(execution, "price"),
        "comm": _first_attr(execution, "comm"),
        "source": _first_attr(execution, "source"),
        "meta": _first_attr(execution, "meta"),
    }


_MISSING = object()


def _first_attr(value: Any, *names: str) -> Any:
    for name in names:
        result = getattr(value, name, _MISSING)
        if result is not _MISSING:
            return result
    return None


def _contains_record(records: Iterable[Any], record: Any, id_names: tuple[str, ...]) -> bool:
    record_id = _first_attr(record, *id_names)
    if record_id in (None, ""):
        return any(item is record for item in records)
    return any(_first_attr(item, *id_names) == record_id for item in records)
