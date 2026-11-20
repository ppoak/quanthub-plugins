"""Xueqiu local order and execution ledger."""

from __future__ import annotations

from typing import Any

import pandas as pd

from base_strategy.ledger import BaseLedger
from .models import XueqiuExecution
from .utils import from_xueqiu_symbol


class XueqiuLedger(BaseLedger):
    """Terminal orders and executions, with optional remote history import."""

    def __init__(self, *, ledger_id: str | None = None, client: Any | None = None, gid: str | None = None) -> None:
        super().__init__()
        self.ledger_id = str(ledger_id or "xueqiu")
        self.client = client
        self.gid = str(gid) if gid is not None else None
        self.account: Any | None = None
        self.account_id: str | None = None

    def subscribe(self, account: Any) -> None:
        if self.account_id is not None and self.account_id != account.account_id:
            raise ValueError("Ledger is already subscribed to another account")
        self.account = account
        self.account_id = str(getattr(account, "account_id", "") or "")

    def order_history(self, *, row: int = 50) -> list[dict[str, Any]]:
        """Return raw remote Xueqiu transaction records."""
        client = self._require_client()
        result = client.ensure_success(
            client.transaction_records(self._require_gid(), row=row),
            "transaction records",
        )
        data = result.get("result_data") or {}
        records = data.get("transactions", data.get("list", []))
        return records if isinstance(records, list) else []

    def execution_history(self, *, row: int = 50, record: bool = False) -> list[XueqiuExecution]:
        """Return remote Xueqiu transaction records as normalized executions."""
        executions = [
            execution
            for item in self.order_history(row=row)
            if (execution := self._transaction_to_execution(item)) is not None
        ]
        if record:
            for execution in executions:
                self.record_execution(execution)
        return executions

    def transfer_history(self, *, row: int = 50) -> list[dict[str, Any]]:
        """Return raw remote bank-transfer records."""
        client = self._require_client()
        result = client.ensure_success(
            client.bank_transfer_records(self._require_gid(), row=row),
            "bank transfer records",
        )
        data = result.get("result_data") or {}
        records = data.get("bank_transfers", data.get("list", []))
        return records if isinstance(records, list) else []

    def _transaction_to_execution(self, item: dict[str, Any]) -> XueqiuExecution | None:
        symbol = from_xueqiu_symbol(item.get("symbol") or item.get("code") or "")
        quantity = float(item.get("shares") or item.get("quantity") or 0.0)
        price = float(item.get("price") or 0.0)
        if not symbol or quantity <= 0 or price <= 0:
            return None
        side_value = str(item.get("type") or item.get("side") or "").lower()
        side = XueqiuExecution.SELL if side_value in {"2", "sell", "s"} else XueqiuExecution.BUY
        execution_id = item.get("tid") or item.get("id") or item.get("transaction_id")
        if item.get("time") is not None:
            timestamp = pd.to_datetime(item["time"], unit="ms", utc=True, errors="coerce")
            timestamp = timestamp.tz_convert("Asia/Shanghai").tz_localize(None)
        else:
            timestamp = pd.to_datetime(item.get("date") or item.get("created_at"), errors="coerce")
        if not execution_id or pd.isna(timestamp):
            return None
        return XueqiuExecution(
            execution_id=str(execution_id),
            account_id=self.account_id or "",
            order_id=str(item.get("order_id") or ""),
            datetime=timestamp,
            side=side,
            code=symbol,
            quantity=quantity,
            price=price,
            comm=float(item.get("commission") or item.get("comm") or 0.0),
            meta={"xueqiu_remote": item},
        )

    def _require_client(self) -> Any:
        if self.client is None:
            raise ValueError("XueqiuLedger requires a client for remote history")
        return self.client

    def _require_gid(self) -> str:
        if not self.gid:
            raise ValueError("XueqiuLedger requires gid for remote history")
        return self.gid
