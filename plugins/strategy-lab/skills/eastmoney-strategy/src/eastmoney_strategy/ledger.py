"""EastMoney historical order and execution ledger."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from base_strategy.ledger import BaseLedger
from .models import EastMoneyOrder
from .utils import (
    EastMoneyTransport,
    _extract_rows,
    _format_yyyymmdd,
    execution_row_to_order,
    order_row_to_model,
)


class EMLedger(BaseLedger, EastMoneyTransport):
    """Fetch and optionally store historical EastMoney order facts."""

    def __init__(
        self,
        *,
        ledger_id: str | None = None,
        user_id: str = "",
        ut_token: str = "",
        ct_token: str = "",
        pi: str | None = None,
        user_agent: str = "",
        app_version: str = "",
        platform: str = "",
        proxy: str | None = None,
        timeout: float = 20.0,
        retries: int = 1,
        retry_sleep: float = 1.0,
        page_size: int = 20,
        pending_order_page_size: int = 200,
        holdings_page_size: int = 200,
    ) -> None:
        BaseLedger.__init__(self)
        EastMoneyTransport.__init__(
            self,
            user_id=user_id,
            ut_token=ut_token,
            ct_token=ct_token,
            pi=pi,
            user_agent=user_agent,
            app_version=app_version,
            platform=platform,
            proxy=proxy,
            timeout=timeout,
            retries=retries,
            retry_sleep=retry_sleep,
            page_size=page_size,
            pending_order_page_size=pending_order_page_size,
            holdings_page_size=holdings_page_size,
        )
        self.ledger_id = str(ledger_id or "eastmoney")
        self.account_id: str | None = None
        self.account: Any | None = None

    @classmethod
    def from_env(
        cls,
        *,
        ledger_id: str | None = None,
        dotenv_path: str | Path | None = ".env",
    ) -> "EMLedger":
        """Build a ledger from ``.env`` credentials."""
        return cls(ledger_id=ledger_id, **cls.env_kwargs(dotenv_path))

    def subscribe(self, account: Any) -> None:
        """Subscribe this ledger to one EastMoney account."""
        account_id = getattr(account, "account_id", None)
        if not account_id:
            raise ValueError("account_id is required")
        if self.account_id is not None and self.account_id != str(account_id):
            raise ValueError(
                f"Ledger account_id {self.account_id} does not match {account_id}"
            )
        self.account = account
        self.account_id = str(account_id)

    def fetch_orders(
        self,
        *,
        start_date: str | date | datetime | pd.Timestamp | None = None,
        end_date: str | date | datetime | pd.Timestamp | None = None,
        rec_idx: int = 0,
        rec_cnt: int | None = None,
        record: bool = False,
    ) -> list[EastMoneyOrder]:
        """Return historical remote order rows for the subscribed account."""
        account_id = self._account_id()
        rec_cnt = self.page_size if rec_cnt is None else rec_cnt
        start_date, end_date = self._resolve_history_dates(
            start_date=start_date,
            end_date=end_date,
        )
        data = self.request_trade(
            self._history_params(
                "orders_his",
                start_date=start_date,
                end_date=end_date,
                rec_idx=rec_idx,
                rec_cnt=rec_cnt,
            )
        )
        orders = [
            order_row_to_model(account_id, row)
            for row in _extract_rows(data, label="order history")
        ]
        if record:
            for order in orders:
                self.record_order(order)
        return orders

    def fetch_executions(
        self,
        *,
        start_date: str | date | datetime | pd.Timestamp | None = None,
        end_date: str | date | datetime | pd.Timestamp | None = None,
        rec_idx: int = 0,
        rec_cnt: int | None = None,
        record: bool = False,
    ) -> list[EastMoneyOrder]:
        """Return historical remote execution rows as EastMoney orders."""
        account_id = self._account_id()
        rec_cnt = self.page_size if rec_cnt is None else rec_cnt
        start_date, end_date = self._resolve_history_dates(
            start_date=start_date,
            end_date=end_date,
        )
        data = self.request_trade(
            self._history_params(
                "deals_his",
                start_date=start_date,
                end_date=end_date,
                rec_idx=rec_idx,
                rec_cnt=rec_cnt,
            )
        )
        orders = [
            order
            for row in _extract_rows(data, label="execution history")
            if (order := execution_row_to_order(account_id, row)) is not None
        ]
        if record:
            for order in orders:
                self.record_execution(order)
        return orders

    def record_order(self, order: Any) -> Any:
        self._validate_account(getattr(order, "account_id", None))
        return super().record_order(order)

    def record_execution(self, execution: Any) -> Any:
        self._validate_account(getattr(execution, "account_id", None))
        return super().record_execution(execution)

    def _history_params(
        self,
        endpoint_type: str,
        *,
        start_date: str | date | datetime | pd.Timestamp,
        end_date: str | date | datetime | pd.Timestamp,
        rec_idx: int,
        rec_cnt: int,
    ) -> dict[str, str]:
        return {
            "type": endpoint_type,
            "zh": self._account_id(),
            "recIdx": str(int(rec_idx)),
            "recCnt": str(int(rec_cnt)),
            "startDate": _format_yyyymmdd(start_date),
            "endDate": _format_yyyymmdd(end_date),
        }

    def _resolve_history_dates(
        self,
        *,
        start_date: str | date | datetime | pd.Timestamp | None,
        end_date: str | date | datetime | pd.Timestamp | None,
    ) -> tuple[pd.Timestamp, pd.Timestamp]:
        account = self._require_account()
        resolved_end = (
            pd.Timestamp.today().normalize()
            if end_date is None
            else pd.Timestamp(end_date)
        )
        if start_date is not None:
            return pd.Timestamp(start_date), resolved_end
        if account.portfolio_start_date is None:
            detail = account._get_portfolio_detail()
            account.portfolio_start_date = account._extract_portfolio_start_date(detail)
        resolved_start = account.portfolio_start_date
        if resolved_start is None:
            resolved_start = pd.Timestamp.today().normalize() - pd.Timedelta(days=30)
        return resolved_start, resolved_end

    def _account_id(self) -> str:
        account = self._require_account()
        return str(account.account_id)

    def _require_account(self) -> Any:
        if self.account is None or self.account_id is None:
            raise ValueError("EMLedger is not subscribed to an account.")
        return self.account

    def _validate_account(self, account_id: Any) -> None:
        self._require_account()
        if str(account_id or "") != self.account_id:
            raise ValueError(
                f"Ledger account_id {self.account_id} does not match {account_id}"
            )
