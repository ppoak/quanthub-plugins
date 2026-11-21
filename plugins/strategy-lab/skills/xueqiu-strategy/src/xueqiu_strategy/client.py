"""Xueqiu paper-trading HTTP client."""

from __future__ import annotations

import http.client
import json
import math
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable, Optional

import pandas as pd

from base_strategy.client import BaseClient
from .errors import XueqiuAPIError, XueqiuTransportError
from .models import XueqiuOrder
from .utils import from_xueqiu_symbol, to_xueqiu_symbol


class XueqiuClient(BaseClient):
    """Low-level HTTP client for Xueqiu quote and paper-trading APIs.

    ``XueqiuClient`` owns token authentication, HTTP request construction, raw
    platform endpoint calls, and strategy order intent creation.
    """

    SUCCESS_CODE = "60000"
    NETWORK_CODE = "80000"

    BASE_URL_TRADE = "https://tc.xueqiu.com/tc/snowx/MONI/"
    BASE_URL_QUOTE = "https://stock.xueqiu.com/v5/stock/batch/quote.json"

    def connect(self, exchange, *, account_id: str | None = None) -> dict[str, Any]:
        """Connect strategy order methods to one local Xueqiu exchange."""
        account = exchange.account(account_id)
        self._exchange = exchange
        self.account_id = account.account_id
        return {"account_id": self.account_id, "gid": exchange.gid,
                "sync_transactions": exchange.sync_transactions}

    def buy(
        self,
        code: str,
        quantity: float,
        *,
        price: float | None = None,
        exectype: str = XueqiuOrder.MARKET,
        **kwargs: Any,
    ) -> XueqiuOrder:
        """Create and submit one local buy intent."""
        return self._submit_order_intent(
            side=XueqiuOrder.BUY,
            code=code,
            quantity=quantity,
            price=price,
            exectype=exectype,
            **kwargs,
        )

    def sell(
        self,
        code: str,
        quantity: float,
        *,
        price: float | None = None,
        exectype: str = XueqiuOrder.MARKET,
        **kwargs: Any,
    ) -> XueqiuOrder:
        """Create and submit one local sell intent."""
        return self._submit_order_intent(
            side=XueqiuOrder.SELL,
            code=code,
            quantity=quantity,
            price=price,
            exectype=exectype,
            **kwargs,
        )

    def cancel(self, order_or_id: XueqiuOrder | str, **kwargs: Any) -> XueqiuOrder:
        """Cancel one accepted local order."""
        return self._require_exchange().cancel(order_or_id)

    def _submit_order_intent(
        self,
        *,
        side: str,
        code: str,
        quantity: float,
        price: float | None,
        exectype: str,
        **kwargs: Any,
    ) -> XueqiuOrder:
        exchange = self._require_exchange()
        quantity = float(quantity)
        parsed_quantity = int(quantity) if math.isfinite(quantity) else 0
        order = XueqiuOrder(
            order_id=XueqiuOrder._auto_id(kwargs.pop("user_id", None)),
            created_at=pd.to_datetime(
                kwargs.pop("created_at", kwargs.pop("trade_time", pd.Timestamp.now()))
            ),
            side=side,
            code=from_xueqiu_symbol(to_xueqiu_symbol(code)),
            quantity=parsed_quantity,
            execution_type=exectype,
            limit_price=float(price) if price is not None else None,
            meta=dict(kwargs.pop("meta", {}) or {}),
            account_id=self.account_id,
        )
        if parsed_quantity <= 0 or abs(parsed_quantity - float(quantity)) > 1e-9:
            order.status = XueqiuOrder.REJECTED
            order.meta["xueqiu_error"] = "quantity must be a positive integer"
            exchange.record_order(order)
            return order
        if exectype not in {XueqiuOrder.MARKET, XueqiuOrder.LIMIT} or (
            exectype == XueqiuOrder.LIMIT
            and (price is None or not math.isfinite(float(price)) or float(price) <= 0)
        ):
            order.status = XueqiuOrder.REJECTED
            order.meta["xueqiu_error"] = "order type or limit price is invalid"
            exchange.record_order(order)
            return order
        order.status = XueqiuOrder.SUBMITTED
        return exchange.submit(order)

    def _require_exchange(self):
        exchange = getattr(self, "_exchange", None)
        if exchange is None:
            raise ValueError("XueqiuClient is not connected to an exchange.")
        return exchange

    def __init__(
        self,
        token: str,
        timeout: float = 10.0,
        opener: Optional[Any] = None,
    ) -> None:
        """Create a Xueqiu client.

        Parameters
        ----------
        token:
            ``xq_a_token`` cookie value from an authenticated Xueqiu session.
        timeout:
            HTTP timeout in seconds.
        opener:
            Optional urllib opener for tests or custom proxy handling.

        Raises
        ------
        ValueError
            If ``token`` is empty.
        """
        if not token:
            raise ValueError("Xueqiu token is required")
        self.token = token
        self.timeout = timeout
        self.opener = opener or urllib.request.build_opener()
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }

    def _url(self, path_or_url: str, params: Optional[dict[str, Any]] = None) -> str:
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            url = path_or_url
        else:
            url = urllib.parse.urljoin(self.BASE_URL_TRADE, path_or_url)
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or parsed.username is not None or parsed.port not in {None, 443}
                or not (host == "xueqiu.com" or host.endswith(".xueqiu.com"))):
            raise ValueError("Xueqiu requests require a trusted HTTPS endpoint")
        if params:
            query = urllib.parse.urlencode(params)
            sep = "&" if urllib.parse.urlparse(url).query else "?"
            url = f"{url}{sep}{query}"
        return url

    def request(
        self,
        method: str,
        path_or_url: str,
        params: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Send an HTTP request and return a normalized JSON dict."""
        payload = None
        if data is not None:
            payload = urllib.parse.urlencode(data).encode("utf-8")
        request = urllib.request.Request(
            self._url(path_or_url, params=params),
            data=payload,
            headers=self.headers,
            method=method.upper(),
        )
        request.add_unredirected_header("Cookie", f"xq_a_token={self.token}")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return {
                "result_code": self.NETWORK_CODE,
                "result_data": None,
                "result_msg": f"HTTP {exc.code}: {exc.reason}",
                "http_status": exc.code,
            }
        except urllib.error.URLError as exc:
            return {
                "result_code": self.NETWORK_CODE,
                "result_data": None,
                "result_msg": str(exc.reason),
                "request_not_sent": isinstance(exc.reason, (ConnectionRefusedError, socket.gaierror)),
            }
        except (OSError, http.client.HTTPException, UnicodeError) as exc:
            raise XueqiuTransportError("Xueqiu response could not be read") from exc
        try:
            result = json.loads(body)
        except ValueError as exc:
            raise XueqiuTransportError("Xueqiu did not return valid JSON") from exc
        if not isinstance(result, dict):
            raise XueqiuTransportError("Xueqiu did not return a JSON object")
        return result

    def ensure_success(self, result: dict[str, Any], action: str) -> dict[str, Any]:
        """Raise ``XueqiuAPIError`` unless a platform response succeeded."""
        if result.get("result_code") == self.SUCCESS_CODE:
            return result
        msg = result.get("result_msg") or result.get("msg") or result
        raise XueqiuAPIError(f"Xueqiu {action} failed: {msg}")

    def list_investment_groups(self) -> dict[str, Any]:
        """Return raw investment-group list payload."""
        return self.request("GET", "trans_group/list.json")

    def add_investment_group(self, name: str) -> dict[str, Any]:
        """Create an investment group and return the raw response."""
        return self.request("POST", "trans_group/add.json", data={"name": name})

    def delete_investment_group(self, gid: str) -> dict[str, Any]:
        """Delete an investment group by remote group id."""
        return self.request("POST", "trans_group/delete.json", data={"gid": gid})

    def bank_transfer(
        self,
        gid: str,
        transfer_type: int,
        date: str,
        market: str,
        amount: float,
    ) -> dict[str, Any]:
        """Submit a simulated cash transfer to a Xueqiu investment group."""
        return self.request(
            "POST",
            "bank_transfer/add.json",
            data={
                "gid": gid,
                "type": transfer_type,
                "date": date,
                "market": market,
                "amount": amount,
            },
        )

    def portfolio_performance(self, gid: str) -> dict[str, Any]:
        """Return raw portfolio performance and holding payload."""
        return self.request("GET", "performances.json", params={"gid": gid})

    def add_transaction(
        self,
        gid: str,
        symbol: str,
        shares: int,
        transaction_type: int,
        date: str,
        price: float,
        tax_rate: float = 0.0,
        commission_rate: float = 0.0,
        comment: str = "",
    ) -> dict[str, Any]:
        """Submit a simulated stock transaction to Xueqiu."""
        return self.request(
            "POST",
            "transaction/add.json",
            data={
                "gid": gid,
                "symbol": symbol,
                "shares": shares,
                "type": transaction_type,
                "date": date,
                "price": price,
                "tax_rate": tax_rate,
                "commission_rate": commission_rate,
                "comment": comment,
            },
        )

    def transaction_records(self, gid: str, row: int = 50, *, pos: int | str | None = None) -> dict[str, Any]:
        """Return raw transaction records for an investment group."""
        params = {"gid": gid, "row": row}
        if pos is not None:
            params["pos"] = pos
        return self.request(
            "GET",
            "transaction/list.json",
            params=params,
        )

    def bank_transfer_records(self, gid: str, row: int = 50, *, pos: int | str | None = None) -> dict[str, Any]:
        """Return raw bank-transfer records for an investment group."""
        params = {"gid": gid, "row": row}
        if pos is not None:
            params["pos"] = pos
        return self.request(
            "GET",
            "bank_transfer/query.json",
            params=params,
        )

    def get_market_price(self, symbols: str | Iterable[str]) -> list[dict[str, Any]]:
        """Return quote rows for one or more Xueqiu symbols."""
        if isinstance(symbols, str):
            raw_symbols = [item for item in symbols.split(",") if item]
        else:
            raw_symbols = list(symbols)
        symbol_param = ",".join(to_xueqiu_symbol(symbol) for symbol in raw_symbols)
        result = self.request(
            "GET",
            self.BASE_URL_QUOTE,
            params={"symbol": symbol_param, "extend": "detail"},
        )
        if result.get("error_code", 1) == 0:
            return result.get("data", {}).get("items", [])
        raise XueqiuAPIError(f"Xueqiu quote failed: {result}")
