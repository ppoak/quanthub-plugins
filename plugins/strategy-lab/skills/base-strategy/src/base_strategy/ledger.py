"""Shared canonical fact ledger contract."""

from __future__ import annotations

from typing import Any, Iterable


class BaseLedger:
    """Base in-memory ledger for terminal orders and executions."""

    def __init__(
        self,
        *,
        orders: Iterable[Any] | None = None,
        executions: Iterable[Any] | None = None,
    ) -> None:
        self.orders: list[Any] = [] if orders is None else list(orders)
        self.executions: list[Any] = [] if executions is None else list(executions)

    def record_order(self, order: Any) -> Any:
        if not _contains_record(self.orders, order, ("order_id",)):
            self.orders.append(order)
        return order

    def record_execution(self, execution: Any) -> Any:
        if not _contains_record(self.executions, execution, ("execution_id",)):
            self.executions.append(execution)
        return execution


def _first_attr(value: Any, *names: str) -> Any:
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _contains_record(records: Iterable[Any], record: Any, id_names: tuple[str, ...]) -> bool:
    record_id = _first_attr(record, *id_names)
    if record_id in (None, ""):
        return any(item is record for item in records)
    return any(_first_attr(item, *id_names) == record_id for item in records)
