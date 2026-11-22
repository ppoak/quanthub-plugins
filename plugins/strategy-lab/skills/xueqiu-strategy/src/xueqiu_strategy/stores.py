"""Persist Xueqiu session state and export account and ledger reports."""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
from datetime import date
from decimal import Decimal
import json
import sqlite3
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np
import pandas as pd

from .models import XueqiuExecution, XueqiuOrder, XueqiuPosition
from .utils import shanghai_time

__all__ = ["XueqiuStore", "to_excel"]


class XueqiuStore:
    """Store current state and append history in the same SQLite transaction."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._initialized = False
        self._offsets: dict[str, int] = {}
        self._collections: dict[str, list] = {}

    def load(self) -> dict:
        with closing(sqlite3.connect(self.path)) as db:
            state = json.loads(db.execute("SELECT payload FROM state WHERE id = 1").fetchone()[0])
            history: dict[str, list] = {}
            for kind, payload in db.execute("SELECT kind, payload FROM history ORDER BY kind, seq"):
                history.setdefault(kind, []).append(json.loads(payload))
            pending = [json.loads(payload) for _, payload in db.execute(
                "SELECT position, payload FROM pending_orders ORDER BY position"
            )]
        self._offsets = {kind: len(rows) for kind, rows in history.items()}
        self._initialized = True
        state["orders"] = history.get("orders", [])
        state["executions"] = history.get("executions", [])
        state["queue"] = pending
        for code, position in state["positions"].items():
            position["executions"] = history.get(f"position:{code}", [])
        return state

    def save(self, data: dict) -> None:
        self.commit(data)

    def commit(self, data: dict) -> None:
        """Commit account, history, queue, and checkpoint fields atomically."""
        data = data.copy()
        queue = data.pop("queue", [])
        histories = {kind: data.pop(kind) for kind in ("orders", "executions")}
        positions = {}
        for code, position in data["positions"].items():
            fields = vars(position).copy()
            histories[f"position:{code}"] = fields.pop("executions")
            positions[code] = fields
        data["positions"] = positions

        if self._initialized:
            self._write(self.path, data, histories, queue)
        else:
            # Build a new database away from the old file before replacing it atomically.
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=self.path.parent, delete=False) as stream:
                temporary = Path(stream.name)
            try:
                self._write(temporary, data, histories, queue)
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)
            self._initialized = True
        self._offsets = {kind: len(rows) for kind, rows in histories.items()}
        self._collections = histories

    def _write(self, path: Path, data: dict, histories: dict[str, list], queue: list) -> None:
        with closing(sqlite3.connect(path)) as db, db:
            if not self._initialized:
                db.execute("CREATE TABLE state (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
                db.execute(
                    "CREATE TABLE history (kind TEXT, seq INTEGER, payload TEXT NOT NULL, PRIMARY KEY(kind, seq))"
                )
                db.execute(
                    "CREATE TABLE pending_orders "
                    "(order_id TEXT PRIMARY KEY, position INTEGER NOT NULL, payload TEXT NOT NULL)"
                )
            for kind in self._offsets.keys() - histories.keys():
                db.execute("DELETE FROM history WHERE kind = ?", (kind,))
            for kind, rows in histories.items():
                start = self._offsets.get(kind, 0)
                if len(rows) < start or (kind in self._collections and self._collections[kind] is not rows):
                    db.execute("DELETE FROM history WHERE kind = ?", (kind,))
                    start = 0
                if len(rows) > start:
                    db.executemany(
                        "INSERT INTO history VALUES (?, ?, ?)",
                        ((kind, i, _dumps(rows[i])) for i in range(start, len(rows))),
                    )
            db.execute(
                "INSERT INTO state VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload "
                "WHERE state.payload != excluded.payload", (_dumps(data),),
            )
            current = {
                order.order_id: (position, _dumps(order))
                for position, order in enumerate(queue)
            }
            self._sync_pending_orders(db, current)

    def _sync_pending_orders(self, db, current: dict[str, tuple[int, str]]) -> None:
        """Apply only the queue rows that changed since the last checkpoint."""
        stored = {
            order_id: (position, payload)
            for order_id, position, payload in db.execute(
                "SELECT order_id, position, payload FROM pending_orders"
            )
        }
        for order_id in stored.keys() - current.keys():
            db.execute("DELETE FROM pending_orders WHERE order_id = ?", (order_id,))
        for order_id, (position, payload) in current.items():
            if stored.get(order_id) != (position, payload):
                db.execute(
                    "INSERT INTO pending_orders VALUES (?, ?, ?) "
                    "ON CONFLICT(order_id) DO UPDATE SET position=excluded.position, payload=excluded.payload",
                    (order_id, position, payload),
                )


def _dumps(value) -> str:
    return json.dumps(value, default=_encode, separators=(",", ":"))


def restore_execution(data: dict) -> XueqiuExecution:
    restored = dict(data, datetime=_timestamp(data["datetime"]))
    if "comm" in restored:
        restored["comm"] = Decimal(str(restored["comm"]))
    return XueqiuExecution(**restored)


def restore_order(data: dict) -> XueqiuOrder:
    return XueqiuOrder(**dict(
        data, created_at=_timestamp(data["created_at"]), executed_at=_timestamp(data["executed_at"]),
        executions=[restore_execution(item) for item in data["executions"]],
    ))


def restore_position(data: dict) -> XueqiuPosition:
    restored = dict(data, executions=[restore_execution(item) for item in data["executions"]])
    restored["realized_pnl_value"] = Decimal(str(restored.get("realized_pnl_value", 0)))
    return XueqiuPosition(**restored)


def _timestamp(value):
    return pd.Timestamp(value) if value is not None else None


def _encode(value):
    if isinstance(value, (date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, (XueqiuExecution, XueqiuOrder, XueqiuPosition)):
        return asdict(value)
    raise TypeError(f"Unsupported checkpoint value: {type(value).__name__}")


def to_excel(account, ledger, path: str) -> None:
    """Export the current Xueqiu account and ledger state to an Excel workbook.

    ``openpyxl`` is required. Install the ``excel`` extra of ``base-strategy``.
    """
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(destination, engine="openpyxl") as writer:
        pd.DataFrame(
            [{"cash": account.cash, "market_value": account.market_value, "nav": account.net_asset_value}]
        ).to_excel(writer, sheet_name="Account", index=False)
        pd.DataFrame(
            [
                {
                    "code": code,
                    "quantity": position.quantity,
                    "cost_price": position.cost_price,
                    "price": position.price,
                    "book_value": position.book_value,
                    "market_value": position.market_value,
                    "unrealized_pnl": position.unrealized_pnl,
                }
                for code, position in account.positions.items()
            ]
        ).to_excel(writer, sheet_name="Position", index=False)
        pd.DataFrame([dict(vars(order), created_at=shanghai_time(order.created_at),
                           executed_at=shanghai_time(order.executed_at)) for order in ledger.orders]).to_excel(
            writer, sheet_name="Order", index=False
        )
        pd.DataFrame([dict(vars(execution), datetime=shanghai_time(execution.datetime))
                      for execution in ledger.executions]).to_excel(
            writer, sheet_name="Execution", index=False
        )
