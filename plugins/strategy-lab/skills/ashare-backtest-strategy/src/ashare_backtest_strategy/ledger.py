"""A-share in-memory order and execution ledger."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Iterable, Mapping

import pandas as pd

from base_strategy.ledger import BaseLedger
from .models import AShareExecution, ASharePosition

SchemaField = str | tuple[str, str | Sequence[str]]

ORDER_SCHEMA: tuple[SchemaField, ...] = (
    ("order_id", ("order_id", "oid")), "account_id", "client_id", "created_at",
    "code", "side", "quantity", ("order_type", ("order_type", "execution_type")),
    "limit_price", "status", ("filled_quantity", ("filled_quantity", "filled")),
    ("avg_price", ("avg_price", "executed_price")), "comm", "source", "meta",
)
EXECUTION_SCHEMA: tuple[SchemaField, ...] = (
    ("execution_id", ("execution_id", "did")), "order_id", "account_id", "client_id",
    ("timestamp", ("timestamp", "datetime")), "code", "side", "quantity", "price",
    "comm", "source", "meta",
)


class AShareLedger(BaseLedger):
    """Single-account in-memory ledger for terminal orders and executions."""

    _SEQ: dict[str, int] = {}

    def __init__(self, *, ledger_id: str | None = None) -> None:
        self.ledger_id = str(ledger_id or self._auto_id())
        self.account_id: str | None = None
        self.account: Any | None = None
        self.init_cash = 0.0
        self._orders: list[Any] = []
        self._executions: list[AShareExecution] = []
        self._order_ids: set[Any] = set()
        self._execution_ids: set[Any] = set()
        self._cache: dict[str, pd.DataFrame | None] = {"orders": None, "executions": None}
        self._dirty = {"orders": True, "executions": True}

    @classmethod
    def _auto_id(cls) -> str:
        cls._SEQ[cls.__name__] = cls._SEQ.get(cls.__name__, 0) + 1
        return f"{cls.__name__}#{cls._SEQ[cls.__name__]}"

    @property
    def orders(self) -> list[Any]:
        """Terminal orders recorded by this ledger."""
        return list(self._orders)

    @property
    def executions(self) -> list[AShareExecution]:
        """Completed executions recorded by this ledger."""
        return list(self._executions)

    def subscribe(self, account: Any) -> None:
        """Subscribe to this ledger's account."""
        account_id = getattr(account, "account_id", None)
        if not account_id:
            raise ValueError("account_id is required")
        account_id = str(account_id)
        if self.account_id is not None and account_id != self.account_id:
            raise ValueError(f"Ledger account_id {self.account_id} does not match {account_id}")
        self.account = account
        self.account_id = account_id
        self.init_cash = float(getattr(account, "init_cash", self.init_cash) or 0.0)

    def record_order(self, order: Any) -> Any:
        return self._record("orders", order, "order_id")

    def record_execution(self, execution: AShareExecution) -> AShareExecution:
        return self._record("executions", execution, "execution_id")

    def orders_frame(
        self,
        *,
        start: Any | None = None,
        end: Any | None = None,
        status: str | None = None,
        extra_schema: Iterable[SchemaField] | None = None,
    ) -> pd.DataFrame:
        frame = self._frame("orders", ORDER_SCHEMA, extra_schema)
        if frame.empty or (start is None and end is None and status is None):
            return frame
        mask = pd.Series(True, index=frame.index)
        if status is not None:
            mask &= frame["status"] == status
        if start is not None or end is not None:
            mask &= self._time_mask(frame["created_at"], start=start, end=end)
        return frame.loc[mask].copy()

    def executions_frame(
        self,
        *,
        code: str | None = None,
        start: Any | None = None,
        end: Any | None = None,
        extra_schema: Iterable[SchemaField] | None = None,
    ) -> pd.DataFrame:
        frame = self._frame("executions", EXECUTION_SCHEMA, extra_schema)
        if frame.empty or (start is None and end is None and code is None):
            return frame
        mask = pd.Series(True, index=frame.index)
        if code is not None:
            mask &= frame["code"] == code
        if start is not None or end is not None:
            mask &= self._time_mask(frame["timestamp"], start=start, end=end)
        return frame.loc[mask].copy()

    def snapshot(self, *, timestamp: Any | None = None, prices: Mapping[str, float] | None = None) -> dict[str, Any]:
        """Replay executions and return account state at one timestamp."""
        frame = self.executions_frame(end=timestamp, extra_schema=["cashflow"])
        cash = self.init_cash + (0.0 if frame.empty else float(frame["cashflow"].fillna(0.0).sum()))
        positions: dict[str, ASharePosition] = {}
        skip = {AShareExecution.TRANSFER, AShareExecution.WITHDRAW, AShareExecution.DIVIDEND}

        for index in frame.sort_values("timestamp", na_position="first").index:
            execution = self._executions[index]
            if execution.code == "CASH" or execution.side in skip:
                continue
            position = positions.get(execution.code)
            if position is None:
                position = positions[execution.code] = ASharePosition(code=execution.code)
            position.apply_execution(execution)
            position.update_price(float(execution.price))
        for code, price in (prices or {}).items():
            if code in positions:
                positions[code].update_price(float(price))

        market_value = sum(position.market_value for position in positions.values())
        book_value = sum(position.book_value for position in positions.values())
        return {
            "account_id": self._require_account_id(),
            "timestamp": pd.to_datetime(timestamp) if timestamp is not None else None,
            "cash": cash,
            "positions": positions,
            "book_value": book_value,
            "market_value": market_value,
            "net_asset_value": cash + market_value,
        }

    def _record(self, bucket: str, record: Any, id_attr: str) -> Any:
        self._validate_account(record)
        items = self._orders if bucket == "orders" else self._executions
        record_ids = self._order_ids if bucket == "orders" else self._execution_ids
        record_id = getattr(record, id_attr, None)
        if not record_id or record_id not in record_ids:
            items.append(record)
            if record_id:
                record_ids.add(record_id)
            self._dirty[bucket] = True
        return record

    def _frame(
        self,
        bucket: str,
        schema: Sequence[SchemaField],
        extra_schema: Iterable[SchemaField] | None,
    ) -> pd.DataFrame:
        records = self._orders if bucket == "orders" else self._executions
        if extra_schema is not None:
            return _records_to_frame(records, schema=schema, extra_schema=tuple(extra_schema))
        if self._dirty[bucket] or self._cache[bucket] is None:
            self._cache[bucket] = _records_to_frame(records, schema=schema)
            self._dirty[bucket] = False
        return self._cache[bucket].copy()

    def _validate_account(self, record: Any) -> None:
        ledger_account_id = self._require_account_id()
        account_id = getattr(record, "account_id", None)
        if not account_id:
            raise ValueError("record account_id is required")
        if str(account_id) != ledger_account_id:
            raise ValueError(f"Ledger account_id {ledger_account_id} does not match {account_id}")

    def _require_account_id(self) -> str:
        if self.account_id is None:
            raise ValueError("AShareLedger is not subscribed to an account.")
        return self.account_id

    @staticmethod
    def _time_mask(values: pd.Series, *, start: Any | None, end: Any | None) -> pd.Series:
        timestamps = pd.to_datetime(values)
        mask = pd.Series(True, index=values.index)
        if start is not None:
            mask &= timestamps >= pd.to_datetime(start)
        if end is not None:
            mask &= timestamps <= pd.to_datetime(end)
        return mask & timestamps.notna() if start is not None or end is not None else mask


