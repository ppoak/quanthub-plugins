"""Xueqiu identity verification and browser login primitives."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable
from urllib.parse import unquote

from .accounts import XueqiuAccount, parse_proxy_url
from .client import DEFAULT_USER_AGENT, build_xueqiu_opener


XUEQIU_ORIGIN = "https://www.xueqiu.com"
ACCOUNT_BIND_URL = f"{XUEQIU_ORIGIN}/account/bind/show.json"
USER_SHOW_URL = f"{XUEQIU_ORIGIN}/user/show.json"


class AuthenticationFailed(RuntimeError):
    """Xueqiu rejected the credentials."""


class AccountIdentityMismatch(AuthenticationFailed):
    """The authenticated account does not match the requested phone."""


class IdentityUnavailable(RuntimeError):
    """Identity could not be verified because the service or response failed."""


class NoIdentityRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _identity_payload(status: int, payload: object) -> dict:
    data = payload if isinstance(payload, dict) else {}
    code = str(data.get("error_code") or "0")
    if status == 401 or (
        (200 <= status < 300 or status in {400, 403}) and code in {"400013", "400016"}
    ):
        raise AuthenticationFailed(f"Identity endpoint rejected login (HTTP {status})")
    if not 200 <= status < 300:
        raise IdentityUnavailable(f"Identity endpoint returned HTTP {status}")
    if not isinstance(payload, dict) or code != "0":
        raise IdentityUnavailable("Identity endpoint returned an invalid response or API error")
    return data


def _request_json(
    url: str, token: str, *, proxy: str | None, timeout: float,
) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json",
            "Cookie": f"xq_a_token={token};xq-dj-token={token};xq_is_login=1",
        },
    )
    try:
        opener = build_xueqiu_opener(proxy, NoIdentityRedirect())
        with opener.open(request, timeout=timeout) as response:
            return _identity_payload(response.status, json.loads(response.read()))
    except urllib.error.HTTPError as exc:
        with exc:
            try:
                payload = json.loads(exc.read())
            except (OSError, ValueError):
                payload = None
        return _identity_payload(exc.code, payload)
    except (OSError, ValueError) as exc:
        raise IdentityUnavailable(f"Identity request failed ({type(exc).__name__})") from exc


def _account_username(request_json: Callable[[str], dict], phone: str, phones: set[str]) -> str:
    telephone = str(request_json(ACCOUNT_BIND_URL).get("telephone") or "").strip()
    pattern = re.escape(telephone).replace(r"\*", r"\d")
    if not telephone or sum(char.isdigit() for char in telephone) < 4:
        raise IdentityUnavailable("Identity endpoint returned no usable phone")
    if not re.fullmatch(pattern, phone):
        raise AccountIdentityMismatch("Xueqiu account phone does not match the login target")
    candidates = phones | {phone}
    if sum(bool(re.fullmatch(pattern, candidate)) for candidate in candidates) > 1:
        raise IdentityUnavailable("Masked phone matches multiple configured accounts")
    profile = request_json(USER_SHOW_URL)
    username = str(profile.get("screen_name") or profile.get("name") or "").strip()
    if not username:
        raise IdentityUnavailable("Identity endpoint returned no username")
    return username


def request_timeout(timeout: float, deadline: float | None) -> float:
    remaining = timeout if deadline is None else min(timeout, deadline - time.monotonic())
    if remaining <= 0:
        raise IdentityUnavailable("Identity verification deadline reached")
    return remaining


def verify_token_account(
    account: XueqiuAccount, *, proxy: str | None, timeout: float, phones: set[str],
    deadline: float | None = None, candidate_token: str | None = None,
) -> str:
    token = account.token if candidate_token is None else candidate_token
    proxy = account.proxy_url or proxy

    def request_json(url: str) -> dict:
        return _request_json(url, token, proxy=proxy, timeout=request_timeout(timeout, deadline))

    return _account_username(request_json, account.phone, phones)


def _session_cookie(context) -> str:
    return next(
        (item["value"] for item in context.cookies([XUEQIU_ORIGIN]) if item["name"] == "xq_a_token"),
        "",
    )


def session_token(
    context, account: XueqiuAccount, *, timeout_ms: int, phones: set[str],
    deadline: float | None = None, reject_session: bool = False,
) -> tuple[str, str] | None:
    from playwright.sync_api import Error

    def request_json(url: str) -> dict:
        timeout = request_timeout(timeout_ms / 1000, deadline)
        try:
            response = context.request.get(url, timeout=timeout * 1000, max_redirects=0)
        except Error as exc:
            raise IdentityUnavailable("Browser identity request failed") from exc
        try:
            payload = response.json()
        except (Error, ValueError):
            payload = None
        return _identity_payload(response.status, payload)

    if not _session_cookie(context):
        return None
    try:
        _account_username(request_json, account.phone, phones)
        token = _session_cookie(context)
        if not token:
            return None
        username = verify_token_account(
            account, candidate_token=token, proxy=os.environ.get("XUEQIU_PROXY"),
            timeout=timeout_ms / 1000, deadline=deadline, phones=phones,
        )
    except AccountIdentityMismatch:
        raise
    except AuthenticationFailed:
        if reject_session:
            raise
        return None
    return token, username


def open_qr_login(page) -> None:
    page.goto(f"{XUEQIU_ORIGIN}/", wait_until="domcontentloaded", timeout=30_000)
    page.get_by_text("二维码登录", exact=True).filter(visible=True).first.click(timeout=30_000)


def _qr_wrapper(page, deadline: float | None = None):
    wrapper = page.locator('[class*="index_qrcode__wrapper"]').first
    wrapper.wait_for(state="visible", timeout=request_timeout(20, deadline) * 1000)
    return wrapper


def capture_qr(page, output_dir: Path, sequence: int, *, deadline: float | None = None) -> Path:
    wrapper = _qr_wrapper(page, deadline)
    canvas = wrapper.locator("canvas").first
    canvas.wait_for(state="visible", timeout=request_timeout(20, deadline) * 1000)
    page.wait_for_function(
        """() => {
            const wrapper = document.querySelector('[class*="index_qrcode__wrapper"]');
            if (!wrapper || /刷新二维码|失效/.test(wrapper.innerText)) return false;
            const c = wrapper.querySelector('canvas');
            if (!c || !c.width || !c.height) return false;
            const p = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
            for (let i = 0; i < p.length; i += 4) {
                if (p[i] < 80 && p[i + 3] > 0) return true;
            }
            return false;
        }""",
        timeout=request_timeout(20, deadline) * 1000,
    )
    box = canvas.bounding_box(timeout=request_timeout(20, deadline) * 1000)
    if box is None:
        raise RuntimeError("Xueqiu login QR code bounds are unavailable")
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"qrcode-{sequence}.png"
    page.screenshot(
        path=str(path),
        timeout=request_timeout(20, deadline) * 1000,
        clip={
            "x": max(0, box["x"] - 16),
            "y": max(0, box["y"] - 16),
            "width": box["width"] + 32,
            "height": box["height"] + 32,
        },
    )
    os.chmod(path, 0o600)
    return path


def qr_state(page, *, deadline: float | None = None) -> str:
    try:
        return _qr_wrapper(page, deadline).inner_text(timeout=request_timeout(1, deadline) * 1000).strip()
    except Exception:
        return ""


def refresh_qr(page, *, deadline: float | None = None) -> None:
    previous_image = _qr_wrapper(page, deadline).locator("canvas").first.evaluate(
        "canvas => canvas.toDataURL()", timeout=request_timeout(20, deadline) * 1000,
    )
    refresh = page.get_by_text("点击刷新二维码", exact=True).filter(visible=True).first
    try:
        refresh.click(timeout=request_timeout(2, deadline) * 1000)
    except Exception:
        page.reload(wait_until="domcontentloaded", timeout=request_timeout(30, deadline) * 1000)
        page.get_by_text("二维码登录", exact=True).filter(visible=True).first.click(
            timeout=request_timeout(30, deadline) * 1000,
        )
    page.wait_for_function(
        """previous => {
            const wrapper = document.querySelector('[class*="index_qrcode__wrapper"]');
            if (!wrapper || /刷新二维码|失效/.test(wrapper.innerText)) return false;
            const canvas = wrapper.querySelector('canvas');
            return canvas && canvas.toDataURL() !== previous;
        }""",
        arg=previous_image,
        timeout=request_timeout(20, deadline) * 1000,
    )


def browser_proxy_options(proxy: str | None) -> dict[str, str] | None:
    if not proxy:
        return None
    url = parse_proxy_url(proxy, "Xueqiu browser proxy")
    settings = {"server": f"{url.scheme}://{url.netloc.rsplit('@', 1)[-1]}"}
    if url.username is not None:
        settings.update(username=unquote(url.username), password=unquote(url.password or ""))
    return settings


def clear_browser_session(context) -> None:
    existing_pages = list(context.pages)
    page = context.new_page()
    for existing_page in existing_pages:
        existing_page.close()
    context.clear_cookies()
    devtools = context.new_cdp_session(page)
    try:
        for origin in (XUEQIU_ORIGIN, "https://xueqiu.com"):
            devtools.send("Storage.clearDataForOrigin", {"origin": origin, "storageTypes": "all"})
    finally:
        devtools.detach()
