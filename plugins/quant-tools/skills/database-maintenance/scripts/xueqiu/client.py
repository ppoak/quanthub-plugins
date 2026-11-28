from __future__ import annotations

import argparse
from base64 import b64encode
import json
import os
import random
import smtplib
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import duckdb

from .accounts import XueqiuAccount, load_accounts, parse_proxy_url


AUTH_ERROR_PATTERNS = ("token", "login", "auth", "unauthorized", "登录", "登陆", "过期", "失效")
DEFAULT_USER_AGENT = "Xueqiu iPhone 14.91"
EMAIL_RECIPIENT_ENV = "DATABASE_MAINTENANCE_EMAIL_TO"
PORTFOLIO_URL = "https://stock.xueqiu.com/v5/stock/portfolio/stock/hasexist.json"
HOT_INFO_URL = "https://stock.xueqiu.com/v5/stock/hot/info.json"
HOT_STOCK_LIST_URL = "https://stock.xueqiu.com/v5/stock/hot_stock/list.json"
STATUS_SEARCH_URL = "https://api.xueqiu.com/query/v1/symbol/search/status.json"
CUBE_TRADE_URL = "https://api.xueqiu.com/cube/center/awkwardness/getTradeData.json"


def smtp_settings() -> tuple[str | None, str | None, str | None, str, int, bool]:
    sender = os.environ.get("SENDER")
    password = os.environ.get("EMAIL_PASSWORD")
    server = os.environ.get("SMTP_SERVER")
    recipient = os.environ.get(EMAIL_RECIPIENT_ENV, "").strip()
    port = int(os.environ.get("SMTP_PORT", "465"))
    use_ssl = os.environ.get("SMTP_USE_SSL", "1").lower() not in {"0", "false", "no"}
    return sender, password, server, recipient, port, use_ssl


def send_email(
    message: EmailMessage,
    *,
    sender: str,
    password: str,
    server: str,
    port: int,
    use_ssl: bool,
) -> None:
    if use_ssl:
        with smtplib.SMTP_SSL(server, port, timeout=20, context=ssl.create_default_context()) as smtp:
            smtp.login(sender, password)
            smtp.send_message(message)
        return
    with smtplib.SMTP(server, port, timeout=20) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(sender, password)
        smtp.send_message(message)