def _records_to_frame(
    records: Iterable[Any],
    *,
    schema: Sequence[SchemaField],
    extra_schema: Sequence[SchemaField] = (),
) -> pd.DataFrame:
    materialized = list(records)
    fields = tuple(schema) + tuple(extra_schema)
    compiled_fields = [
        (column, tuple(tuple(path.split(".")) for path in paths))
        for column, paths in map(_parse_schema_field, fields)
    ]
    object_records = all(not isinstance(record, dict) for record in materialized)
    data = {}
    for column, paths in compiled_fields:
        if object_records and paths and len(paths[0]) == 1:
            attribute = paths[0][0]
            if len(paths) == 1:
                values = [getattr(record, attribute, None) for record in materialized]
            elif len(paths) == 2 and len(paths[1]) == 1:
                fallback = paths[1][0]
                values = [
                    value if (value := getattr(record, attribute, _MISSING)) is not _MISSING
                    else getattr(record, fallback, None)
                    for record in materialized
                ]
            else:
                values = [getattr(record, attribute, _MISSING) for record in materialized]
                for index, value in enumerate(values):
                    if value is _MISSING:
                        values[index] = _first_compiled_path(materialized[index], paths[1:])
        else:
            values = [_first_compiled_path(record, paths) for record in materialized]
        data[column] = values
    return pd.DataFrame(data)


def _parse_schema_field(field: SchemaField) -> tuple[str, tuple[str, ...]]:
    if isinstance(field, str):
        return field, (field,)
    column, paths = field
    return column, (paths,) if isinstance(paths, str) else tuple(paths)


_MISSING = object()


def _first_compiled_path(value: Any, paths: Sequence[Sequence[str]]) -> Any:
    for parts in paths:
        current = value
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part, _MISSING)
            else:
                current = getattr(current, part, _MISSING)
            if current is _MISSING:
                break
        else:
            return current
    return None
