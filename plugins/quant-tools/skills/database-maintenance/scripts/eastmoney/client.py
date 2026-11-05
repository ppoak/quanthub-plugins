from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb


FALLBACK_APP_USER_AGENT = "Mozilla/5.0"
FALLBACK_WEBVIEW_USER_AGENT = "Mozilla/5.0"
FALLBACK_STOCK_CARD_CLIENT_VERSION = "11.1.6"
FALLBACK_PLATFORM = "iOS"
FALLBACK_STOCK_CARD_USER_ID = ""
FALLBACK_DEVICE_ID = ""

STOCK_CARD_URL = "https://eminterservice.securities.eastmoney.com/api/data/stockCard"
HIS_PROFILE_URL = "https://emappdata.eastmoney.com/stockrank/getHisProfileList"
HIS_RANK_URL = "https://emappdata.eastmoney.com/stockrank/getHisList"
STOCK_CHANGES_URL = "https://push2ex.eastmoney.com/getAllStockChanges"

AUTH_ERROR_PATTERNS = ("token", "login", "auth", "unauthorized", "登录", "登陆", "过期", "失效")
STOCK_CHANGE_TYPES = {
    "火箭发射": "8201",
    "快速反弹": "8202",
    "大笔买入": "8193",
    "封涨停板": "4",
    "打开跌停板": "32",
    "有大买盘": "64",
    "竞价上涨": "8207",
    "高开5日线": "8209",
    "向上缺口": "8211",
    "60日新高": "8213",
    "60日大幅上涨": "8215",
    "加速下跌": "8204",
    "高台跳水": "8203",
    "大笔卖出": "8194",
    "封跌停板": "8",
    "打开涨停板": "16",
    "有大卖盘": "128",
    "竞价下跌": "8208",
    "低开5日线": "8210",
    "向下缺口": "8212",
    "60日新低": "8214",
    "60日大幅下跌": "8216",
}
STOCK_CHANGE_TYPE_NAMES = {value: key for key, value in STOCK_CHANGE_TYPES.items()}


class EastMoneyAuthError(RuntimeError):
    pass


class EastMoneyApiError(RuntimeError):
    pass


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


def mask_secret(value: str) -> str:
    if len(value) <= 12:
        return value[:2] + "***"
    return value[:6] + "..." + value[-6:]


def is_auth_error(message: str) -> bool:
    lowered = message.lower()
    return any(pattern in lowered for pattern in AUTH_ERROR_PATTERNS)


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


def to_eastmoney_symbol(code: str) -> str:
    normalized = normalize_code(code)
    ticker, exchange = normalized.split(".", 1)
    return f"{exchange}{ticker}"


def normalize_eastmoney_ticker(code: str) -> str:
    raw = code.strip().upper()
    if "." in raw:
        ticker, _exchange = raw.split(".", 1)
        return ticker
    if raw.startswith(("SH", "SZ", "BJ")):
        return raw[2:]
    return raw


def infer_market(code: str) -> str:
    ticker = normalize_eastmoney_ticker(code)
    if ticker.startswith(("5", "6", "9")):
        return "1"
    if ticker.startswith(("0", "1", "2", "3")):
        return "0"
    if ticker.startswith(("4", "8")):
        return "2"
    raise ValueError(f"Cannot infer EastMoney market from code: {code}")


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