def load_dotenv(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def optional_int_from_float(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def is_auth_error(message: str) -> bool:
    lowered = message.lower()
    return any(pattern in lowered for pattern in AUTH_ERROR_PATTERNS)


def mask_token(token: str) -> str:
    if len(token) <= 12:
        return token[:2] + "***"
    return token[:6] + "..." + token[-6:]


def to_xueqiu_symbol(code: str) -> str:
    raw = code.strip().upper()
    if "." in raw:
        ticker, exchange = raw.split(".", 1)
        return f"{exchange}{ticker}"
    if raw.startswith(("SH", "SZ", "BJ")):
        return raw
    if raw.startswith(("5", "6", "9")):
        return f"SH{raw}"
    if raw.startswith(("0", "1", "2", "3")):
        return f"SZ{raw}"
    if raw.startswith(("4", "8")):
        return f"BJ{raw}"
    raise ValueError(f"Cannot infer Xueqiu symbol from code: {code}")


def normalize_code(code: str) -> str:
    raw = code.strip().upper()
    if "." in raw:
        ticker, exchange = raw.split(".", 1)
        return f"{ticker}.{exchange}"
    if raw.startswith(("SH", "SZ", "BJ")):
        return f"{raw[2:]}.{raw[:2]}"
    if raw.startswith(("5", "6", "9")):
        return f"{raw}.SH"
    if raw.startswith(("0", "1", "2", "3")):
        return f"{raw}.SZ"
    if raw.startswith(("4", "8")):
        return f"{raw}.BJ"
    raise ValueError(f"Cannot infer normalized code from code: {code}")


def latest_quotes_day_partition(data_root: Path, trade_date: str) -> tuple[str, Path]:
    quotes_root = data_root / "quotes_day"
    if not quotes_root.exists():
        raise ValueError(f"quotes_day table does not exist: {quotes_root.as_posix()}")

    target_date = datetime.fromisoformat(trade_date).date()
    candidates: list[tuple[datetime.date, Path]] = []
    for path in quotes_root.glob("date=*"):
        if not path.is_dir():
            continue
        try:
            partition_date = datetime.fromisoformat(path.name.split("=", 1)[1]).date()
        except ValueError:
            continue
        if partition_date <= target_date and any(path.glob("*.parquet")):
            candidates.append((partition_date, path))

    if not candidates:
        raise ValueError(f"No quotes_day universe available on or before {trade_date}")
    partition_date, partition_path = max(candidates, key=lambda item: item[0])
    return partition_date.isoformat(), partition_path


def load_codes_from_quotes_day(data_root: Path, trade_date: str) -> list[str]:
    universe_date, partition_path = latest_quotes_day_partition(data_root, trade_date)
    quotes_glob = (partition_path / "*.parquet").as_posix()
    con = duckdb.connect(database=":memory:")
    try:
        print(f"universe_date={universe_date}")
        rows = con.execute(
            """
            SELECT DISTINCT code
            FROM parquet_scan(?, union_by_name=1)
            WHERE code IS NOT NULL
            ORDER BY code
            """,
            [quotes_glob],
        ).fetchall()
    finally:
        con.close()
    return [str(row[0]) for row in rows]


class TokenInvalidError(RuntimeError):
    def __init__(self, token: str, reason: str) -> None:
        super().__init__(reason)
        self.token = token
        self.reason = reason


class TokenInvalidNotifier:
    def __init__(self, recipient: str | None = None) -> None:
        self.recipient = recipient
        self.sent_tokens: set[str] = set()
        self.lock = threading.Lock()

    def notify_once(self, token: str, reason: str) -> None:
        masked = mask_token(token)
        with self.lock:
            if token in self.sent_tokens:
                return
            self.sent_tokens.add(token)
        sender, password, server, recipient, port, use_ssl = smtp_settings()
        recipient = self.recipient or recipient
        if not sender or not password or not server or not recipient:
            print(f"token_invalid_email_skipped token={masked} missing_smtp_config=1")
            return
        message = EmailMessage()
        message["From"] = sender
        message["To"] = recipient
        message["Subject"] = f"Xueqiu token invalid: {masked}"
        message.set_content(
            "\n".join(
                [
                    "A Xueqiu token appears to be invalid.",
                    f"Token: {masked}",
                    f"Reason: {reason}",
                    f"Time: {datetime.now(timezone.utc).replace(microsecond=0).isoformat()}",
                ]
            )
        )
        try:
            send_email(message, sender=sender, password=password, server=server, port=port, use_ssl=use_ssl)
        except Exception as exc:  # noqa: BLE001 - notification must not mask the token error.
            print(f"token_invalid_email_failed token={masked} error={exc}")
            return
        print(f"token_invalid_email_sent token={masked} recipient={recipient}")


class ExplicitProxyHandler(urllib.request.ProxyHandler):
    def proxy_open(self, request: urllib.request.Request, proxy: str, protocol: str) -> None:
        # An explicit proxy must not be bypassed by NO_PROXY or system settings.
        url = urllib.parse.urlsplit(proxy)
        if url.username is not None:
            username = urllib.parse.unquote(url.username)
            password = urllib.parse.unquote(url.password or "")
            credentials = b64encode(f"{username}:{password}".encode()).decode("ascii")
            request.add_unredirected_header("Proxy-Authorization", f"Basic {credentials}")
        request.set_proxy(url.netloc.rsplit("@", 1)[-1], url.scheme)


def build_xueqiu_opener(proxy: str | None, *handlers) -> urllib.request.OpenerDirector:
    if proxy:
        parse_proxy_url(proxy, "Xueqiu proxy")
    proxy_handler = (
        ExplicitProxyHandler({"http": proxy, "https": proxy})
        if proxy else urllib.request.ProxyHandler({})
    )
    return urllib.request.build_opener(proxy_handler, *handlers)


class XueqiuClient:
    def __init__(
        self,
        accounts: list[XueqiuAccount],
        *,
        host: str,
        proxy: str | None = None,
        notifier: TokenInvalidNotifier | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        accept_language: str = "zh-Hans-CN;q=1, en-CN;q=0.9",
        device_id: str | None = "DC27D03A-915A-4630-B430-A0FE52185B7A",
        timeout: float = 20.0,
        retries: int = 3,
        retry_sleep: float = 1.0,
        waf_405_message: str | None = None,
        pinned_account: XueqiuAccount | None = None,
    ) -> None:
        self.accounts = accounts
        if not self.accounts:
            raise ValueError("Xueqiu account token pool is empty")
        self.notifier = notifier
        self.timeout = timeout
        self.retries = retries
        self.retry_sleep = retry_sleep
        self.waf_405_message = waf_405_message
        self.pinned_account = pinned_account
        self.base_headers = {
            "User-Agent": user_agent,
            "Accept": "application/json",
            "Accept-Language": accept_language,
            "host": host,
            "x-device-os": "iOS 26.3.1",
            "x-device-model-name": "iPhone 15 Plus_iPhone15,5",
        }
        if device_id:
            self.base_headers["x-device-id"] = device_id
        self.openers = {
            account.id: build_xueqiu_opener(account.proxy_url or proxy)
            for account in self.accounts
        }

    def get_json(self, url: str, params: dict[str, Any], *, add_nonce: bool = True) -> dict[str, Any]:
        query = dict(params)
        if add_nonce:
            query["_"] = int(time.time() * 1000)
            query["x"] = round(random.random(), 3)
        request_url = f"{url}?{urllib.parse.urlencode(query)}"
        last_error: Exception | None = None
        account = self.pinned_account or random.choice(self.accounts)
        for attempt in range(self.retries + 1):
            try:
                payload = self._open_request(request_url, account)
                data = json.loads(payload)
                self._raise_api_error_if_needed(data, account.token)
                return data
            except urllib.error.HTTPError as exc:
                if exc.code == 405 and self.waf_405_message:
                    raise RuntimeError(self.waf_405_message) from exc
                if exc.code == 401:
                    token = account.token
                    reason = f"HTTP {exc.code}"
                    self._notify_token_invalid(token, reason)
                    raise TokenInvalidError(token, reason) from exc
                last_error = exc
                if attempt >= self.retries:
                    break
                time.sleep(self.retry_sleep * (attempt + 1))
            except (
                TimeoutError,
                urllib.error.URLError,
                json.JSONDecodeError,
                RuntimeError,
            ) as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                time.sleep(self.retry_sleep * (attempt + 1))
        raise RuntimeError(f"GET {url} failed after retries: {last_error}") from last_error

    def _open_request(self, request_url: str, account: XueqiuAccount) -> str:
        headers = dict(self.base_headers)
        token = account.token
        headers["Cookie"] = f"xq_a_token={token};xq-dj-token={token};xq_is_login=1"
        request = urllib.request.Request(request_url, headers=headers)
        with self.openers[account.id].open(request, timeout=self.timeout) as response:
            return response.read().decode("utf-8")

    def _raise_api_error_if_needed(self, data: dict[str, Any], selected_token: str | None) -> None:
        message = ""
        has_error = False
        if data.get("error_code", 0) != 0:
            has_error = True
            message = str(data.get("error_description") or "")
        elif data.get("success") is False or data.get("result_code", 0) not in (0, None):
            has_error = True
            message = str(data.get("message") or data.get("error_description") or "")
        if not has_error:
            return
        if is_auth_error(message):
            token = selected_token or self.accounts[0].token
            self._notify_token_invalid(token, message)
            raise TokenInvalidError(token, message)
        raise RuntimeError(f"Xueqiu API error: {message}")

    def _notify_token_invalid(self, token: str, reason: str) -> None:
        if self.notifier is not None:
            self.notifier.notify_once(token, reason)

    def get_followers(self, xueqiu_symbol: str) -> int | None:
        data = self.get_json(PORTFOLIO_URL, {"symbol": xueqiu_symbol})
        return optional_int(data.get("data", {}).get("count"))

    def get_discussions(self, xueqiu_symbol: str) -> int | None:
        data = self.get_json(HOT_INFO_URL, {"symbol": xueqiu_symbol})
        return optional_int(data.get("data", {}).get("discussions"))

    def get_hot_stock_items(self, *, rank_type: int, size: int = 100) -> list[dict[str, Any]]:
        data = self.get_json(
            HOT_STOCK_LIST_URL,
            {
                "page": 1,
                "size": size,
                "order": "desc",
                "order_by": "value",
                "type": rank_type,
                "include": "true",
            },
        )
        return data.get("data", {}).get("items") or data.get("data", {}).get("list") or []

    def search_status_page(self, symbol: str, page: int, *, count: int = 20) -> dict[str, Any]:
        params = {
            "comment": 0,
            "count": count,
            "filter_retweet_text": 1,
            "hl": 0,
            "page": page,
            "sort": "time",
            "source": "all",
            "symbol": symbol,
        }
        return self.get_json(STATUS_SEARCH_URL, params)

    def get_cube_trade_data(self, xueqiu_symbol: str) -> dict[str, Any]:
        return self.get_json(CUBE_TRADE_URL, {"symbol": xueqiu_symbol})


def add_xueqiu_auth_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--account", help="Use only the named XUEQIU_ACCOUNT_<ID> record.")
    parser.add_argument("--proxy", help="Fallback HTTP proxy URL for accounts without a proxy.")


def create_runtime(
    args: argparse.Namespace,
) -> tuple[list[XueqiuAccount], str | None, TokenInvalidNotifier]:
    accounts = load_accounts()
    selected = getattr(args, "account", None)
    if selected is not None:
        if selected not in accounts:
            raise ValueError(f"Unknown Xueqiu account: {selected}")
        accounts = {selected: accounts[selected]}
    active = [account for account in accounts.values() if account.token]
    if not active:
        raise ValueError("A registered XUEQIU_ACCOUNT_<ID> with a token is required for collection")
    proxy = getattr(args, "proxy", None) or os.environ.get("XUEQIU_PROXY")
    return active, proxy, TokenInvalidNotifier()
