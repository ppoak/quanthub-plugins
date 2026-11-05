"""EastMoney QR-code login helpers."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any

from .constants import PASSPORT_LOGIN_URL
from .utils import _env_float, _user_id_from_pi, load_dotenv, mask_secret


class EastMoneyLoginError(RuntimeError):
    """Base error for EastMoney login failures."""


class EastMoneyQrCodeExpired(EastMoneyLoginError):
    """Raised when the QR code expires before login succeeds."""


class EastMoneyLoginTimeout(EastMoneyLoginError):
    """Raised when polling reaches the login timeout."""


@dataclass(frozen=True)
class EastMoneyCredentials:
    """Credentials extracted from an EastMoney logged-in web session."""

    user_id: str
    ut_token: str
    ct_token: str
    pi: str | None = None
    user_name: str = ""
    cookies: dict[str, str] = field(default_factory=dict)

    def as_kwargs(self) -> dict[str, Any]:
        """Return constructor kwargs shared by EastMoney transport classes."""
        return {
            "user_id": self.user_id,
            "ut_token": self.ut_token,
            "ct_token": self.ct_token,
            "pi": self.pi,
        }

    def masked(self) -> dict[str, str]:
        """Return a logging-safe summary."""
        return {
            "user_id": self.user_id,
            "ut_token": mask_secret(self.ut_token),
            "ct_token": mask_secret(self.ct_token),
            "pi": mask_secret(self.pi or ""),
            "user_name": self.user_name,
        }

    def env_values(self) -> dict[str, str]:
        """Return environment values used by existing ``from_env`` helpers."""
        values = {
            "EASTMONEY_MONI_USER_ID": self.user_id,
            "EASTMONEY_MONI_UT_TOKEN": self.ut_token,
            "EASTMONEY_MONI_CT_TOKEN": self.ct_token,
        }
        if self.pi:
            values["EASTMONEY_MONI_PI"] = self.pi
        return values

    def write_dotenv(self, path: str | Path = ".env") -> None:
        """Upsert EastMoney credential keys into a dotenv file."""
        env_path = Path(path)
        existing = (
            env_path.read_text(encoding="utf-8").splitlines()
            if env_path.exists()
            else []
        )
        updates = self.env_values()
        seen: set[str] = set()
        lines: list[str] = []
        for line in existing:
            key = line.split("=", 1)[0].strip() if "=" in line else ""
            if key in updates:
                lines.append(f"{key}={updates[key]}")
                seen.add(key)
            else:
                lines.append(line)
        for key, value in updates.items():
            if key not in seen:
                lines.append(f"{key}={value}")
        env_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


@dataclass(frozen=True)
class EastMoneyQrCode:
    """A generated EastMoney login QR-code image."""

    image_url: str
    image_path: Path
    expires_hint_seconds: int = 120


class EastMoneyLoginSession:
    """Stateful QR-code login session using EastMoney web endpoints."""

    def __init__(
        self,
        *,
        login_url: str = PASSPORT_LOGIN_URL,
        qr_path: str | Path = ".tmp/eastmoney_login_qr.png",
        timeout: float = 180.0,
        poll_interval: float = 1.0,
        user_agent: str = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
        ),
    ) -> None:
        self.login_url = login_url
        self.qr_path = Path(qr_path)
        self.timeout = float(timeout)
        self.poll_interval = float(poll_interval)
        self.user_agent = user_agent
        self.cookie_jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar)
        )
        self.request_log: list[dict[str, Any]] = []
        self._app_uri = ""
        self._headers: dict[str, str] = {}

    def start(self) -> EastMoneyQrCode:
        """Open the login page, generate a QR image, and save it locally."""
        login_html = self._request_text(self.login_url)
        frame_url = self._extract_iframe_src(login_html) or self.login_url
        login_form = self._request_text(frame_url)
        self._app_uri = self._origin(frame_url) + "/"
        self._headers = self._build_login_headers(frame_url, login_form)
        qr_url = urllib.parse.urljoin(
            self._app_uri,
            f"jsonapi/GenerateQrCode?rnd={time.time()}",
        )
        image = self._request_bytes(qr_url)
        self.qr_path.parent.mkdir(parents=True, exist_ok=True)
        self.qr_path.write_bytes(image)
        return EastMoneyQrCode(image_url=qr_url, image_path=self.qr_path)

    def wait(self) -> EastMoneyCredentials:
        """Poll EastMoney until the QR login succeeds, expires, or times out."""
        if not self._app_uri:
            raise EastMoneyLoginError("Call start() before wait().")
        status_url = urllib.parse.urljoin(self._app_uri, "JsonAPI/GetQrCodeLoginStatus")
        deadline = time.monotonic() + self.timeout
        last_payload: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            payload = self._post_json(status_url)
            last_payload = payload
            if payload.get("rc"):
                return self._credentials_from_state(payload.get("result"))
            if payload.get("errorcode") in (20, "20"):
                raise EastMoneyQrCodeExpired("EastMoney QR code expired.")
            time.sleep(self.poll_interval)
        raise EastMoneyLoginTimeout(
            f"EastMoney QR login timed out; last payload={last_payload!r}"
        )

    def login(self) -> EastMoneyCredentials:
        """Generate a QR code and wait for scanned login."""
        self.start()
        return self.wait()

    def _credentials_from_state(self, result: Any) -> EastMoneyCredentials:
        cookies = self.cookies()
        found = self._find_identity_values(result)
        ut_token = cookies.get("ut") or found.get("ut") or found.get("utToken") or ""
        ct_token = cookies.get("ct") or found.get("ct") or found.get("ctToken") or ""
        pi = cookies.get("pi") or found.get("pi") or None
        user_id = (
            found.get("userId")
            or found.get("uid")
            or _user_id_from_pi(pi)
            or self._user_id_from_uidal(cookies.get("uidal"))
            or ""
        )
        user_name = self._user_name_from_uidal(cookies.get("uidal"))
        if not ut_token or not ct_token:
            raise EastMoneyLoginError(
                "EastMoney login succeeded but ut/ct cookies were not found."
            )
        if not user_id:
            raise EastMoneyLoginError(
                "EastMoney login succeeded but user_id could not be inferred."
            )
        return EastMoneyCredentials(
            user_id=str(user_id),
            ut_token=str(ut_token),
            ct_token=str(ct_token),
            pi=str(pi) if pi else None,
            user_name=user_name,
            cookies=cookies,
        )

    def cookies(self) -> dict[str, str]:
        """Return current cookie jar values by cookie name."""
        return {cookie.name: cookie.value for cookie in self.cookie_jar}

    def _request_text(self, url: str) -> str:
        return self._request_bytes(url).decode("utf-8", "replace")

    def _request_bytes(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers=self._base_headers())
        self.request_log.append({"method": "GET", "url": url})
        with self.opener.open(request, timeout=30) as response:
            return response.read()

    def _post_json(self, url: str) -> dict[str, Any]:
        headers = {**self._base_headers(), **self._headers}
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        headers["X-Requested-With"] = "XMLHttpRequest"
        form = {}
        token = self._headers.get("RequestVerificationToken")
        if token:
            form["__RequestVerificationToken"] = token
        data = urllib.parse.urlencode(form).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        self.request_log.append({"method": "POST", "url": url, "form": form})
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            raise EastMoneyLoginError(
                f"POST {url} failed with HTTP {exc.code}: "
                f"{raw.decode('utf-8', 'replace')[:300]}"
            ) from exc
        data = json.loads(raw.decode("utf-8", "replace"))
        if not isinstance(data, dict):
            raise EastMoneyLoginError(f"Unexpected login status payload: {data!r}")
        return data

    def _base_headers(self) -> dict[str, str]:
        return {
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "User-Agent": self.user_agent,
        }

    def _build_login_headers(self, frame_url: str, html: str) -> dict[str, str]:
        request_data = self._request_data(frame_url)
        token = self._extract_request_verification_token(html)
        headers = {
            "Referer": frame_url,
            "Origin": self._origin(frame_url),
        }
        if token:
            headers["RequestVerificationToken"] = token
        data = request_data.get("data") if isinstance(request_data, dict) else {}
        if isinstance(data, dict):
            for source, target in (
                ("domainName", "domainName"),
                ("deviceType", "deviceType"),
                ("productType", "productType"),
                ("version", "version"),
            ):
                value = data.get(source)
                if value:
                    headers[target] = str(value)
        return headers

    def _request_data(self, frame_url: str) -> dict[str, Any]:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(frame_url).query)
        raw = (query.get("request") or [""])[0]
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _extract_iframe_src(html: str) -> str | None:
        frame_login = re.search(
            r"<iframe[^>]+id=[\"']frame_login[\"'][^>]+src=[\"']([^\"']+)[\"']",
            html,
            re.I,
        ) or re.search(
            r"<iframe[^>]+src=[\"']([^\"']+)[\"'][^>]+id=[\"']frame_login[\"']",
            html,
            re.I,
        )
        if frame_login:
            return urllib.parse.urljoin(
                PASSPORT_LOGIN_URL,
                frame_login.group(1).replace("&amp;", "&"),
            )
        dynamic = re.search(
            r"['\"](https://exaccount2\.eastmoney\.com/home/login4\?request=[^'\"]+)",
            html,
            re.I,
        )
        if dynamic:
            origin = urllib.parse.quote(
                urllib.parse.urlparse(PASSPORT_LOGIN_URL).scheme
                + "://"
                + urllib.parse.urlparse(PASSPORT_LOGIN_URL).netloc,
                safe="",
            )
            return dynamic.group(1).replace("__origin__", origin).replace("&amp;", "&")
        match = re.search(r"<iframe[^>]+src=[\"']([^\"']+)[\"']", html, re.I)
        if not match:
            return None
        src = match.group(1).replace("&amp;", "&")
        if "footer-" in src:
            return None
        return urllib.parse.urljoin(PASSPORT_LOGIN_URL, src)

    @staticmethod
    def _extract_request_verification_token(html: str) -> str:
        match = re.search(
            r"name=[\"']__RequestVerificationToken[\"'][^>]*value=[\"']([^\"']+)[\"']",
            html,
            re.I,
        )
        if match:
            return match.group(1)
        match = re.search(
            r"value=[\"']([^\"']+)[\"'][^>]*name=[\"']__RequestVerificationToken[\"']",
            html,
            re.I,
        )
        return match.group(1) if match else ""

    @staticmethod
    def _origin(url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    @classmethod
    def _find_identity_values(cls, data: Any) -> dict[str, Any]:
        keys = {"ut", "ct", "utToken", "ctToken", "pi", "userId", "uid"}
        found: dict[str, Any] = {}
        if isinstance(data, dict):
            for key, value in data.items():
                if key in keys and value not in (None, ""):
                    found[key] = value
                nested = cls._find_identity_values(value)
                found.update({k: v for k, v in nested.items() if k not in found})
        elif isinstance(data, list):
            for item in data:
                nested = cls._find_identity_values(item)
                found.update({k: v for k, v in nested.items() if k not in found})
        return found

    @staticmethod
    def _user_id_from_uidal(uidal: str | None) -> str:
        if not uidal:
            return ""
        decoded = urllib.parse.unquote(uidal)
        prefix = ""
        for char in decoded:
            if not char.isdigit():
                break
            prefix += char
        return prefix[:16] if len(prefix) >= 16 else prefix

    @staticmethod
    def _user_name_from_uidal(uidal: str | None) -> str:
        if not uidal:
            return ""
        decoded = urllib.parse.unquote(uidal)
        user_id = EastMoneyLoginSession._user_id_from_uidal(uidal)
        return decoded[len(user_id) :] if user_id else decoded


def login_with_qr(
    *,
    qr_path: str | Path | None = None,
    dotenv_path: str | Path | None = ".env",
    save_env: bool = True,
    timeout: float | None = None,
    poll_interval: float | None = None,
) -> EastMoneyCredentials:
    """Generate an EastMoney QR code, wait for scan, and optionally save dotenv."""
    load_dotenv(dotenv_path or ".env")
    qr_path = (
        qr_path
        or os.environ.get("EASTMONEY_MONI_QR_PATH")
        or ".tmp/eastmoney_login_qr.png"
    )
    timeout = (
        _env_float("EASTMONEY_MONI_LOGIN_TIMEOUT_SECONDS", 180.0)
        if timeout is None
        else timeout
    )
    poll_interval = (
        _env_float("EASTMONEY_MONI_LOGIN_POLL_INTERVAL_SECONDS", 1.0)
        if poll_interval is None
        else poll_interval
    )
    session = EastMoneyLoginSession(
        qr_path=qr_path,
        timeout=timeout,
        poll_interval=poll_interval,
    )
    qr = session.start()
    print(f"EastMoney QR code saved to: {qr.image_path}")
    print("Scan it with EastMoney app, then confirm login on the phone.")
    credentials = session.wait()
    if save_env and dotenv_path is not None:
        credentials.write_dotenv(dotenv_path)
        print(f"EastMoney credentials saved to: {dotenv_path}")
    print(f"EastMoney login succeeded: {credentials.masked()}")
    return credentials


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Login to EastMoney with QR code.")
    parser.add_argument("--qr-path")
    parser.add_argument("--dotenv-path", default=".env")
    parser.add_argument("--no-save-env", action="store_true")
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--poll-interval", type=float)
    args = parser.parse_args()

    login_with_qr(
        qr_path=args.qr_path,
        dotenv_path=args.dotenv_path,
        save_env=not args.no_save_env,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
    )


if __name__ == "__main__":
    main()
