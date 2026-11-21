"""Persist a resumable Xueqiu session without serializing credentials."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np
import pandas as pd

from .models import XueqiuExecution, XueqiuOrder, XueqiuPosition


def load_checkpoint(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_checkpoint(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, default=_encode)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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
