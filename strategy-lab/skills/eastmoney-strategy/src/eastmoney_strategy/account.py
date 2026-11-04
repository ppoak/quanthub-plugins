"""EastMoney remote account mirror."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pandas as pd

from base_strategy.account import BaseAccount
from .constants import LIST_URL, SIM_QUERY_URL
from .errors import EastMoneyApiError, EastMoneyAuthError
from .models import EastMoneyPosition
from .utils import (
    EastMoneyTransport,
    _extract_rows,
    _find_first_key,
    _has_auth_error,
    _to_float,
    normalize_code,
)


class EMAccount(BaseAccount, EastMoneyTransport):
    """Mutable mirror of one EastMoney simulated portfolio."""

    def __init__(
        self,
        *,
        account_id: str,
        init_cash: float = 0.0,
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
        self.account_id = str(account_id)
        self.init_cash = float(init_cash)
        self.available_cash = float(init_cash)
        self.frozen_cash = 0.0
        self.fund_balance = float(init_cash)
        self.total_asset = float(init_cash)
        self.market_value = 0.0
        self.positions: dict[str, EastMoneyPosition] = {}
        self.portfolio_name = ""
        self.portfolio_market: str | None = None
        self.portfolio_net_value: str | None = None
        self.portfolio_day_return: str | None = None
        self.portfolio_total_return: str | None = None
        self.portfolio_start_date: pd.Timestamp | None = None
        self.raw_account_info: dict[str, Any] | None = None
        self.raw_holdings: dict[str, Any] | None = None
        self.raw_portfolio: dict[str, Any] | None = None

    @classmethod
    def from_env(
        cls,
        *,
        account_id: str | None = None,
        dotenv_path: str | Path | None = ".env",
        init_cash: float = 0.0,
    ) -> "EMAccount":
        """Build an account mirror from ``.env`` and process environment variables."""
        config = cls.env_kwargs(dotenv_path)
        account_id = account_id or os.environ.get(
            "EASTMONEY_MONI_ACCOUNT_ID", ""
        ).strip()
        if not account_id:
            raise ValueError(
                "account_id or EASTMONEY_MONI_ACCOUNT_ID is required"
            )
        return cls(
            account_id=account_id,
            init_cash=init_cash,
            **config,
        )

    @property
    def cash(self) -> float:
        return self.available_cash

    @property
    def book_value(self) -> float:
        return sum(position.quantity * position.cost_price for position in self.positions.values())

    @property
    def net_asset_value(self) -> float:
        return self.total_asset

    def position(self, code: str) -> EastMoneyPosition:
        """Return the current position object for one instrument code."""
        normalized = normalize_code(code)
        if normalized not in self.positions:
            self.positions[normalized] = EastMoneyPosition(code=normalized)
        return self.positions[normalized]

    def update_price(self, code: str, price: float) -> None:
        """Update latest mark price for one mirrored position."""
        position = self.position(code)
        position.last_price = float(price)
        position.market_value = position.quantity * position.last_price

    def available_quantity(self, code: str) -> float:
        """Return server-reported sellable quantity for one code."""
        return self.positions.get(
            normalize_code(code),
            EastMoneyPosition(code=normalize_code(code)),
        ).available_quantity

    def list_accounts(self) -> list[dict[str, Any]]:
        """Return EastMoney simulated portfolios visible to the authenticated user."""
        self.require_identity()
        data = self.request_json(
            LIST_URL,
            {
                "type": "zhl_get_order_allList",
                "zuhelist": "",
                "userId": self.user_id,
                "plat": self.platform,
                "ver": self.app_version,
                "utToken": self.ut_token,
                "ctToken": self.ct_token,
            },
        )
        self._raise_list_error_if_needed(data)
        rows = data.get("data") or []
        if not isinstance(rows, list):
            raise EastMoneyApiError("Unexpected account list response: data is not a list")
        return [
            self._normalize_account_row(row)
            for row in rows
            if isinstance(row, dict)
        ]

    def find_account(self, name: str) -> dict[str, Any] | None:
        """Find one EastMoney simulated portfolio by display name."""
        target = str(name)
        for account in self.list_accounts():
            if account["name"] == target:
                return account
        return None

    def user_basic(self, *, user_id: str | None = None) -> dict[str, Any]:
        """Return EastMoney web-profile summary for the authenticated user."""
        resolved_user_id = user_id or self.user_id
        if not resolved_user_id:
            raise ValueError("user_id is required")
        data = self.request_json(
            SIM_QUERY_URL,
            self._web_params(
                {
                    "type": "spo_user_basic",
                    "userid": str(resolved_user_id),
                }
            ),
        )
        self._raise_list_error_if_needed(data)
        payload = data.get("data") or {}
        if not isinstance(payload, dict):
            raise EastMoneyApiError("Unexpected user_basic response: data is not an object")
        return payload

    def list_accounts_web(self, *, user_id: str | None = None) -> list[dict[str, Any]]:
        """Return portfolios using the same endpoint as ``MyCombin.html``."""
        resolved_user_id = user_id or self.user_id
        if not resolved_user_id:
            raise ValueError("user_id is required")
        data = self.request_json(
            SIM_QUERY_URL,
            self._web_params(
                {
                    "type": "spo_zuhe_preview",
                    "userid": str(resolved_user_id),
                }
            ),
        )
        self._raise_list_error_if_needed(data)
        return [
            self._normalize_account_row(row)
            for row in self._web_rows(data)
            if isinstance(row, dict)
        ]

    def open_account(
        self,
        *,
        name: str,
        init_cash: float,
        comment: str = "",
        permit: int = 0,
    ) -> dict[str, Any]:
        """Open a new EastMoney simulated portfolio through the server."""
        data = self.request_trade(
            {
                "type": "reg_zhuhe",
                "zhName": str(name),
                "iniBal": self.format_price(init_cash),
                "zhComment": str(comment),
                "zhPermit": str(int(permit)),
            }
        )
        portfolio_id = _find_first_key(
            data,
            ("zh", "zhbs", "zjzh", "portfolioId", "portfolio_id"),
        )
        if portfolio_id not in (None, ""):
            self.account_id = str(portfolio_id)
        self.init_cash = float(init_cash)
        return data

    def close_account(self) -> dict[str, Any]:
        """Close this EastMoney simulated portfolio through the server."""
        if not self.account_id:
            raise ValueError("account_id is required")
        return self.request_trade({"type": "delete_zhuhe", "zh": self.account_id})

    def _replace(
        self,
        *,
        account_info: dict[str, Any],
        positions: dict[str, EastMoneyPosition],
        raw_holdings: dict[str, Any],
        portfolio_start_date: pd.Timestamp | None = None,
    ) -> None:
        """Replace the remote mirror atomically after a successful refresh."""
        self._apply_account_info(account_info)
        self.positions = dict(positions)
        self.raw_holdings = raw_holdings
        if portfolio_start_date is not None:
            self.portfolio_start_date = portfolio_start_date

    def refresh(self, *, rec_cnt: int | None = None) -> None:
        """Refresh this account from complete EastMoney account payloads."""
        rec_cnt = self.holdings_page_size if rec_cnt is None else rec_cnt
        account_info = self._get_account_info()
        holdings = self._get_holdings(rec_idx=0, rec_cnt=rec_cnt)
        self._replace(
            account_info=account_info,
            positions=self._parse_positions(holdings),
            raw_holdings=holdings,
            portfolio_start_date=(
                self._extract_portfolio_start_date(account_info)
                or self.portfolio_start_date
            ),
        )

    def _get_portfolio_detail(self) -> dict[str, Any]:
        data = self.request_trade({"type": "zhuhe_detail_new", "zh": self.account_id})
        rows = data.get("listData") or []
        if not rows:
            return data
        detail = rows[0]
        if not isinstance(detail, dict):
            raise EastMoneyApiError("Unexpected portfolio detail response")
        return detail

    def _get_account_info(self) -> dict[str, Any]:
        return self.request_web_oper(
            {
                "type": "spo_bal_info",
                "zjzh": self.account_id,
            }
        )

    def _get_holdings(self, *, rec_idx: int = 0, rec_cnt: int = 20) -> dict[str, Any]:
        return self.request_web_oper(
            {
                "type": "spo_hold",
                "zjzh": self.account_id,
                "reqUserid": "",
                "recIdx": str(int(rec_idx)),
                "recCnt": str(int(rec_cnt)),
            }
        )

    def _apply_account_info(self, data: dict[str, Any]) -> None:
        row = self._account_info_row(data)
        self.available_cash = _to_float(
            row.get("kyye") or row.get("kyzj") or row.get("availableCash") or row.get("cash")
        )
        self.frozen_cash = _to_float(row.get("sdje") or row.get("frozenCash"))
        self.fund_balance = _to_float(row.get("zjye") or row.get("fundBalance") or row.get("cashBalance"))
        self.total_asset = _to_float(row.get("zzc") or row.get("totalAsset") or row.get("zhye"))
        self.market_value = _to_float(
            row.get("mktVal") or row.get("mktValue") or row.get("marketValue")
        )
        if self.fund_balance <= 0:
            self.fund_balance = self.available_cash + self.frozen_cash
        if self.total_asset <= 0:
            self.total_asset = self.fund_balance + self.market_value
        self.raw_account_info = data

    @classmethod
    def _parse_positions(cls, data: dict[str, Any]) -> dict[str, EastMoneyPosition]:
        positions = {}
        for row in _extract_rows(data, label="holdings"):
            position = cls._parse_holding_row(row)
            if position is not None:
                positions[position.code] = position
        return positions

    @classmethod
    def _parse_holding_row(cls, row: dict[str, Any]) -> EastMoneyPosition | None:
        code = str(row.get("__code") or row.get("code") or row.get("stockCode") or row.get("stkCode") or "").strip()
        name = str(row.get("__name") or row.get("name") or row.get("stkName") or "").strip()
        if not code or code == "000000" or name.lower() == "cash" or name == "现金":
            return None
        quantity = _to_float(row.get("zqsl") or row.get("vol") or row.get("quantity") or row.get("shares"))
        if quantity <= 0:
            return None
        return EastMoneyPosition(
            code=normalize_code(code),
            name=name,
            quantity=quantity,
            available_quantity=_to_float(row.get("kysl") or row.get("availableQuantity") or row.get("available")),
            cost_price=_to_float(row.get("cbj") or row.get("costPrice") or row.get("price")),
            last_price=cls._holding_last_price(row),
            market_value=_to_float(row.get("mktVal") or row.get("marketValue")),
            holding_profit_loss=_to_float(row.get("webYk") or row.get("__fdyk") or row.get("fdyk") or row.get("profitLoss") or row.get("pnl")),
            holding_profit_loss_ratio=_to_float(row.get("webYkRate") or row.get("rateMax") or row.get("profitLossRatio") or row.get("pnlRatio")),
            day_profit_loss=_to_float(row.get("dayYk") or row.get("dayProfitLoss") or row.get("todayPnl")),
            day_profit_loss_ratio=_to_float(row.get("dayYkRate") or row.get("dayProfitLossRatio") or row.get("todayPnlRatio")),
            profit_loss=_to_float(row.get("webYk") or row.get("__fdyk") or row.get("fdyk") or row.get("profitLoss") or row.get("pnl")),
            profit_loss_ratio=_to_float(row.get("webYkRate") or row.get("rateMax") or row.get("profitLossRatio") or row.get("pnlRatio")),
            raw=row,
        )

    @classmethod
    def _extract_portfolio_start_date(cls, data: dict[str, Any] | None) -> pd.Timestamp | None:
        if not isinstance(data, dict):
            return None
        rows = _extract_rows(data, label="portfolio start date") if data.get("listData") else []
        candidates = rows or [data]
        for row in candidates:
            if not isinstance(row, dict):
                continue
            value = row.get("startDate") or row.get("khrq") or row.get("cjrq") or row.get("createdAt")
            timestamp = pd.to_datetime(value, format="%Y%m%d", errors="coerce")
            if pd.isna(timestamp):
                timestamp = pd.to_datetime(value, errors="coerce")
            if not pd.isna(timestamp):
                return timestamp.normalize()
        return None

    @staticmethod
    def _account_info_row(data: dict[str, Any]) -> dict[str, Any]:
        rows = _extract_rows(data, label="account info")
        row = rows[0] if rows else data.get("data") if isinstance(data.get("data"), dict) else data
        return row if isinstance(row, dict) else {}

    @staticmethod
    def _holding_last_price(row: dict[str, Any]) -> float:
        price = _to_float(row.get("__zxjg") or row.get("latestPrice"))
        if price > 0:
            return price
        new_price = _to_float(row.get("newPrc"))
        if new_price > 1000:
            return new_price / 1000.0
        return new_price

    def _web_params(self, params: dict[str, Any]) -> dict[str, str]:
        return {
            "plat": "2",
            "ver": self.app_version or "web20",
            "utToken": self.ut_token,
            "ctToken": self.ct_token,
            **{key: str(value) for key, value in params.items()},
        }

    @staticmethod
    def _web_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
        payload = data.get("data") or []
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            for key in (
                "list",
                "List",
                "items",
                "Items",
                "zuheList",
                "ownZuheList",
                "data",
            ):
                rows = payload.get(key)
                if isinstance(rows, list):
                    return [row for row in rows if isinstance(row, dict)]
            if any(key in payload for key in ("zjzh", "zhbs", "zuheName", "zuhename")):
                return [payload]
        raise EastMoneyApiError("Unexpected web account list response: data is not a list")

    @staticmethod
    def _normalize_account_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "account_id": str(row.get("zhbs") or row.get("zjzh") or row.get("zh") or ""),
            "name": str(
                row.get("zuhename")
                or row.get("zuheName")
                or row.get("zhName")
                or row.get("name")
                or ""
            ),
            "user_id": str(row.get("uid") or row.get("userid") or row.get("userId") or ""),
            "is_owned": str(row.get("isowned") or row.get("isOwned") or "") == "1",
            "market": str(row.get("market")) if row.get("market") is not None else None,
            "net_value": str(row.get("jz")) if row.get("jz") is not None else None,
            "day_return": str(row.get("riserate") or row.get("syl_dr") or "")
            if row.get("riserate") is not None or row.get("syl_dr") is not None
            else None,
            "total_return": str(row.get("ratetotal") or row.get("zsyl") or "")
            if row.get("ratetotal") is not None or row.get("zsyl") is not None
            else None,
            "position_ratio": str(row.get("holdPos"))
            if row.get("holdPos") is not None
            else None,
            "permit": str(row.get("permit")) if row.get("permit") is not None else None,
            "raw": row,
        }

    @staticmethod
    def _raise_list_error_if_needed(data: dict[str, Any]) -> None:
        code = data.get("code")
        message = str(data.get("message") or data.get("msg") or "")
        if code in (0, "0", None):
            return
        if _has_auth_error(message):
            raise EastMoneyAuthError(message or f"EastMoney account list auth error: code={code}")
        raise EastMoneyApiError(message or f"EastMoney account list error: code={code}")

    def __str__(self) -> str:
        return (
            f"EMAccount({self.account_id}, "
            f"available_cash={self.available_cash:,.2f}, "
            f"market_value={self.market_value:,.2f}, "
            f"net_asset_value={self.net_asset_value:,.2f})"
        )

    def __repr__(self) -> str:
        return self.__str__()
