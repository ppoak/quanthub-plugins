"""Xueqiu local order and execution ledger."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pandas as pd

from base_strategy.ledger import BaseLedger
from .errors import XueqiuAPIError
from .fees import fee_amount
from .models import XueqiuExecution
from .utils import from_xueqiu_symbol, shanghai_time


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

    def record_execution(self, execution: XueqiuExecution) -> XueqiuExecution:
        """Store a confirmed fact once, even when it has a local and a remote ID."""
        remote = execution.meta.get("xueqiu_remote") or {}
        remote_id = execution.meta.get("xueqiu_tid")
        comment = remote.get("comment")
        for existing in self.executions:
            if remote_id and existing.meta.get("xueqiu_tid") == remote_id:
                return existing
            if comment and existing.meta.get("xueqiu_comment") == comment:
                existing.meta["xueqiu_tid"] = remote_id
                return existing
        return super().record_execution(execution)

    def order_history(
        self, *, row: int = 50, pos: int | str | None = None, all_pages: bool = False,
    ) -> list[dict[str, Any]]:
        """Return raw remote Xueqiu transaction records."""
        return self._history(self._require_client().transaction_records, "transactions", row, pos, all_pages)

    def execution_history(
        self, *, row: int = 50, record: bool = False, pos: int | str | None = None, all_pages: bool = False,
    ) -> list[XueqiuExecution]:
        """Return remote Xueqiu transaction records as normalized executions."""
        executions = [
            execution
            for item in self.order_history(row=row, pos=pos, all_pages=all_pages)
            if (execution := self._transaction_to_execution(item)) is not None
        ]
        if record:
            for execution in executions:
                self.record_execution(execution)
        return executions

    def transfer_history(
        self, *, row: int = 50, pos: int | str | None = None, all_pages: bool = False,
    ) -> list[dict[str, Any]]:
        """Return raw remote bank-transfer records."""
        return self._history(self._require_client().bank_transfer_records, "bank_transfers", row, pos, all_pages)

    def _history(self, fetch, key: str, row: int, pos, all_pages: bool) -> list[dict[str, Any]]:
        client = self._require_client()
        gid = self._require_gid()
        records: list[dict[str, Any]] = []
        cursors = {str(pos)} if pos is not None else set()
        while True:
            response = fetch(gid, row=row) if pos is None else fetch(gid, row=row, pos=pos)
            data = client.ensure_success(response, key).get("result_data")
            page = data.get(key, data.get("list")) if isinstance(data, dict) else None
            if not isinstance(page, list):
                if all_pages:
                    raise XueqiuAPIError(f"Missing {key} ledger records")
                return records
            if not page:
                return records
            records.extend(page)
            pos = data.get("pos")
            if not all_pages or not pos or str(pos) in cursors:
                return records
            cursors.add(str(pos))

    def _transaction_to_execution(self, item: dict[str, Any]) -> XueqiuExecution | None:
        symbol = from_xueqiu_symbol(item.get("symbol") or item.get("code") or "")
        quantity = float(item.get("shares") or item.get("quantity") or 0.0)
        price = float(item.get("price") or 0.0)
        if not symbol or quantity <= 0 or price <= 0:
            return None
        side_value = str(item.get("type") or item.get("side") or "").lower()
        if side_value in {"1", "buy", "b"}:
            side = XueqiuExecution.BUY
        elif side_value in {"2", "sell", "s"}:
            side = XueqiuExecution.SELL
        else:
            return None
        execution_id = item.get("tid") or item.get("id") or item.get("transaction_id")
        if item.get("time") is not None:
            timestamp = pd.to_datetime(item["time"], unit="ms", utc=True, errors="coerce")
            timestamp = shanghai_time(timestamp)
        else:
            timestamp = shanghai_time(pd.to_datetime(item.get("date") or item.get("created_at"), errors="coerce"))
        if not execution_id or pd.isna(timestamp):
            return None
        gross = Decimal(str(price)) * Decimal(str(quantity))
        commission = item.get("commission")
        if commission is None:
            commission = item.get("comm")
        fees = {
            "commission": fee_amount(commission, item.get("commission_rate") or 0, gross, "commission"),
            "tax": fee_amount(item.get("tax"), item.get("tax_rate") or 0, gross, "tax"),
        }
        return XueqiuExecution(
            execution_id=str(execution_id),
            account_id=self.account_id or "",
            order_id=str(item.get("order_id") or ""),
            datetime=timestamp,
            side=side,
            code=symbol,
            quantity=quantity,
            price=price,
            comm=float(sum(fees.values())),
            meta={"xueqiu_remote": item, "xueqiu_tid": str(execution_id),
                  "fees": {name: float(amount) for name, amount in fees.items()}},
        )

    def _require_client(self) -> Any:
        if self.client is None:
            raise ValueError("XueqiuLedger requires a client for remote history")
        return self.client

    def _require_gid(self) -> str:
        if not self.gid:
            raise ValueError("XueqiuLedger requires gid for remote history")
        return self.gid
