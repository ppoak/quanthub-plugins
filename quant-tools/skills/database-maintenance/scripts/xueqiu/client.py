from __future__ import annotations

import argparse
import json
import os
import random
import smtplib
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


AUTH_ERROR_PATTERNS = ("token", "login", "auth", "unauthorized", "登录", "登陆", "过期", "失效")
DEFAULT_USER_AGENT = "Xueqiu iPhone 14.91"
DEFAULT_USER_AGENTS = [
    "Xueqiu iPhone 14.91",
    "Xueqiu iPhone 14.90",
    "Xueqiu iPhone 14.89",
    "Xueqiu iPhone 14.88",
]
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
        with smtplib.SMTP_SSL(server, port, timeout=20) as smtp:
            smtp.login(sender, password)
            smtp.send_message(message)
        return
    with smtplib.SMTP(server, port, timeout=20) as smtp:
        smtp.starttls()
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


class ClashNodeSwitcher:
    def __init__(
        self,
        *,
        controller: str,
        secret: str,
        group: str,
        node_indices: list[int],
        tokens: list[str],
    ) -> None:
        self.controller = controller.rstrip("/")
        self.secret = secret
        self.group = group
        self.lock = threading.Lock()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.nodes = self._resolve_nodes(node_indices)
        if not self.nodes:
            raise ValueError("No Clash nodes resolved")
        if len(tokens) != len(self.nodes):
            raise ValueError(
                "When Clash switching is enabled, --tokens and --clash-node-indices "
                "must have the same length so token-node pairs are explicit."
            )
        self.routes = list(zip(tokens, self.nodes))

    def select_random_route(self) -> tuple[str, str]:
        token, node = random.choice(self.routes)
        self.switch_to_route(token, node)
        return token, node

    def choose_random_route(self) -> tuple[str, str]:
        return random.choice(self.routes)

    def switch_to_route(self, token: str, node: str) -> None:
        url = f"{self.controller}/proxies/{urllib.parse.quote(self.group, safe='')}"
        request = urllib.request.Request(
            url,
            data=json.dumps({"name": node}).encode("utf-8"),
            method="PUT",
            headers={
                "Authorization": f"Bearer {self.secret}",
                "Content-Type": "application/json",
            },
        )
        with self.opener.open(request, timeout=5) as response:
            if response.status not in (200, 204):
                raise RuntimeError(f"Failed to switch Clash node, status={response.status}")

    def _resolve_nodes(self, node_indices: list[int]) -> list[str]:
        request = urllib.request.Request(
            f"{self.controller}/proxies",
            headers={"Authorization": f"Bearer {self.secret}"},
        )
        with self.opener.open(request, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
        group_info = data.get("proxies", {}).get(self.group)
        if not group_info:
            raise ValueError(f"Clash proxy group not found: {self.group}")
        all_nodes = group_info.get("all") or []
        nodes: list[str] = []
        for index in node_indices:
            if index < 0 or index >= len(all_nodes):
                raise ValueError(f"Clash node index out of range: {index}")
            nodes.append(all_nodes[index])
        return nodes


class XueqiuClient:
    def __init__(
        self,
        tokens: list[str],
        *,
        host: str,
        proxy: str | None = None,
        clash_switcher: ClashNodeSwitcher | None = None,
        notifier: TokenInvalidNotifier | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        accept_language: str = "zh-Hans-CN;q=1, en-CN;q=0.9",
        device_id: str | None = "DC27D03A-915A-4630-B430-A0FE52185B7A",
        timeout: float = 20.0,
        retries: int = 3,
        retry_sleep: float = 1.0,
        waf_405_message: str | None = None,
        pinned_route: tuple[str, str] | None = None,
        pinned_token: str | None = None,
        pinned_device_id: str | None = None,
        user_agents: list[str] | None = None,
        device_ids: list[str] | None = None,
    ) -> None:
        self.tokens = [token for token in tokens if token]
        if not self.tokens:
            raise ValueError("Xueqiu token is empty")
        self.clash_switcher = clash_switcher
        self.notifier = notifier
        self.timeout = timeout
        self.retries = retries
        self.retry_sleep = retry_sleep
        self.waf_405_message = waf_405_message
        self.pinned_route = pinned_route
        self.pinned_token = pinned_token
        self.pinned_device_id = pinned_device_id
        self.user_agents = user_agents or []
        self.device_ids = device_ids or []
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
        if pinned_device_id:
            self.base_headers["x-device-id"] = pinned_device_id
        if proxy:
            self.opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
        else:
            self.opener = urllib.request.build_opener()

    def get_json(self, url: str, params: dict[str, Any], *, add_nonce: bool = True) -> dict[str, Any]:
        query = dict(params)
        if add_nonce:
            query["_"] = int(time.time() * 1000)
            query["x"] = round(random.random(), 3)
        request_url = f"{url}?{urllib.parse.urlencode(query)}"
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            selected_token: str | None = None
            try:
                if self.clash_switcher is not None:
                    with self.clash_switcher.lock:
                        route_index: int | None = None
                        if self.pinned_route is not None:
                            selected_token, node = self.pinned_route
                            route_index = self.clash_switcher.routes.index(self.pinned_route)
                            self.clash_switcher.switch_to_route(selected_token, node)
                        else:
                            selected_token, node = self.clash_switcher.select_random_route()
                            route_index = self.clash_switcher.routes.index((selected_token, node))
                        payload = self._open_request(request_url, selected_token, route_index=route_index)
                else:
                    selected_token = self.pinned_token or random.choice(self.tokens)
                    payload = self._open_request(request_url, selected_token)
                data = json.loads(payload)
                self._raise_api_error_if_needed(data, selected_token)
                return data
            except urllib.error.HTTPError as exc:
                if exc.code == 405 and self.waf_405_message:
                    raise RuntimeError(self.waf_405_message) from exc
                if exc.code == 401:
                    token = selected_token or self.tokens[0]
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

    def _open_request(self, request_url: str, token: str, *, route_index: int | None = None) -> str:
        headers = dict(self.base_headers)
        if route_index is not None:
            if self.user_agents:
                headers["User-Agent"] = self.user_agents[route_index % len(self.user_agents)]
            if self.device_ids:
                headers["x-device-id"] = self.device_ids[route_index % len(self.device_ids)]
        headers["Cookie"] = f"xq_a_token={token};xq-dj-token={token};xq_is_login=1"
        request = urllib.request.Request(request_url, headers=headers)
        with self.opener.open(request, timeout=self.timeout) as response:
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
            token = selected_token or self.tokens[0]
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


def parse_tokens(args: argparse.Namespace) -> list[str]:
    if getattr(args, "tokens", None):
        return args.tokens
    token = getattr(args, "token", None) or os.environ.get("XUEQIU_TOKENS") or os.environ.get("XUEQIU_TOKEN")
    return [t.strip() for t in str(token or "").replace(";", ",").split(",") if t.strip()]


def parse_clash_node_indices(args: argparse.Namespace) -> list[int] | None:
    indices = getattr(args, "clash_node_indices", None)
    if indices is None and os.environ.get("XUEQIU_CLASH_NODE_INDICES"):
        indices = [
            int(i.strip())
            for i in os.environ["XUEQIU_CLASH_NODE_INDICES"].replace(";", ",").split(",")
            if i.strip()
        ]
    return indices


def build_clash_switcher(tokens: list[str], args: argparse.Namespace) -> ClashNodeSwitcher | None:
    clash_controller = getattr(args, "clash_controller", None) or os.environ.get("XUEQIU_CLASH_CONTROLLER")
    clash_secret = getattr(args, "clash_secret", None) or os.environ.get("XUEQIU_CLASH_SECRET")
    clash_group = getattr(args, "clash_group", None) or os.environ.get("XUEQIU_CLASH_GROUP") or "GLOBAL"
    clash_indices = parse_clash_node_indices(args)
    if clash_controller or clash_indices:
        if not clash_controller or not clash_secret or not clash_indices:
            raise SystemExit("--clash-controller, --clash-secret, and --clash-node-indices must be provided together.")
        if len(clash_indices) < len(tokens):
            raise SystemExit(
                f"Xueqiu requires at least one Clash node per token: tokens={len(tokens)} "
                f"nodes={len(clash_indices)}."
            )
        # Keep token-node pairing positional while allowing a reusable node pool
        # to contain more entries than the token set used by this run.
        clash_indices = clash_indices[: len(tokens)]
        switcher = ClashNodeSwitcher(
            controller=clash_controller,
            secret=clash_secret,
            group=clash_group,
            node_indices=clash_indices,
            tokens=tokens,
        )
        print(
            f"clash_switcher group={clash_group} token_node_pairs={len(switcher.routes)} "
            "request_node_switching=locked",
            flush=True,
        )
        return switcher
    return None


def parse_list_value(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]


def parse_user_agents(args: argparse.Namespace) -> list[str]:
    values = getattr(args, "user_agents", None)
    if values:
        return values
    env_values = parse_list_value(os.environ.get("XUEQIU_USER_AGENTS"))
    return env_values or DEFAULT_USER_AGENTS


def parse_device_ids(args: argparse.Namespace) -> list[str]:
    values = getattr(args, "device_ids", None)
    if values:
        return values
    return parse_list_value(os.environ.get("XUEQIU_DEVICE_IDS"))


def add_xueqiu_identity_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--user-agents", nargs="*", default=None, help="User-Agent list bound by route index.")
    parser.add_argument("--device-ids", nargs="*", default=None, help="Device ID list bound by route index.")


def add_xueqiu_auth_args(parser: argparse.ArgumentParser, *, default_clash_group: str | None = None) -> None:
    parser.add_argument("--token", default=None, help="Xueqiu token. Prefer XUEQIU_TOKEN env var.")
    parser.add_argument("--tokens", nargs="*", default=None, help="Xueqiu token pool. Overrides --token and XUEQIU_TOKEN.")
    parser.add_argument("--proxy", default=None, help="Optional HTTP(S) proxy URL. Prefer XUEQIU_PROXY env var.")
    parser.add_argument("--clash-controller", default=None, help="Optional Clash controller URL.")
    parser.add_argument("--clash-secret", default=None, help="Optional Clash controller secret.")
    parser.add_argument("--clash-group", default=default_clash_group, help="Clash proxy group to switch.")
    parser.add_argument(
        "--clash-node-indices",
        nargs="*",
        type=int,
        default=None,
        help="Node indices within --clash-group for token-node pairs.",
    )


def create_runtime(args: argparse.Namespace) -> tuple[list[str], str | None, ClashNodeSwitcher | None, TokenInvalidNotifier]:
    tokens = parse_tokens(args)
    if not tokens:
        raise SystemExit("Xueqiu token is required. Set XUEQIU_TOKEN or pass --token.")
    proxy = getattr(args, "proxy", None) or os.environ.get("XUEQIU_PROXY")
    clash_switcher = build_clash_switcher(tokens, args)
    notifier = TokenInvalidNotifier()
    return tokens, proxy, clash_switcher, notifier
