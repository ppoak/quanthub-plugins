"""EastMoney parsing and normalization helpers."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .constants import AUTH_ERROR_PATTERNS, SECRET_PARAM_KEYS, SIM_OPER_URL, TRADE_URL
from .errors import EastMoneyApiError, EastMoneyAuthError
from .models import EastMoneyOrder


def load_dotenv(path: str | Path | None = ".env", *, override: bool = False) -> None:
    if path is None:
        return
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if override or key not in os.environ:
            os.environ[key] = value


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    return float(value) if value else default


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


def mask_secret(value: str) -> str:
    if len(value) <= 12:
        return value[:2] + "***"
    return value[:6] + "..." + value[-6:]


def normalize_code(code: str) -> str:
    raw = str(code).strip().upper()
    if "." in raw:
        ticker, _exchange = raw.split(".", 1)
        return ticker
    if raw.startswith(("SH", "SZ", "BJ")):
        return raw[2:]
    return raw


def infer_market(code: str) -> str:
    ticker = normalize_code(code)
    if ticker.startswith(("5", "6", "9")):
        return "1"
    if ticker.startswith(("0", "1", "2", "3")):
        return "0"
    if ticker.startswith(("4", "8")):
        return "2"
    raise ValueError(f"Cannot infer EastMoney market from code: {code}")


def side_to_mmbz(side: str) -> str:
    normalized = str(side).strip().lower()
    if normalized in ("buy", "b", "0"):
        return "0"
    if normalized in ("sell", "s", "1"):
        return "1"
    raise ValueError(f"Unsupported side: {side}")


def side_to_mmfx(side: str) -> str:
    """Return the web trading direction code."""
    normalized = str(side).strip().lower()
    if normalized in ("buy", "b", "0"):
        return "1"
    if normalized in ("sell", "s", "1"):
        return "2"
    raise ValueError(f"Unsupported side: {side}")


def _has_auth_error(message: str) -> bool:
    lowered = message.lower()
    return any(pattern in lowered for pattern in AUTH_ERROR_PATTERNS)


def _find_first_key(data: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(data, dict):
        for key in keys:
            if key in data and data[key] not in (None, ""):
                return data[key]
        for value in data.values():
            found = _find_first_key(value, keys)
            if found not in (None, ""):
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_first_key(item, keys)
            if found not in (None, ""):
                return found
    return None


def _to_float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(str(value).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return default


def _extract_rows(data: dict[str, Any], *, label: str) -> list[dict[str, Any]]:
    rows = (
        data.get("listData")
        or data.get("ListData")
        or data.get("data")
        or data.get("Data")
        or []
    )
    if isinstance(rows, dict):
        rows = (
            rows.get("list")
            or rows.get("List")
            or rows.get("items")
            or rows.get("Items")
            or []
        )
    if not isinstance(rows, list):
        raise EastMoneyApiError(f"Unexpected {label} response: payload is not a list")
    return [row for row in rows if isinstance(row, dict)]


def _format_yyyymmdd(value: str | date | datetime | pd.Timestamp) -> str:
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y%m%d")
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return text
    parsed = pd.to_datetime(text)
    return parsed.strftime("%Y%m%d")


def _user_id_from_pi(pi: str | None) -> str | None:
    if not pi:
        return None
    prefix = ""
    for char in pi:
        if not char.isdigit():
            break
        prefix += char
    if len(prefix) >= 16:
        return prefix[:16]
    return prefix or None


class EastMoneyTransport:
    """Authentication and HTTP helpers shared by EastMoney layers."""

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
        self.user_id = user_id
        self.ut_token = ut_token
        self.ct_token = ct_token
        self.pi = pi
        self.user_agent = user_agent
        self.app_version = app_version
        self.platform = platform
        self.timeout = timeout
        self.retries = retries
        self.retry_sleep = retry_sleep
        self.page_size = int(page_size)
        self.pending_order_page_size = int(pending_order_page_size)
        self.holdings_page_size = int(holdings_page_size)
        if proxy:
            self.opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
        else:
            self.opener = urllib.request.build_opener()

    @classmethod
    def env_kwargs(cls, dotenv_path: str | Path | None = ".env") -> dict[str, Any]:
        load_dotenv(dotenv_path)
        pi = os.environ.get("EASTMONEY_MONI_PI") or None
        return {
            "user_id": os.environ.get("EASTMONEY_MONI_USER_ID", "")
            or _user_id_from_pi(pi)
            or "",
            "ut_token": os.environ.get("EASTMONEY_MONI_UT_TOKEN", ""),
            "ct_token": os.environ.get("EASTMONEY_MONI_CT_TOKEN", ""),
            "pi": pi,
            "user_agent": os.environ.get("EASTMONEY_MONI_USER_AGENT", ""),
            "app_version": os.environ.get("EASTMONEY_MONI_APP_VERSION", ""),
            "platform": os.environ.get("EASTMONEY_MONI_PLATFORM", ""),
            "proxy": os.environ.get("EASTMONEY_MONI_PROXY") or None,
            "timeout": _env_float("EASTMONEY_MONI_HTTP_TIMEOUT_SECONDS", 20.0),
            "retries": _env_int("EASTMONEY_MONI_HTTP_RETRIES", 1),
            "retry_sleep": _env_float("EASTMONEY_MONI_HTTP_RETRY_SLEEP_SECONDS", 1.0),
            "page_size": _env_int("EASTMONEY_MONI_PAGE_SIZE", 20),
            "pending_order_page_size": _env_int(
                "EASTMONEY_MONI_PENDING_ORDER_PAGE_SIZE", 200
            ),
            "holdings_page_size": _env_int("EASTMONEY_MONI_HOLDINGS_PAGE_SIZE", 200),
        }

    def request_trade(self, params: dict[str, Any]) -> dict[str, Any]:
        data = self.request_json(TRADE_URL, {**self.auth_params(), **params})
        self.raise_trade_error_if_needed(data)
        return data

    def request_web_oper(self, params: dict[str, Any]) -> dict[str, Any]:
        """Request the EastMoney web simulated-trading endpoint."""
        self.require_identity()
        data = self.request_json(
            SIM_OPER_URL, {**self.web_oper_auth_params(), **params}
        )
        self.raise_trade_error_if_needed(data)
        return data

    def request_json(
        self,
        url: str,
        params: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        request_url = f"{url}?{urllib.parse.urlencode(params)}"
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                request_headers = {**self.headers(), **(headers or {})}
                request = urllib.request.Request(request_url, headers=request_headers)
                with self.opener.open(request, timeout=self.timeout) as response:
                    raw = response.read()
                    if response.headers.get("content-encoding") == "gzip":
                        import gzip

                        raw = gzip.decompress(raw)
                data = json.loads(raw.decode("utf-8", "replace"))
                if not isinstance(data, dict):
                    raise EastMoneyApiError("Unexpected JSON response type")
                return data
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise EastMoneyAuthError(f"HTTP {exc.code}") from exc
                last_error = exc
            except (
                TimeoutError,
                urllib.error.URLError,
                json.JSONDecodeError,
                EastMoneyApiError,
            ) as exc:
                last_error = exc
            if attempt < self.retries:
                time.sleep(self.retry_sleep * (attempt + 1))
        raise EastMoneyApiError(
            f"GET {url} failed after retries: {last_error}"
        ) from last_error

    def headers(self) -> dict[str, str]:
        headers = {
            "Accept-Encoding": "gzip",
            "Accept-Language": "zh-CN,zh-Hans;q=0.9",
        }
        if self.user_agent:
            headers["User-Agent"] = self.user_agent
        return headers

    def auth_params(self) -> dict[str, str]:
        if not self.ut_token or not self.ct_token:
            raise ValueError(
                "ut_token and ct_token are required for trade/detail endpoints"
            )
        return {
            "pi": self.pi or "",
            "appVer": self.app_version or "",
            "utToken": self.ut_token,
            "ctToken": self.ct_token,
        }

    def web_oper_auth_params(self) -> dict[str, str]:
        if not self.user_id or not self.ut_token or not self.ct_token:
            raise ValueError(
                "user_id, ut_token, and ct_token are required for web trade endpoints"
            )
        return {
            "userid": self.user_id,
            "ctToken": self.ct_token,
            "utToken": self.ut_token,
            "plat": self.platform or "2",
            "ver": self.app_version or "web20",
        }

    def require_identity(self) -> None:
        if not self.user_id:
            raise ValueError("user_id is required")
        if not self.ut_token:
            raise ValueError("ut_token is required")
        if not self.ct_token:
            raise ValueError("ct_token is required")

    def raise_trade_error_if_needed(self, data: dict[str, Any]) -> None:
        result = data.get("result")
        message = str(data.get("message") or data.get("msg") or "")
        if result in (0, "0", None):
            return
        if _has_auth_error(message):
            raise EastMoneyAuthError(
                message or f"EastMoney trade API auth error: result={result}"
            )
        raise EastMoneyApiError(
            message or f"EastMoney trade API error: result={result}"
        )

    def sanitize_response(self, response: dict[str, Any]) -> dict[str, Any]:
        sanitized = dict(response)
        if isinstance(sanitized.get("params"), dict):
            sanitized["params"] = self._sanitize_params(sanitized["params"])
        return sanitized

    def format_price(self, price: float | str) -> str:
        return f"{float(price):.6f}"

    def format_volume(self, volume: int | float) -> str:
        parsed = int(volume)
        if parsed <= 0:
            raise ValueError("volume must be positive")
        return str(parsed)

    def _sanitize_params(self, params: dict[str, Any]) -> dict[str, Any]:
        sanitized = dict(params)
        for key in SECRET_PARAM_KEYS:
            if key in sanitized and sanitized[key]:
                sanitized[key] = mask_secret(str(sanitized[key]))
        return sanitized


def parse_remote_side(row: dict[str, Any]) -> str:
    value = (
        str(row.get("mmbz") or row.get("mmflag") or row.get("side") or "")
        .strip()
        .lower()
    )
    if value in ("买入", "buy", "b", "0"):
        return EastMoneyOrder.BUY
    if value in ("卖出", "sell", "s", "1"):
        return EastMoneyOrder.SELL
    raise ValueError(f"Cannot parse remote order side: {value}")


def parse_remote_order_status(row: dict[str, Any]) -> str:
    value = str(row.get("ztflag") or row.get("status") or "").strip()
    filled = _to_float(row.get("cjsl") or row.get("filled"))
    quantity = _to_float(row.get("wtsl") or row.get("quantity"))
    if value in ("0", "未成交"):
        return EastMoneyOrder.SUBMITTED
    if value in ("1", "已成", "已成交", "成交", "全部成交"):
        return EastMoneyOrder.FILLED
    if value in ("2", "部成", "部分成交"):
        return EastMoneyOrder.PARTIAL
    if value in ("3", "已撤", "撤单", "全部撤单"):
        return EastMoneyOrder.CANCELED
    if value in ("4", "部分撤单"):
        return EastMoneyOrder.PARTIAL if filled > 0 else EastMoneyOrder.CANCELED
    if value in ("5", "废单", "已废", "失败", "非法委托"):
        return EastMoneyOrder.MARGIN
    if value in ("已成", "已成交", "成交"):
        return EastMoneyOrder.FILLED
    if value in ("废单", "已废", "失败"):
        return EastMoneyOrder.MARGIN
    if value in ("已撤", "撤单"):
        return EastMoneyOrder.CANCELED
    if value in ("部成", "部分成交") or (filled > 0 and quantity > filled):
        return EastMoneyOrder.PARTIAL
    return EastMoneyOrder.SUBMITTED


def parse_remote_datetime(date_value: Any, time_value: Any) -> pd.Timestamp:
    date_text = str(date_value or "").replace("/", "-").strip()
    time_text = str(time_value or "00:00:00").strip()
    if len(time_text) == 6 and time_text.isdigit():
        time_text = f"{time_text[:2]}:{time_text[2:4]}:{time_text[4:]}"
    return pd.to_datetime(f"{date_text} {time_text}", errors="coerce")


def order_row_to_model(account_id: str, row: dict[str, Any]) -> EastMoneyOrder:
    remote_order_id = str(row.get("wth") or row.get("orderId") or "")
    return EastMoneyOrder(
        order_id=remote_order_id or EastMoneyOrder._auto_id("eastmoney-remote"),
        account_id=str(account_id),
        created_at=parse_remote_datetime(
            row.get("wtrq") or row.get("date"),
            row.get("wtsj") or row.get("time"),
        ),
        side=parse_remote_side(row),
        code=normalize_code(
            str(
                row.get("__code")
                or row.get("code")
                or row.get("stockCode")
                or row.get("stkCode")
                or ""
            )
        ),
        quantity=int(
            _to_float(row.get("wtsl") or row.get("quantity") or row.get("vol"))
        ),
        execution_type=EastMoneyOrder.LIMIT,
        limit_price=_to_float(row.get("wtjg") or row.get("price")),
        status=parse_remote_order_status(row),
        filled=_to_float(row.get("cjsl") or row.get("filled")),
        meta={
            "eastmoney_order_id": remote_order_id,
            "eastmoney_remote": row,
            "eastmoney_can_cancel": str(row.get("canCancel") or "") == "1",
        },
    )


def execution_row_to_order(
    account_id: str, row: dict[str, Any]
) -> EastMoneyOrder | None:
    code = normalize_code(
        str(
            row.get("__code")
            or row.get("code")
            or row.get("stockCode")
            or row.get("stkCode")
            or ""
        )
    )
    quantity = _to_float(
        row.get("cjsl") or row.get("quantity") or row.get("shares") or row.get("vol")
    )
    price = _to_float(row.get("cjjg") or row.get("price") or row.get("dealPrice"))
    if not code or quantity <= 0 or price <= 0:
        return None
    try:
        side = parse_remote_side(row)
    except ValueError:
        return None
    created_at = parse_remote_datetime(
        row.get("cjrq") or row.get("date") or row.get("dealDate"),
        row.get("cjsj") or row.get("time") or row.get("dealTime"),
    )
    if pd.isna(created_at):
        return None
    deal_id = str(row.get("cjlsh") or row.get("dealId") or "")
    order_id = str(row.get("wth") or row.get("orderId") or "")
    fee_meta = execution_fee_meta(row)
    # Older responses omit the deal ID; use the immutable fill fields then.
    fill_key = deal_id or "/".join(
        (order_id, created_at.isoformat(), code, side, str(quantity), str(price))
    )
    return EastMoneyOrder(
        order_id=deal_id or order_id or EastMoneyOrder._auto_id("eastmoney-deal"),
        execution_id=f"{account_id}/{fill_key}",
        account_id=str(account_id),
        created_at=created_at,
        side=side,
        code=code,
        quantity=quantity,
        execution_type=EastMoneyOrder.LIMIT,
        limit_price=price,
        status=EastMoneyOrder.FILLED,
        filled=quantity,
        meta={
            "eastmoney_deal_id": deal_id,
            "eastmoney_order_id": order_id,
            **fee_meta,
            "eastmoney_remote": row,
        },
    )


def execution_fee_meta(row: dict[str, Any]) -> dict[str, float]:
    """Normalize descriptive fee components from an EastMoney execution row."""
    broker_commission = (
        _to_float(row.get("qsyj"))
        or _to_float(row.get("sxf"))
        or _to_float(row.get("commission"))
        or _to_float(row.get("fee"))
    )
    transfer_fee = _to_float(row.get("ghf") or row.get("transferFee"))
    stamp_tax = _to_float(row.get("yhs") or row.get("stampTax"))
    return {
        "eastmoney_broker_commission": broker_commission,
        "eastmoney_transfer_fee": transfer_fee,
        "eastmoney_stamp_tax": stamp_tax,
    }
