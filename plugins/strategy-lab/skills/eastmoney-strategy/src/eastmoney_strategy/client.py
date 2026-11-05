"""EastMoney simulated trading HTTP client."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pandas as pd

from base_strategy.client import BaseClient
from .constants import (
    MYFAVOR_APPKEY,
    MYFAVOR_URL,
    ORDER_ID_KEYS,
    QUOTE_FIELDS,
    QUOTE_URL,
    SIM_OPER_URL,
)
from .errors import EastMoneyApiError, EastMoneyAuthError
from .models import (
    EastMoneyOrder,
    EastMoneyQuote,
)
from .utils import (
    EastMoneyTransport,
    _find_first_key,
    _to_float,
    infer_market,
    normalize_code,
    side_to_mmfx,
)


class EMClient(BaseClient, EastMoneyTransport):
    """EastMoney strategy-facing client and HTTP transport.

    ``EMClient`` is the strategy trading interface for the EastMoney backend.
    It owns authentication and HTTP transport, accepts strategy order intents,
    and delegates remote account/order/execution coordination to ``EMExchange``.
    EastMoney remains the matching and accounting authority; local state is only
    a server-reported mirror held by ``EMAccount`` and ``EMLedger``.

    Credentials and connection settings can be supplied explicitly or loaded
    from ``EASTMONEY_MONI_*`` environment variables with ``from_env()``.
    """

    _SEQ: dict[str, int] = {}

    def __init__(
        self,
        *,
        client_id: str | None = None,
        user_id: str,
        ut_token: str,
        ct_token: str,
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
        live: bool = False,
    ) -> None:
        """Create an EastMoney client from explicit credentials.

        Parameters such as ``ut_token``, ``ct_token``, ``pi``, ``user_agent``,
        ``app_version``, and ``platform`` come from EastMoney simulated-trading
        web or app requests. If calls fail with authentication or empty-data
        errors, refresh these credentials from a logged-in session first.
        """
        self.client_id = str(client_id or self._auto_id())
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
        self._exchange = None
        self._account = None
        self.account_id: str | None = None
        self.live = bool(live)

    @classmethod
    def _auto_id(cls) -> str:
        cls._SEQ[cls.__name__] = cls._SEQ.get(cls.__name__, 0) + 1
        return f"{cls.__name__}#{cls._SEQ[cls.__name__]}"

    def connect(self, exchange, *, account_id: str) -> dict[str, Any]:
        """Connect this client to one registered EastMoney exchange account."""
        if not account_id:
            raise ValueError("account_id is required")
        account = exchange.account(str(account_id), refresh=False)
        self._exchange = exchange
        self._account = account
        self.account_id = account.account_id
        return {
            "account_id": self.account_id,
            "live": self.live,
        }

    @property
    def exchange(self):
        return self._require_exchange()

    @property
    def account(self):
        if self._account is None:
            raise ValueError("EMClient is not connected to an account.")
        return self._account

    def buy(
        self,
        code: str,
        quantity: int | float,
        price: float | str | None = None,
        *,
        limit: float | str | None = None,
        market: str | None = None,
        quote_level: int = 1,
        meta: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> EastMoneyOrder:
        """Submit an EastMoney buy order through the connected exchange."""
        return self.submit(
            self._make_order(
                side=EastMoneyOrder.BUY,
                code=code,
                quantity=quantity,
                price=self._resolve_price(
                    code,
                    price,
                    limit,
                    side=EastMoneyOrder.BUY,
                    quote_level=quote_level,
                    market=market,
                ),
                market=market,
                meta=meta,
                **kwargs,
            )
        )

    def sell(
        self,
        code: str,
        quantity: int | float,
        price: float | str | None = None,
        *,
        limit: float | str | None = None,
        market: str | None = None,
        quote_level: int = 1,
        meta: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> EastMoneyOrder:
        """Submit an EastMoney sell order through the connected exchange."""
        return self.submit(
            self._make_order(
                side=EastMoneyOrder.SELL,
                code=code,
                quantity=quantity,
                price=self._resolve_price(
                    code,
                    price,
                    limit,
                    side=EastMoneyOrder.SELL,
                    quote_level=quote_level,
                    market=market,
                ),
                market=market,
                meta=meta,
                **kwargs,
            )
        )

    def cancel(
        self,
        order_or_id: EastMoneyOrder | str,
        *,
        price: float | str | None = None,
    ) -> EastMoneyOrder:
        """Cancel a local or remote EastMoney order mirror."""
        order = (
            self.exchange.resolve_order(order_or_id, account_id=self._account_id())
            if isinstance(order_or_id, str)
            else order_or_id
        )
        remote_order_id = order.meta.get("eastmoney_order_id")
        if remote_order_id:
            params = self._build_cancel_params(
                order_id=str(remote_order_id),
                code=order.code,
                side=order.side,
                portfolio_id=order.account_id,
                market=order.meta.get("market"),
            )
            if self.live:
                response = self.request_web_oper(params)
            else:
                response = {
                    "dry_run": True,
                    "url": SIM_OPER_URL,
                    "params": self._sanitize_params(params),
                }
            order.meta["eastmoney_cancel"] = self.sanitize_response(response)
        order.cancel()
        return self.exchange.record_order(order)

    def close(self, code: str, **kwargs: Any) -> EastMoneyOrder:
        """Create and submit a sell order for the current holding quantity."""
        self.exchange.account(self._account_id())
        position = self.account.position(code)
        return self.sell(code, quantity=position.quantity, **kwargs)

    def order_target_value(
        self,
        code: str,
        target_value: float,
        price: float | None = None,
        *,
        market: str | None = None,
        meta: dict[str, Any] | None = None,
        lot_size: int = 100,
        quote_level: int = 1,
        min_order_value: float = 0.0,
        sync: bool = True,
        **kwargs: Any,
    ) -> EastMoneyOrder | None:
        """Submit an order toward a target server-reported position value."""
        normalized = normalize_code(code)
        if sync:
            self.exchange.account(self._account_id())
        if price is None:
            quote = self.quote(normalized, market=market)
            value_price = quote.latest
            buy_price = quote.order_price(EastMoneyOrder.BUY, level=quote_level)
            sell_price = quote.order_price(EastMoneyOrder.SELL, level=quote_level)
        else:
            value_price = buy_price = sell_price = float(price)
        position = self.account.positions.get(normalized)
        current_value = position.market_value if position is not None else 0.0
        if current_value <= 0 and position is not None and value_price > 0:
            current_value = position.quantity * value_price
        delta_value = float(target_value) - current_value
        if abs(delta_value) < float(min_order_value):
            return None
        if delta_value > 0:
            trade_price = buy_price
            side = EastMoneyOrder.BUY
        else:
            trade_price = sell_price
            side = EastMoneyOrder.SELL
        if trade_price <= 0:
            return None
        if float(target_value) <= 0 and side == EastMoneyOrder.SELL and position is not None:
            quantity = int(position.quantity)
        else:
            quantity = self.round_lot_down(abs(delta_value) / trade_price, lot_size)
        if quantity <= 0:
            return None
        if side == EastMoneyOrder.BUY:
            return self.buy(normalized, quantity, price=buy_price, market=market, meta=meta, **kwargs)
        return self.sell(normalized, quantity, price=sell_price, market=market, meta=meta, **kwargs)

    def order_target_percent(
        self,
        code: str,
        percent: float,
        price: float | None = None,
        *,
        market: str | None = None,
        meta: dict[str, Any] | None = None,
        lot_size: int = 100,
        quote_level: int = 1,
        min_order_value: float = 0.0,
        sync: bool = True,
        **kwargs: Any,
    ) -> EastMoneyOrder | None:
        """Submit an order toward a target percentage of remote NAV."""
        if sync:
            self.exchange.account(self._account_id())
        return self.order_target_value(
            code=code,
            target_value=self.account.net_asset_value * float(percent),
            price=price,
            market=market,
            meta=meta,
            lot_size=lot_size,
            quote_level=quote_level,
            min_order_value=min_order_value,
            sync=False,
            **kwargs,
        )

    def submit(self, order: EastMoneyOrder) -> EastMoneyOrder:
        """Send one normalized order to EastMoney and register its response."""
        if self.live:
            blocked = self._precheck_order(order)
            if blocked is not None:
                return blocked
        params = self._build_submit_params(
            code=order.code,
            side=order.side,
            price=order.limit_price,
            volume=int(order.quantity),
            portfolio_id=order.account_id,
            market=order.meta.get("market"),
        )
        if self.live:
            response = self.request_web_oper(params)
        else:
            response = {
                "dry_run": True,
                "url": SIM_OPER_URL,
                "params": self._sanitize_params(params),
            }
        order.meta["eastmoney"] = self.sanitize_response(response)
        remote_order_id = _find_first_key(response, ORDER_ID_KEYS)
        if remote_order_id not in (None, ""):
            order.meta["eastmoney_order_id"] = str(remote_order_id)
        if response.get("dry_run"):
            order.status = EastMoneyOrder.CREATED
        return order

    def _precheck_order(self, order: EastMoneyOrder) -> EastMoneyOrder | None:
        price = order.limit_price
        if price is None:
            raise ValueError("limit_price is required for EastMoney order precheck")
        market = order.meta.get("market")
        quantity = int(order.quantity)
        if order.side == EastMoneyOrder.BUY:
            available = self.max_buy_quantity(order.code, price, market=market)
            limit_name = "max_buy_quantity"
        elif order.side == EastMoneyOrder.SELL:
            available = self.max_sell_quantity(order.code, price, market=market)
            limit_name = "max_sell_quantity"
        else:
            raise ValueError(f"Unsupported side: {order.side}")
        order.meta["eastmoney_precheck"] = {
            "limit": limit_name,
            "available": available,
            "required": quantity,
        }
        if available >= quantity:
            return None
        order.status = EastMoneyOrder.MARGIN
        order.meta["eastmoney_precheck"]["blocked"] = True
        order.meta["eastmoney_precheck"]["reason"] = (
            f"{limit_name} {available} is below order quantity {quantity}"
        )
        return self.exchange.record_order(order)

    def _make_order(
        self,
        *,
        side: str,
        code: str,
        quantity: int | float,
        price: float | str,
        market: str | None = None,
        meta: dict[str, Any] | None = None,
        user_id: str | None = None,
        **kwargs: Any,
    ) -> EastMoneyOrder:
        volume = int(quantity)
        if volume <= 0:
            raise ValueError("quantity must be positive")
        order = EastMoneyOrder(
            order_id=EastMoneyOrder._auto_id(user_id),
            account_id=self._account_id(),
            client_id=self.client_id,
            created_at=pd.Timestamp.now(),
            side=side,
            code=normalize_code(code),
            quantity=volume,
            execution_type=EastMoneyOrder.LIMIT,
            limit_price=float(price),
            status=EastMoneyOrder.SUBMITTED,
            meta=dict(meta or {}),
        )
        order.meta["market"] = market
        order.meta.update(kwargs)
        return order

    def quote(self, code: str, *, market: str | None = None) -> EastMoneyQuote:
        data = self.request_json(QUOTE_URL, self._build_quote_params(code, market=market))
        payload = data.get("data") or {}
        if not isinstance(payload, dict):
            raise EastMoneyApiError("Unexpected quote response: data is not an object")
        return EastMoneyQuote(
            code=str(payload.get("f57") or normalize_code(code)),
            name=str(payload.get("f58") or ""),
            latest=_to_float(payload.get("f43")),
            bids=self._quote_book(
                payload,
                (("f19", "f20"), ("f17", "f18"), ("f15", "f16"), ("f13", "f14"), ("f11", "f12")),
            ),
            asks=self._quote_book(
                payload,
                (("f39", "f40"), ("f37", "f38"), ("f35", "f36"), ("f33", "f34"), ("f31", "f32")),
            ),
            raw=payload,
        )

    def list_watchlist_groups(self) -> list[dict[str, Any]]:
        """Return EastMoney watchlist groups visible to the authenticated user."""
        data = self._request_watchlist("ggdefstkindexinfos")
        payload = data.get("data") or {}
        rows = payload.get("ginfolist") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise EastMoneyApiError("Unexpected watchlist group response: ginfolist is not a list")
        web_index = self._parse_watchlist_web_index(payload.get("webindex"))
        groups = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            group_id = str(row.get("gid") or "")
            groups.append(
                {
                    "group_id": group_id,
                    "name": str(row.get("gname") or ""),
                    "web_index": web_index.get(group_id),
                    "raw": row,
                }
            )
        return groups

    def list_watchlist_stocks(self, group_id: str | int) -> list[dict[str, Any]]:
        """Return stocks in one EastMoney watchlist group."""
        data = self._request_watchlist("gstkinfos", {"g": str(group_id)})
        payload = data.get("data") or {}
        rows = payload.get("stkinfolist") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise EastMoneyApiError("Unexpected watchlist stock response: stkinfolist is not a list")
        return [
            self._normalize_watchlist_stock(row)
            for row in rows
            if isinstance(row, dict)
        ]

    def add_watchlist_stock(
        self,
        code: str,
        *,
        group_id: str | int,
        market: str | None = None,
    ) -> dict[str, Any]:
        """Add one stock to a specific EastMoney watchlist group."""
        return self._request_watchlist(
            "as",
            {
                "g": str(group_id),
                "sc": self._watchlist_security_code(code, market=market),
            },
        )

    def delete_watchlist_stock(
        self,
        code: str,
        *,
        group_id: str | int,
        market: str | None = None,
    ) -> dict[str, Any]:
        """Delete one stock from a specific EastMoney watchlist group."""
        return self._request_watchlist(
            "ds",
            {
                "g": str(group_id),
                "sc": self._watchlist_security_code(code, market=market),
            },
        )

    def _quote_price(
        self,
        code: str,
        *,
        side: str | None = None,
        level: int = 1,
        market: str | None = None,
    ) -> float:
        quote = self.quote(code, market=market)
        price = quote.order_price(side or "", level=level) if side else quote.latest
        if price <= 0:
            raise EastMoneyApiError(f"Quote price is not available for code: {code}")
        return price

    def round_lot_down(self, quantity: int | float, lot_size: int = 100) -> int:
        """Round quantity down to the requested lot size."""
        parsed = int(quantity)
        if lot_size <= 1:
            return max(parsed, 0)
        return max(parsed // int(lot_size) * int(lot_size), 0)

    def _resolve_price(
        self,
        code: str,
        price: float | str | None,
        limit: float | str | None,
        *,
        side: str | None = None,
        quote_level: int = 1,
        market: str | None = None,
    ) -> float | str:
        resolved = price if price is not None else limit
        if resolved is not None:
            return resolved
        return self._quote_price(code, side=side, level=quote_level, market=market)

    def _request_watchlist(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.require_identity()
        data = self.request_json(
            f"{MYFAVOR_URL}/{endpoint}",
            {
                "appkey": MYFAVOR_APPKEY,
                **{key: str(value) for key, value in (params or {}).items()},
            },
            headers=self._watchlist_headers(),
        )
        self._raise_watchlist_error_if_needed(data)
        return data

    def _watchlist_headers(self) -> dict[str, str]:
        cookies = {
            "ut": self.ut_token,
            "ct": self.ct_token,
        }
        if self.pi:
            cookies["pi"] = self.pi
        if self.user_id:
            cookies["uidal"] = self.user_id
        headers = {
            "Accept": "application/json,text/javascript,*/*;q=0.01",
            "Referer": "https://quote.eastmoney.com/zixuan/",
            "Cookie": "; ".join(f"{key}={value}" for key, value in cookies.items() if value),
        }
        if not self.user_agent:
            headers["User-Agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
            )
        return headers

    @staticmethod
    def _raise_watchlist_error_if_needed(data: dict[str, Any]) -> None:
        state = data.get("state")
        message = str(data.get("message") or data.get("msg") or "")
        if state in (0, "0", None):
            return
        lowered = message.lower()
        if state in (-2, "-2", -3, "-3") or "token" in lowered or "登录" in message:
            raise EastMoneyAuthError(message or f"EastMoney watchlist auth error: state={state}")
        raise EastMoneyApiError(message or f"EastMoney watchlist API error: state={state}")

    @staticmethod
    def _parse_watchlist_web_index(value: Any) -> dict[str, Any]:
        if not value:
            return {}
        if isinstance(value, str):
            import json

            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return {}
        if not isinstance(value, dict):
            return {}
        custom_index = value.get("custom_index")
        return custom_index if isinstance(custom_index, dict) else {}

    def _normalize_watchlist_stock(self, row: dict[str, Any]) -> dict[str, Any]:
        security = str(row.get("security") or row.get("sc") or row.get("code") or "")
        market, code = self._parse_watchlist_security(security)
        update_time = str(row.get("updatetime") or "")
        return {
            "code": code,
            "market": market,
            "quote_code": f"{market}.{code}" if market and code else "",
            "security": security,
            "name": str(row.get("name") or row.get("securityname") or row.get("stockName") or ""),
            "is_top": bool(row.get("star")),
            "update_time": update_time,
            "update_date": update_time[:8] if len(update_time) >= 8 else "",
            "price": _to_float(row.get("price")),
            "note": row.get("note") if isinstance(row.get("note"), dict) else None,
            "raw": row,
        }

    def _watchlist_security_code(self, code: str, *, market: str | None = None) -> str:
        raw = str(code).strip().upper()
        if "$" in raw:
            return raw
        if "." in raw:
            prefix, ticker = raw.split(".", 1)
            if prefix in {"0", "1", "2"}:
                return f"{prefix}${normalize_code(ticker)}"
        ticker = normalize_code(raw)
        return f"{market or infer_market(ticker)}${ticker}"

    def _parse_watchlist_security(self, security: str) -> tuple[str, str]:
        raw = str(security).strip().upper()
        marker = raw.replace(".", "$")
        parts = marker.split("$")
        if len(parts) >= 2 and parts[0].isdigit():
            return parts[0], normalize_code(parts[1])
        code = normalize_code(raw)
        if not code:
            return "", ""
        return infer_market(code), code

    def _require_exchange(self):
        if self._exchange is None:
            raise ValueError("EMClient is not connected to an exchange.")
        return self._exchange

    def _account_id(self) -> str:
        if self.account_id is None:
            raise ValueError("EMClient is not connected to an account.")
        return self.account_id

    @classmethod
    def from_env(
        cls,
        *,
        dotenv_path: str | Path | None = ".env",
        live: bool = False,
    ) -> "EMClient":
        """Build a client from ``.env`` and process environment variables."""
        return cls(**cls.env_kwargs(dotenv_path), live=live)

    @classmethod
    def login(
        cls,
        *,
        qr_path: str | Path | None = None,
        dotenv_path: str | Path | None = ".env",
        save_env: bool = True,
        timeout: float | None = None,
        poll_interval: float | None = None,
    ):
        """Login with an EastMoney QR code and return extracted credentials."""
        from .login import login_with_qr

        return login_with_qr(
            qr_path=qr_path,
            dotenv_path=dotenv_path,
            save_env=save_env,
            timeout=timeout,
            poll_interval=poll_interval,
        )

    def max_sell_quantity(
        self,
        code: str,
        price: float | str,
        *,
        market: str | None = None,
    ) -> int:
        """Return maximum server-reported sellable volume."""
        return self._order_limit(
            code=code,
            side=EastMoneyOrder.SELL,
            price=price,
            market=market,
        )

    def max_buy_quantity(
        self,
        code: str,
        price: float | str,
        *,
        market: str | None = None,
    ) -> int:
        """Return maximum server-reported buyable volume."""
        return self._order_limit(
            code=code,
            side=EastMoneyOrder.BUY,
            price=price,
            market=market,
        )

    def _order_limit(
        self,
        *,
        code: str,
        side: str,
        price: float | str,
        market: str | None,
    ) -> int:
        ticker = normalize_code(code)
        params = {
            "type": "spo_order_limit",
            "zjzh": self._account_id(),
            "mmfx": side_to_mmfx(side),
            "mktCode": market or infer_market(ticker),
            "stkCode": ticker,
            "price": self.format_price(price),
            "wtsl": "0" if side == EastMoneyOrder.BUY else "-1",
            "asset": "0",
        }
        data = self.request_web_oper(params)
        return int(float(data.get("orderLimit") or 0))

    def _build_submit_params(
        self,
        *,
        code: str,
        side: str,
        price: float | str,
        volume: int,
        portfolio_id: str | None = None,
        market: str | None = None,
    ) -> dict[str, str]:
        zh = self._resolve_portfolio_id(portfolio_id)
        ticker = normalize_code(code)
        return {
            "type": "spo_order",
            "zjzh": zh,
            "mmfx": side_to_mmfx(side),
            "mktCode": market or infer_market(ticker),
            "stkCode": ticker,
            "price": self.format_price(price),
            "wtsl": self.format_volume(volume),
            "r": str(random.randint(1, 100_000_000)),
        }

    def _build_cancel_params(
        self,
        *,
        order_id: str,
        code: str,
        side: str,
        portfolio_id: str | None = None,
        market: str | None = None,
    ) -> dict[str, str]:
        zh = self._resolve_portfolio_id(portfolio_id)
        ticker = normalize_code(code)
        return {
            "type": "spo_cancel",
            "zjzh": zh,
            "stkCode": ticker,
            "mktCode": market or infer_market(ticker),
            "wth": str(order_id),
            "mmfx": side_to_mmfx(side),
        }

    def _build_quote_params(self, code: str, *, market: str | None = None) -> dict[str, str]:
        ticker = normalize_code(code)
        return {
            "fltt": "2",
            "invt": "2",
            "fields": QUOTE_FIELDS,
            "secid": f"{market or infer_market(ticker)}.{ticker}",
        }

    def _quote_book(
        self,
        payload: dict[str, Any],
        field_pairs: tuple[tuple[str, str], ...],
    ) -> tuple[tuple[float, float], ...]:
        return tuple(
            (_to_float(payload.get(price_key)), _to_float(payload.get(volume_key)) * 100)
            for price_key, volume_key in field_pairs
        )

    def _resolve_portfolio_id(self, portfolio_id: str | None) -> str:
        zh = portfolio_id or self.account_id
        if not zh:
            raise ValueError("portfolio_id is required")
        return str(zh)
