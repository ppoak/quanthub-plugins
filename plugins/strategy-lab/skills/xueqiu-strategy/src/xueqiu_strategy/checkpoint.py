"""Persist a resumable Xueqiu session without serializing credentials."""

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


class Checkpoint:
    """Store current state and append history in the same SQLite transaction."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._initialized = False
        self._offsets: dict[str, int] = {}
        self._collections: dict[str, list] = {}

    def load(self) -> dict:
        with self.path.open("rb") as stream:
            if stream.read(16) != b"SQLite format 3\0":
                stream.seek(0)
                return json.load(stream)

        with closing(sqlite3.connect(self.path)) as db:
            state = json.loads(db.execute("SELECT payload FROM state WHERE id = 1").fetchone()[0])
            history: dict[str, list] = {}
            for kind, payload in db.execute("SELECT kind, payload FROM history ORDER BY kind, seq"):
                history.setdefault(kind, []).append(json.loads(payload))
        self._offsets = {kind: len(rows) for kind, rows in history.items()}
        self._initialized = True
        state["orders"] = history.get("orders", [])
        state["executions"] = history.get("executions", [])
        for code, position in state["positions"].items():
            position["executions"] = history.get(f"position:{code}", [])
        return state

    def save(self, data: dict) -> None:
        data = data.copy()
        histories = {kind: data.pop(kind) for kind in ("orders", "executions")}
        positions = {}
        for code, position in data["positions"].items():
            fields = vars(position).copy()
            histories[f"position:{code}"] = fields.pop("executions")
            positions[code] = fields
        data["positions"] = positions

        if self._initialized:
            self._write(self.path, data, histories)
        else:
            # Build new databases away from the old file, including JSON migration.
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=self.path.parent, delete=False) as stream:
                temporary = Path(stream.name)
            try:
                self._write(temporary, data, histories)
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)
            self._initialized = True
        self._offsets = {kind: len(rows) for kind, rows in histories.items()}
        self._collections = histories

    def _write(self, path: Path, data: dict, histories: dict[str, list]) -> None:
        with closing(sqlite3.connect(path)) as db, db:
            if not self._initialized:
                db.execute("CREATE TABLE state (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
                db.execute(
                    "CREATE TABLE history (kind TEXT, seq INTEGER, payload TEXT NOT NULL, PRIMARY KEY(kind, seq))"
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


def _dumps(value) -> str:
    return json.dumps(value, default=_encode, separators=(",", ":"))


def restore_execution(data: dict) -> XueqiuExecution:
    return XueqiuExecution(**dict(data, datetime=_timestamp(data["datetime"])))


def restore_order(data: dict) -> XueqiuOrder:
    return XueqiuOrder(**dict(
        data, created_at=_timestamp(data["created_at"]), executed_at=_timestamp(data["executed_at"]),
        executions=[restore_execution(item) for item in data["executions"]],
    ))


def restore_position(data: dict) -> XueqiuPosition:
    return XueqiuPosition(**dict(data, executions=[restore_execution(item) for item in data["executions"]]))


def _timestamp(value):
    return pd.Timestamp(value) if value is not None else None


def _encode(value):
    if isinstance(value, (date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, (XueqiuExecution, XueqiuOrder, XueqiuPosition)):
        return asdict(value)
    raise TypeError(f"Unsupported checkpoint value: {type(value).__name__}")
