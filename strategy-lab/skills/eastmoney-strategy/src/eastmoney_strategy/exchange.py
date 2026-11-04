"""EastMoney same-day remote exchange coordinator."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from base_strategy.exchange import BaseExchange
from .account import EMAccount
from .ledger import EMLedger
from .models import EastMoneyOrder
from .utils import (
    EastMoneyTransport,
    _extract_rows,
    execution_row_to_order,
    order_row_to_model,
)


class EMExchange(BaseExchange, EastMoneyTransport):
    """Fetch same-day EastMoney order and execution state."""

    def __init__(
        self,
        *,
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
        self._accounts: dict[str, EMAccount] = {}
        self._ledgers: dict[str, EMLedger] = {}

    @classmethod
    def from_env(cls, *, dotenv_path: str | Path | None = ".env") -> "EMExchange":
        """Build an exchange coordinator from ``.env`` credentials."""
        return cls(**cls.env_kwargs(dotenv_path))

    @property
    def order_queue(self) -> list[EastMoneyOrder]:
        """Return latest active same-day remote orders across registered accounts."""
        orders: list[EastMoneyOrder] = []
        for account_id in self._accounts:
            orders.extend(self.fetch_pending_orders(account_id=account_id))
        return orders

    def register(
        self,
        account: EMAccount,
        ledger: EMLedger | None = None,
    ) -> EMAccount:
        """Register one remote account mirror and its optional history ledger."""
        account_id = str(account.account_id)
        if account_id in self._accounts:
            raise ValueError(f"Account already registered: {account_id}")
        self._accounts[account_id] = account
        if ledger is not None:
            ledger.subscribe(account)
            self._ledgers[account_id] = ledger
        return account

    def account(self, account_id: str, *, refresh: bool = True) -> EMAccount:
        """Return one registered account, optionally refreshing its mirror."""
        account = self._require_account(account_id)
        if refresh:
            account.refresh()
        return account

    def ledger(self, account_id: str) -> EMLedger:
        """Return the history ledger registered for one account."""
        try:
            return self._ledgers[str(account_id)]
        except KeyError as exc:
            raise KeyError(f"No ledger registered for account: {account_id}") from exc

    def fetch_pending_orders(
        self,
        *,
        account_id: str,
        rec_cnt: int | None = None,
    ) -> list[EastMoneyOrder]:
        """Return latest same-day active remote orders for one account."""
        rec_cnt = self.pending_order_page_size if rec_cnt is None else rec_cnt
        return [
            order
            for order in self.fetch_orders(account_id=account_id, rec_cnt=rec_cnt)
            if order.status in {EastMoneyOrder.SUBMITTED, EastMoneyOrder.PARTIAL}
        ]

    def _fetch_all_rows(
        self,
        *,
        account_id: str,
        endpoint_type: str,
        label: str,
        rec_idx: int,
        rec_cnt: int,
    ) -> list[dict[str, Any]]:
        if rec_idx < 0:
            raise ValueError("rec_idx must be non-negative")
        if rec_cnt <= 0:
            raise ValueError("rec_cnt must be positive")

        rows: list[dict[str, Any]] = []
        seen_pages: set[tuple[tuple[tuple[str, str], ...], ...]] = set()
        offset = rec_idx
        while True:
            request = self.request_trade if endpoint_type == "deals" else self.request_web_oper
            params = self._paged_params(account_id, endpoint_type, rec_idx=offset, rec_cnt=rec_cnt)
            if endpoint_type == "deals":
                params["zh"] = params.pop("zjzh")
                params.pop("reqUserid")
            data = request(params)
            page = _extract_rows(data, label=label)
            if not page:
                break
            signature = tuple(
                tuple(sorted((str(key), repr(value)) for key, value in row.items()))
                for row in page
            )
            if signature in seen_pages:
                raise RuntimeError(f"EastMoney repeated a {label} page at offset {offset}")
            seen_pages.add(signature)
            rows.extend(page)
            if len(page) < rec_cnt:
                break
            offset += len(page)
        return rows

    def fetch_orders(
        self,
        *,
        account_id: str,
        rec_idx: int = 0,
        rec_cnt: int | None = None,
    ) -> list[EastMoneyOrder]:
        """Return all same-day remote order rows, fetching every page."""
        self._require_account(account_id)
        rec_cnt = self.page_size if rec_cnt is None else rec_cnt
        rows = self._fetch_all_rows(
            account_id=account_id,
            endpoint_type="spo_orders_all",
            label="today orders",
            rec_idx=rec_idx,
            rec_cnt=rec_cnt,
        )
        return [
            order_row_to_model(account_id, row)
            for row in rows
        ]

    def fetch_executions(
        self,
        *,
        account_id: str,
        rec_idx: int = 0,
        rec_cnt: int | None = None,
    ) -> list[EastMoneyOrder]:
        """Return all same-day execution rows, fetching every page."""
        self._require_account(account_id)
        rec_cnt = self.page_size if rec_cnt is None else rec_cnt
        rows = self._fetch_all_rows(
            account_id=account_id,
            endpoint_type="deals",
            label="today executions",
            rec_idx=rec_idx,
            rec_cnt=rec_cnt,
        )
        return [
            order
            for row in rows
            if (order := execution_row_to_order(account_id, row)) is not None
        ]

    def resolve_order(self, order_id: str, *, account_id: str) -> EastMoneyOrder:
        """Find one active same-day remote order by local or EastMoney id."""
        for order in self.fetch_pending_orders(account_id=account_id):
            remote_order_id = str(order.meta.get("eastmoney_order_id") or "")
            if order.order_id == order_id or remote_order_id == order_id:
                return order
        raise KeyError(f"EastMoney order is not active or was not found: {order_id}")

    def record_order(self, order: EastMoneyOrder) -> EastMoneyOrder:
        """Forward a terminal order fact to its ledger when available."""
        self._require_account(order.account_id)
        ledger = self._ledgers.get(order.account_id)
        if ledger is not None and order.status in {
            EastMoneyOrder.FILLED,
            EastMoneyOrder.CANCELED,
            EastMoneyOrder.REJECTED,
            EastMoneyOrder.MARGIN,
            EastMoneyOrder.EXPIRED,
        }:
            ledger.record_order(order)
        return order

    def _paged_params(
        self,
        account_id: str,
        endpoint_type: str,
        *,
        rec_idx: int,
        rec_cnt: int,
    ) -> dict[str, str]:
        return {
            "type": endpoint_type,
            "zjzh": str(account_id),
            "reqUserid": "",
            "recIdx": str(int(rec_idx)),
            "recCnt": str(int(rec_cnt)),
        }

    def _require_account(self, account_id: str) -> EMAccount:
        try:
            return self._accounts[str(account_id)]
        except KeyError as exc:
            raise KeyError(f"No account registered: {account_id}") from exc