class EastMoneyDataClient:
    def __init__(
        self,
        *,
        user_id: str = "",
        ut_token: str = "",
        ct_token: str = "",
        app_user_agent: str = FALLBACK_APP_USER_AGENT,
        webview_user_agent: str = FALLBACK_WEBVIEW_USER_AGENT,
        stock_card_client_version: str = FALLBACK_STOCK_CARD_CLIENT_VERSION,
        platform: str = FALLBACK_PLATFORM,
        device_id: str | None = None,
        em_gt: str | None = None,
        proxy: str | None = None,
        timeout: float = 20.0,
        retries: int = 1,
        retry_sleep: float = 1.0,
    ) -> None:
        self.user_id = user_id
        self.ut_token = ut_token
        self.ct_token = ct_token
        self.app_user_agent = app_user_agent
        self.webview_user_agent = webview_user_agent
        self.stock_card_client_version = stock_card_client_version
        self.platform = platform
        self.device_id = device_id or device_id_from_user_agent(app_user_agent) or FALLBACK_DEVICE_ID
        self.em_gt = em_gt
        self.timeout = timeout
        self.retries = retries
        self.retry_sleep = retry_sleep
        if proxy:
            self.opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
        else:
            self.opener = urllib.request.build_opener()

    @classmethod
    def from_env(cls, *, dotenv_path: str | Path = ".env") -> "EastMoneyDataClient":
        load_dotenv(dotenv_path)
        return cls(
            user_id=os.environ.get("EASTMONEY_STOCK_CARD_USER_ID", FALLBACK_STOCK_CARD_USER_ID),
            ut_token=os.environ.get("EASTMONEY_MONI_UT_TOKEN", ""),
            ct_token=os.environ.get("EASTMONEY_MONI_CT_TOKEN", ""),
            app_user_agent=os.environ.get("EASTMONEY_APP_USER_AGENT", FALLBACK_APP_USER_AGENT),
            webview_user_agent=os.environ.get("EASTMONEY_WEBVIEW_USER_AGENT", FALLBACK_WEBVIEW_USER_AGENT),
            stock_card_client_version=os.environ.get(
                "EASTMONEY_STOCK_CARD_CLIENT_VERSION",
                FALLBACK_STOCK_CARD_CLIENT_VERSION,
            ),
            platform=os.environ.get("EASTMONEY_APP_PLATFORM", FALLBACK_PLATFORM),
            device_id=os.environ.get("EASTMONEY_APP_DEVICE_ID") or FALLBACK_DEVICE_ID or None,
            em_gt=os.environ.get("EASTMONEY_APP_GT") or None,
            proxy=os.environ.get("EASTMONEY_MONI_PROXY") or None,
        )

    def build_stock_card_payload(
        self,
        *,
        code: str,
        method: str = "qgqm",
        line: str = "5",
        stock_type: int = 2,
        page_id: str = "stockmk",
        market_code: int | None = None,
        switch_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        resolved_market = market_code if market_code is not None else int(infer_market(code))
        ticker = normalize_eastmoney_ticker(code)
        stock_code = stock_code_with_market(ticker, resolved_market)
        return {
            "appKey": "cfw",
            "deviceId": self.device_id,
            "clientVersion": self.stock_card_client_version,
            "client": self.platform.lower(),
            "clientType": "cfw",
            "method": method,
            "reserve": "",
            "args": {
                "stockType": stock_type,
                "uid": self.user_id or FALLBACK_STOCK_CARD_USER_ID,
                "marketCode": resolved_market,
                "fundLogin": "false",
                "pageId": page_id,
                "custid": "",
                "customerId": "",
                "fundId": "",
                "stockCode": stock_code,
                "stockCodeWithoutMarket": ticker,
                "hkFundLogin": "false",
                "hkFundId": "",
                "line": str(line),
                "switchMap": switch_map or {"shilaohua": "0"},
            },
        }

    def stock_card(
        self,
        *,
        code: str,
        method: str = "qgqm",
        line: str = "5",
        stock_type: int = 2,
        page_id: str = "stockmk",
        market_code: int | None = None,
        switch_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        payload = self.build_stock_card_payload(
            code=code,
            method=method,
            line=line,
            stock_type=stock_type,
            page_id=page_id,
            market_code=market_code,
            switch_map=switch_map,
        )
        data = self._get_json(
            STOCK_CARD_URL,
            {"data": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
            headers=self._stock_card_headers(),
        )
        self._raise_endpoint_error_if_needed(data, "stockCard")
        return data

    def his_profile_list(
        self,
        *,
        code: str,
        year_type: int = 2,
        app_id: str = "appId01",
        market: str | None = None,
    ) -> dict[str, Any]:
        payload = self.build_his_profile_payload(
            code=code,
            year_type=year_type,
            app_id=app_id,
            market=market,
        )
        data = self._post_json(HIS_PROFILE_URL, payload, headers=self._stockrank_headers())
        self._raise_endpoint_error_if_needed(data, "stockrank/getHisProfileList")
        return data

    def his_rank_list(
        self,
        *,
        code: str,
        year_type: int = 2,
        app_id: str = "appId01",
        market: str | None = None,
        market_type: str = "",
    ) -> dict[str, Any]:
        payload = self.build_his_rank_payload(
            code=code,
            year_type=year_type,
            app_id=app_id,
            market=market,
            market_type=market_type,
        )
        data = self._post_json(HIS_RANK_URL, payload, headers=self._stockrank_headers())
        self._raise_endpoint_error_if_needed(data, "stockrank/getHisList")
        return data

    def build_his_profile_payload(
        self,
        *,
        code: str,
        year_type: int = 2,
        app_id: str = "appId01",
        market: str | None = None,
    ) -> dict[str, Any]:
        ticker = normalize_eastmoney_ticker(code)
        resolved_market = market or ("SH" if infer_market(ticker) == "1" else "SZ")
        return {
            "appId": app_id,
            "globalId": "",
            "srcSecurityCode": f"{resolved_market}{ticker}",
            "yearType": int(year_type),
        }

    def build_his_rank_payload(
        self,
        *,
        code: str,
        year_type: int = 2,
        app_id: str = "appId01",
        market: str | None = None,
        market_type: str = "",
    ) -> dict[str, Any]:
        payload = self.build_his_profile_payload(
            code=code,
            year_type=year_type,
            app_id=app_id,
            market=market,
        )
        payload["marketType"] = market_type
        return payload

    def stock_changes(
        self,
        *,
        change_type: str | None = None,
        change_types: list[str] | tuple[str, ...] | None = None,
        page_index: int = 0,
        page_size: int = 5000,
        ut: str = "7eea3edcaed734bea9cbfc24409ed989",
        dpt: str = "wzchanges",
        callback: str | None = None,
    ) -> dict[str, Any]:
        resolved_types = list(change_types or ([change_type] if change_type else STOCK_CHANGE_TYPES.keys()))
        unsupported = [item for item in resolved_types if item not in STOCK_CHANGE_TYPES]
        if unsupported:
            choices = ", ".join(STOCK_CHANGE_TYPES)
            raise ValueError(f"Unsupported EastMoney stock change type: {unsupported}; choices: {choices}")
        params = {
            "type": ",".join(STOCK_CHANGE_TYPES[item] for item in resolved_types),
            "pageindex": str(int(page_index)),
            "pagesize": str(int(page_size)),
            "ut": ut,
            "dpt": dpt,
        }
        if callback:
            params["cb"] = callback
            params["_"] = str(int(time.time() * 1000))
        data = self._get_json(STOCK_CHANGES_URL, params, headers=self._push2ex_headers())
        self._raise_endpoint_error_if_needed(data, "getAllStockChanges")
        return data

    def stock_changes_page(
        self,
        *,
        change_types: list[str] | tuple[str, ...] | None = None,
        page_size: int = 5000,
        page_index: int = 0,
    ) -> dict[str, Any]:
        return self.stock_changes(
            change_types=list(change_types or STOCK_CHANGE_TYPES.keys()),
            page_index=page_index,
            page_size=page_size,
        )

    def _stock_card_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": self.app_user_agent,
            "Accept-Encoding": "gzip",
            "Accept-Language": "zh-CN,zh-Hans;q=0.9",
            "EM-HQDELAY": "",
            "EM-VER": self.stock_card_client_version,
            "EM-PA": "1",
            "EM-SL": "0",
            "EM-OS": self.platform,
            "EM-MD": base64.b64encode(self.device_id.encode("utf-8")).decode("ascii"),
            "EM-PKG": "com.eastmoney.iphone",
            "EM-HQSHIEL": "",
        }
        if self.ct_token:
            headers["EM-CT"] = self.ct_token
        if self.ut_token:
            headers["EM-UT"] = self.ut_token
        if self.em_gt:
            headers["EM-GT"] = self.em_gt
        return headers

    def _stockrank_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.webview_user_agent,
            "Content-Type": "application/json",
            "Accept-Language": "zh-CN,zh-Hans;q=0.9",
            "Origin": "https://vipmoney.eastmoney.com",
            "Referer": "https://vipmoney.eastmoney.com/",
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }

    def _push2ex_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.webview_user_agent or "Mozilla/5.0",
            "Accept-Encoding": "gzip",
            "Accept-Language": "zh-CN,zh-Hans;q=0.9",
            "Referer": "https://quote.eastmoney.com/changes/",
        }

    def _get_json(
        self,
        url: str,
        params: dict[str, Any],
        *,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        request_url = f"{url}?{urllib.parse.urlencode(params, safe=',')}"
        return self._request_json(urllib.request.Request(request_url, headers=headers))

    def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        return self._request_json(request)

    def _request_json(self, request: urllib.request.Request) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    raw = response.read()
                    if response.headers.get("content-encoding") == "gzip":
                        import gzip

                        raw = gzip.decompress(raw)
                text = raw.decode("utf-8", "replace")
                data = json.loads(strip_jsonp(text))
                if not isinstance(data, dict):
                    raise EastMoneyApiError("Unexpected JSON response type")
                return data
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise EastMoneyAuthError(f"HTTP {exc.code}") from exc
                last_error = exc
            except (TimeoutError, urllib.error.URLError, json.JSONDecodeError, EastMoneyApiError) as exc:
                last_error = exc
            if attempt < self.retries:
                time.sleep(self.retry_sleep * (attempt + 1))
        raise EastMoneyApiError(f"EastMoney request failed after retries: {last_error}") from last_error

    def _raise_endpoint_error_if_needed(self, data: dict[str, Any], endpoint_name: str) -> None:
        result = data.get("result") or data.get("code") or data.get("rc") or data.get("status")
        message = str(data.get("message") or data.get("msg") or data.get("error") or "")
        if result in (None, 0, "0", True):
            return
        if is_auth_error(message):
            raise EastMoneyAuthError(message or f"EastMoney {endpoint_name} auth error: result={result}")
        raise EastMoneyApiError(message or f"EastMoney {endpoint_name} error: result={result}")


def device_id_from_user_agent(user_agent: str | None) -> str | None:
    if not user_agent:
        return None
    match = re.search(
        r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}",
        user_agent,
    )
    return match.group(0) if match else None


def stock_code_with_market(ticker: str, market_code: int) -> str:
    if market_code == 1:
        return f"SH{ticker}"
    if market_code == 0:
        return f"SZ{ticker}"
    if market_code == 2:
        return f"BJ{ticker}"
    raise ValueError(f"Unsupported stockCard marketCode: {market_code}")


def strip_jsonp(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return stripped
    if stripped[0] in "[{":
        return stripped
    open_paren = stripped.find("(")
    close_paren = stripped.rfind(")")
    if open_paren > 0 and close_paren > open_paren:
        return stripped[open_paren + 1 : close_paren]
    return stripped
