"""Email-assisted Xueqiu re-login helpers."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import ssl
import stat
import tempfile
import imaplib
import os
import re
import smtplib
import hashlib
import time
import uuid
import json
import urllib.error
import urllib.request
from datetime import datetime
from email import message_from_bytes
from email.header import decode_header
from email.message import EmailMessage
from email.utils import make_msgid
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable


AUTH_ROOT = Path(".tmp/database-maintenance/xueqiu-auth")
DEFAULT_PROFILE_DIR = AUTH_ROOT / "browser"
XUEQIU_ORIGIN = "https://www.xueqiu.com"
ACCOUNT_BIND_URL = f"{XUEQIU_ORIGIN}/account/bind/show.json"
USER_SHOW_URL = f"{XUEQIU_ORIGIN}/user/show.json"


def send_qr_email(
    phone: str,
    recipient: str,
    qr_path: str | Path,
    *,
    sequence: int,
    deadline: float | None = None,
) -> tuple[str, str]:
    """Send one newly generated QR image and return its request identifiers."""
    sender = os.environ["SENDER"]
    password = os.environ["EMAIL_PASSWORD"]
    server = os.environ["SMTP_SERVER"]
    port = int(os.environ.get("SMTP_PORT", "465"))
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    reply_to = os.environ.get("XUEQIU_IMAP_USERNAME") or sender
    if reply_to != sender:
        message["Reply-To"] = reply_to
    request_id = uuid.uuid4().hex[:8].upper()
    suffix = phone[-4:]
    message["Message-ID"] = make_msgid()
    message["Subject"] = f"Xueqiu QR login | {suffix} | {request_id} | {sequence}"
    message.set_content(
        "Please scan the attached Xueqiu login QR code with the Xueqiu App and confirm login.\n"
        "If the QR code has expired or you need a new one, reply to the latest QR email with a single line: 刷新\n"
        f"Request ID: {request_id}\nPhone suffix: {suffix}\n"
    )
    qr_file = Path(qr_path)
    message.add_attachment(
        qr_file.read_bytes(), maintype="image", subtype="png",
        filename=f"xueqiu-login-{suffix}-{sequence}.png",
    )
    use_ssl = os.environ.get("SMTP_USE_SSL", "1").lower() not in {"0", "false", "no"}
    tls = ssl.create_default_context()
    timeout = _request_timeout(30, deadline)
    connection = (
        smtplib.SMTP_SSL(server, port, timeout=timeout, context=tls)
        if use_ssl else smtplib.SMTP(server, port, timeout=timeout)
    )
    with connection as smtp:
        if not use_ssl:
            smtp.sock.settimeout(_request_timeout(30, deadline))
            smtp.starttls(context=tls)
        smtp.sock.settimeout(_request_timeout(30, deadline))
        smtp.login(sender, password)
        smtp.sock.settimeout(_request_timeout(30, deadline))
        smtp.send_message(message)
    return request_id, message["Message-ID"] or ""


class _ReplyHTML(HTMLParser):
    """Read the new reply and stop at common quoted-message wrappers."""

    def __init__(self):
        super().__init__()
        self.chunks: list[str] = []
        self.quoted = False

    def handle_starttag(self, tag, attrs):
        markers = " ".join(value or "" for key, value in attrs if key in {"class", "id"}).lower()
        if tag in {"blockquote", "hr"} or any(
            marker in markers for marker in ("gmail_quote", "yahoo_quoted", "divrplyfwdmsg", "moz-cite-prefix", "qqmail_quote")
        ):
            self.quoted = True
        if tag in {"div", "p", "br", "li"} and not self.quoted:
            self.chunks.append("\n")

    def handle_data(self, data):
        if not self.quoted:
            self.chunks.append(data)


def _message_text(message) -> str:
    from email import policy

    body = message_from_bytes(message.as_bytes(), policy=policy.default).get_body(
        preferencelist=("plain", "html")
    )
    if body is None or body.get_content_disposition() == "attachment":
        return ""
    text = body.get_content()
    if body.get_content_subtype() == "html":
        parser = _ReplyHTML()
        parser.feed(text)
        text = "".join(parser.chunks)
    return re.split(
        r"(?im)^\s*(?:>|On .+ wrote:|[- ]*Original Message[- ]*|[- ]*原始邮件[- ]*|From:|发件人[:：])",
        text, maxsplit=1,
    )[0]


def _is_refresh_reply(body: str) -> bool:
    return body.strip().casefold() in {"刷新", "refresh", "重新发送", "重发二维码"}


def poll_refresh_request(
    *, request_id: str, outbound_message_id: str, seen_message_ids: set[bytes],
    deadline: float | None = None,
) -> str | None:
    username = os.environ.get("XUEQIU_IMAP_USERNAME") or os.environ["SENDER"]
    password = os.environ.get("XUEQIU_IMAP_PASSWORD") or os.environ["EMAIL_PASSWORD"]
    server = os.environ.get("XUEQIU_IMAP_SERVER", "imap.qq.com")
    port = int(os.environ.get("XUEQIU_IMAP_PORT", "993"))
    with imaplib.IMAP4_SSL(
        server, port, timeout=_request_timeout(30, deadline), ssl_context=ssl.create_default_context(),
    ) as mailbox:
        mailbox.sock.settimeout(_request_timeout(30, deadline))
        mailbox.login(username, password)
        mailbox.sock.settimeout(_request_timeout(30, deadline))
        mailbox.select("INBOX", readonly=True)
        candidates: set[bytes] = set()
        for header, value in (
            ("Subject", request_id), ("In-Reply-To", outbound_message_id), ("References", outbound_message_id),
        ):
            mailbox.sock.settimeout(_request_timeout(30, deadline))
            status, data = mailbox.uid("SEARCH", None, "HEADER", header, f'"{value}"')
            if status != "OK":
                raise imaplib.IMAP4.error("IMAP inbox search failed")
            candidates.update(data[0].split())
        for uid in sorted(candidates - seen_message_ids, key=int, reverse=True):
            mailbox.sock.settimeout(_request_timeout(30, deadline))
            status, raw = mailbox.uid("FETCH", uid, "(BODY.PEEK[])")
            if status != "OK":
                raise imaplib.IMAP4.error("IMAP message fetch failed")
            message = message_from_bytes(raw[0][1])
            subject = "".join(
                part.decode(charset or "utf-8", errors="replace") if isinstance(part, bytes) else part
                for part, charset in decode_header(message.get("Subject", ""))
            )
            direct = re.findall(r"<[^<>]+>", str(message.get("In-Reply-To", "")))
            references = re.findall(r"<[^<>]+>", str(message.get("References", "")))
            thread_parent = (direct or references)[-1:]
            matches = (
                thread_parent == [outbound_message_id] if thread_parent else
                bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(request_id)}(?![A-Za-z0-9])", subject))
            )
            seen_message_ids.add(uid)
            if matches and _is_refresh_reply(_message_text(message)):
                return request_id
    return None


def configured_login_targets() -> list[tuple[int, str, str]]:
    phones = _ordered_values("XUEQIU_LOGIN_PHONES")
    recipients = _ordered_values("XUEQIU_LOGIN_EMAILS")
    return [
        (index, phone, recipients[index] if index < len(recipients) else "")
        for index, phone in enumerate(phones)
        if phone
    ]


class AuthenticationFailed(RuntimeError):
    """Xueqiu rejected the credentials."""


class AccountIdentityMismatch(AuthenticationFailed):
    """The authenticated account does not match the requested phone."""


class IdentityUnavailable(RuntimeError):
    """Identity could not be verified because the service or response failed."""


class ReloginFailed(RuntimeError):
    """The login flow failed at a reported stage."""


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


def _request_json(url: str, token: str, *, proxy: str | None, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Xueqiu iPhone 14.91",
            "Accept": "application/json",
            "Cookie": f"xq_a_token={token};xq-dj-token={token};xq_is_login=1",
        },
    )
    proxy_handler = (
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        if proxy
        else urllib.request.ProxyHandler({})
    )
    opener = urllib.request.build_opener(proxy_handler, NoIdentityRedirect())
    try:
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


def _account_username(request_json: Callable[[str], dict], phone: str) -> str:
    telephone = str(request_json(ACCOUNT_BIND_URL).get("telephone") or "").strip()
    pattern = re.escape(telephone).replace(r"\*", r"\d")
    if not telephone or sum(char.isdigit() for char in telephone) < 4:
        raise IdentityUnavailable("Identity endpoint returned no usable phone")
    if not re.fullmatch(pattern, phone):
        raise AccountIdentityMismatch("Xueqiu account phone does not match the login target")
    candidates = set(_ordered_values("XUEQIU_LOGIN_PHONES")) | {phone}
    if sum(bool(re.fullmatch(pattern, candidate)) for candidate in candidates) > 1:
        raise IdentityUnavailable("Masked phone matches multiple configured accounts")
    profile = request_json(USER_SHOW_URL)
    username = str(profile.get("screen_name") or profile.get("name") or "").strip()
    if not username:
        raise IdentityUnavailable("Identity endpoint returned no username")
    return username


def _request_timeout(timeout: float, deadline: float | None) -> float:
    remaining = timeout if deadline is None else min(timeout, deadline - time.monotonic())
    if remaining <= 0:
        raise IdentityUnavailable("Identity verification deadline reached")
    return remaining


def verify_token_account(
    token: str, phone: str, *, proxy: str | None, timeout: float, deadline: float | None = None,
) -> str:
    def request_json(url: str) -> dict:
        return _request_json(url, token, proxy=proxy, timeout=_request_timeout(timeout, deadline))

    return _account_username(request_json, phone)


def _record_account_summary(phone: str, value: str) -> None:
    label = phone if phone.startswith("slot ") else phone[-4:]
    if value.startswith("FAILED:"):
        print(f"xueqiu_account_result account={label} {value}", flush=True)
    try:
        from scripts.common.scheduler import record_task_output
    except ImportError:
        return
    record_task_output(f"Xueqiu account {label}", value, None, operation="account")


def _token_values() -> list[str]:
    return _credential_slots(os.environ)


def scheduled_relogin(
    *, total_timeout_seconds: int = 600,
    env_path: str | Path = ".env", profile_dir: str | Path | None = None,
) -> bool:
    targets = configured_login_targets()
    tokens = _token_values()
    configured_slots = {index for index, _, _ in targets}
    missing_phones = [index for index, token in enumerate(tokens) if token and index not in configured_slots]
    for index in missing_phones:
        _record_account_summary(f"slot {index}", "FAILED: Token has no configured phone")
    if not targets and not missing_phones:
        print("xueqiu_relogin_skipped reason=no_configured_accounts")
        return True
    proxy = os.environ.get("XUEQIU_PROXY")
    validation_timeout = int(os.environ.get("XUEQIU_RELOGIN_VERIFY_TIMEOUT", "20"))
    success = not missing_phones
    for token_index, phone, recipient in targets:
        print(f"xueqiu_account_check token_index={token_index} phone_suffix={phone[-4:]}")
        try:
            token = tokens[token_index] if token_index < len(tokens) else ""
            if not token:
                raise AuthenticationFailed("Missing token")
            username = verify_token_account(
                token, phone, proxy=proxy, timeout=validation_timeout,
            )
            if not recipient:
                _record_account_summary(phone, f"FAILED: Missing QR login email recipient (verified as {username})")
                success = False
            else:
                _record_account_summary(phone, username)
            print(f"xueqiu_account_check_success phone_suffix={phone[-4:]} username={username}")
            continue
        except AuthenticationFailed as exc:
            reason = str(exc)
            print(f"xueqiu_account_check_failed phone_suffix={phone[-4:]} reason={reason}")
        except Exception as exc:
            detail = str(exc) if isinstance(exc, IdentityUnavailable) else type(exc).__name__
            _record_account_summary(phone, f"FAILED: Account verification: {detail}")
            success = False
            continue

        try:
            if not recipient:
                raise ReloginFailed("Missing QR login email recipient")
            print(f"xueqiu_relogin_start token_index={token_index} phone_suffix={phone[-4:]}")
            username = playwright_relogin(
                phone,
                token_index=token_index,
                recipient=recipient,
                env_path=env_path,
                profile_dir=profile_dir,
                total_timeout_seconds=total_timeout_seconds,
            )
            if username is None:
                raise ReloginFailed("QR login timed out")
        except Exception as exc:
            detail = str(exc) if isinstance(exc, ReloginFailed) else f"Browser setup or cleanup failed ({type(exc).__name__})"
            _record_account_summary(phone, f"FAILED: {reason}. Relogin: {detail}")
            success = False
            continue
        _record_account_summary(phone, username)
    return success


def _dotenv_entries(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() and not key.lstrip().startswith("#"):
                entries.setdefault(key.strip(), value.strip().strip("\"'"))
    return entries


def _credential_slots(entries) -> list[str]:
    value = entries.get("XUEQIU_TOKENS") or entries.get("XUEQIU_TOKEN")
    return _split_slots(value) if value else []


def _slot(slots: list[str], index: int) -> str:
    return slots[index] if index < len(slots) else ""


def _set_slot(slots: list[str], index: int, value: str) -> None:
    slots.extend([""] * (index + 1 - len(slots)))
    slots[index] = value


def _publish_tokens(slots: list[str]) -> None:
    os.environ["XUEQIU_TOKENS"] = ",".join(slots)
    os.environ["XUEQIU_TOKEN"] = _slot(slots, 0)


def watch_token_file(path: Path) -> Callable[[], None]:
    """Keep startup overrides until the corresponding file slot changes."""
    previous = _credential_slots(_dotenv_entries(path))

    def reload() -> None:
        nonlocal previous
        current = _credential_slots(_dotenv_entries(path))
        if current == previous:
            return
        active = _token_values()
        for index in range(max(len(previous), len(current))):
            if _slot(current, index) != _slot(previous, index):
                _set_slot(active, index, _slot(current, index))
        _publish_tokens(active)
        previous = current

    return reload


@contextlib.contextmanager
def _token_write_lock(path: Path):
    lock_dir = path.parent / ".tmp"
    lock_dir.mkdir(exist_ok=True)
    with (lock_dir / (path.name + ".lock")).open("a+b") as lock:
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _copy_token_file_permissions(source: Path, target: Path) -> None:
    if not source.exists():
        return
    if os.name != "nt":
        target.chmod(stat.S_IMODE(source.stat().st_mode))
        return

    from ctypes import wintypes

    security = ctypes.WinDLL("advapi32", use_last_error=True)
    get_security = security.GetFileSecurityW
    get_security.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    set_security = security.SetFileSecurityW
    set_security.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p)
    get_control = security.GetSecurityDescriptorControl
    get_control.argtypes = (
        ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD),
    )
    dacl = 0x4
    needed = wintypes.DWORD()
    get_security(str(source), dacl, None, 0, ctypes.byref(needed))
    if not needed.value:
        raise ctypes.WinError(ctypes.get_last_error())
    descriptor = ctypes.create_string_buffer(needed.value)
    if not get_security(str(source), dacl, descriptor, needed, ctypes.byref(needed)):
        raise ctypes.WinError(ctypes.get_last_error())
    control = wintypes.WORD()
    revision = wintypes.DWORD()
    if not get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)):
        raise ctypes.WinError(ctypes.get_last_error())
    inheritance = 0x80000000 if control.value & 0x1000 else 0x20000000
    if not set_security(str(target), dacl | inheritance, descriptor):
        raise ctypes.WinError(ctypes.get_last_error())


def replace_token_in_env(token: str, token_index: int = 0, env_path: str | Path = ".env") -> None:
    path = Path(env_path).resolve()
    with _token_write_lock(path):
        file_slots = _credential_slots(_dotenv_entries(path))
        active_slots = _token_values() or file_slots.copy()
        _set_slot(file_slots, token_index, token)
        _set_slot(active_slots, token_index, token)
        output = []
        pool_written = False
        for line in path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []:
            key, separator, _ = line.partition("=")
            if separator and key.strip() == "XUEQIU_TOKENS":
                if not pool_written:
                    output.append("XUEQIU_TOKENS=" + ",".join(file_slots))
                    pool_written = True
            elif separator and key.strip() == "XUEQIU_TOKEN" and token_index == 0:
                output.append("XUEQIU_TOKEN=" + token)
            else:
                output.append(line)
        if not pool_written:
            output.append("XUEQIU_TOKENS=" + ",".join(file_slots))
        fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        os.close(fd)
        temporary = Path(name)
        try:
            _copy_token_file_permissions(path, temporary)
            temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        _publish_tokens(active_slots)


def log_progress(stage: str, **details: object) -> None:
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    fields = " ".join(f"{key}={value}" for key, value in details.items())
    print(f"{timestamp} stage={stage} {fields}".rstrip(), flush=True)


def _session_cookie(context) -> str:
    return next(
        (item["value"] for item in context.cookies([XUEQIU_ORIGIN]) if item["name"] == "xq_a_token"),
        "",
    )


def session_token(
    context, phone: str, *, timeout_ms: int, deadline: float | None = None, reject_session: bool = False,
) -> tuple[str, str] | None:
    from playwright.sync_api import Error

    def request_json(url: str) -> dict:
        timeout = _request_timeout(timeout_ms / 1000, deadline)
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
        _account_username(request_json, phone)
        token = _session_cookie(context)
        if not token:
            return None
        username = verify_token_account(
            token, phone, proxy=os.environ.get("XUEQIU_PROXY"),
            timeout=timeout_ms / 1000, deadline=deadline,
        )
    except AccountIdentityMismatch:
        raise
    except AuthenticationFailed:
        if reject_session:
            raise
        return None
    return token, username


def _open_qr_login(page) -> None:
    page.goto(f"{XUEQIU_ORIGIN}/", wait_until="domcontentloaded", timeout=30_000)
    page.get_by_text("二维码登录", exact=True).filter(visible=True).first.click(timeout=30_000)


def _qr_wrapper(page, deadline: float | None = None):
    wrapper = page.locator('[class*="index_qrcode__wrapper"]').first
    wrapper.wait_for(state="visible", timeout=_request_timeout(20, deadline) * 1000)
    return wrapper


def _capture_qr(page, output_dir: Path, sequence: int, *, deadline: float | None = None) -> Path:
    wrapper = _qr_wrapper(page, deadline)
    canvas = wrapper.locator("canvas").first
    canvas.wait_for(state="visible", timeout=_request_timeout(20, deadline) * 1000)
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
        timeout=_request_timeout(20, deadline) * 1000,
    )
    box = canvas.bounding_box(timeout=_request_timeout(20, deadline) * 1000)
    if box is None:
        raise RuntimeError("Xueqiu login QR code bounds are unavailable")
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"qrcode-{sequence}.png"
    page.screenshot(
        path=str(path),
        timeout=_request_timeout(20, deadline) * 1000,
        clip={
            "x": max(0, box["x"] - 16),
            "y": max(0, box["y"] - 16),
            "width": box["width"] + 32,
            "height": box["height"] + 32,
        },
    )
    os.chmod(path, 0o600)
    return path


def _qr_state(page, *, deadline: float | None = None) -> str:
    try:
        return _qr_wrapper(page, deadline).inner_text(timeout=_request_timeout(1, deadline) * 1000).strip()
    except Exception:
        return ""


def _refresh_qr(page, *, deadline: float | None = None) -> None:
    previous_image = _qr_wrapper(page, deadline).locator("canvas").first.evaluate(
        "canvas => canvas.toDataURL()", timeout=_request_timeout(20, deadline) * 1000,
    )
    refresh = page.get_by_text("点击刷新二维码", exact=True).filter(visible=True).first
    try:
        refresh.click(timeout=_request_timeout(2, deadline) * 1000)
    except Exception:
        page.reload(wait_until="domcontentloaded", timeout=_request_timeout(30, deadline) * 1000)
        page.get_by_text("二维码登录", exact=True).filter(visible=True).first.click(
            timeout=_request_timeout(30, deadline) * 1000,
        )
    page.wait_for_function(
        """previous => {
            const wrapper = document.querySelector('[class*="index_qrcode__wrapper"]');
            if (!wrapper || /刷新二维码|失效/.test(wrapper.innerText)) return false;
            const canvas = wrapper.querySelector('canvas');
            return canvas && canvas.toDataURL() !== previous;
        }""",
        arg=previous_image,
        timeout=_request_timeout(20, deadline) * 1000,
    )


def playwright_relogin(
    phone: str,
    *,
    token_index: int = 0,
    recipient: str | None = None,
    env_path: str | Path = ".env",
    profile_dir: str | Path | None = None,
    total_timeout_seconds: int = 600,
) -> str | None:
    from playwright.sync_api import sync_playwright

    log_progress("started", phone_suffix=phone[-4:], token_index=token_index,
                 timeout_seconds=total_timeout_seconds, login_mode="qr_email")
    if total_timeout_seconds <= 0:
        raise ValueError("Login timeout must be positive")
    if not recipient:
        raise ValueError("QR login email recipient is required")
    proxy = os.environ.get("XUEQIU_PROXY")
    profile_root = Path(profile_dir) if profile_dir is not None else Path(env_path).resolve().parent / DEFAULT_PROFILE_DIR
    account_key = hashlib.sha256(phone.encode("utf-8")).hexdigest()
    account_profile = profile_root / f"account-{account_key}"
    account_profile.mkdir(parents=True, exist_ok=True)
    run_dir = AUTH_ROOT / "qr-email" / f"account-{account_key}"
    run_dir.mkdir(parents=True, exist_ok=True)
    sequence = 0
    seen_message_ids: set[bytes] = set()
    next_mail_poll = 0.0
    qr_expired = False

    with sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(
                str(account_profile),
                channel="chrome",
                headless=False,
                proxy={"server": proxy} if proxy else None,
            )
        except Exception as exc:
            raise ReloginFailed(f"Launching the browser profile failed ({type(exc).__name__})") from exc
        stage = "browser session verification"
        try:
            try:
                verified = session_token(context, phone, timeout_ms=30_000, reject_session=True)
            except AuthenticationFailed:
                stage = "discarding the existing session"
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
                verified = None
            if verified is not None:
                token, username = verified
                stage = "saving the verified token"
                replace_token_in_env(token, token_index=token_index, env_path=env_path)
                log_progress("completed", result="SUCCESS", session="reused", token_saved=True)
                print("xueqiu_relogin_success=1", flush=True)
                return username

            stage = "opening QR login"
            page = context.pages[-1] if context.pages else context.new_page()
            _open_qr_login(page)
            sequence = 1
            qr_path = _capture_qr(page, run_dir, sequence)
            stage = "sending the QR login email"
            try:
                request_id, outbound_message_id = send_qr_email(phone, recipient, qr_path, sequence=sequence)
            finally:
                qr_path.unlink(missing_ok=True)
            qr_started_at = time.monotonic()
            deadline = qr_started_at + total_timeout_seconds
            log_progress("qr_email_sent", sequence=sequence, request_id=request_id)

            next_identity_poll = 0.0
            last_identity_error = None
            last_mail_error = None
            while time.monotonic() < deadline:
                stage = "waiting for QR login"
                page.wait_for_timeout(min(1000, max(0, (deadline - time.monotonic()) * 1000)))
                if time.monotonic() >= deadline:
                    break
                verified = None
                if time.monotonic() >= next_identity_poll:
                    stage = "verifying the scanned account"
                    try:
                        verified = session_token(context, phone, timeout_ms=15_000, deadline=deadline)
                        last_identity_error = None
                    except IdentityUnavailable as exc:
                        last_identity_error = str(exc)
                        log_progress("identity_request_retry", reason=last_identity_error)
                    next_identity_poll = time.monotonic() + 5
                if time.monotonic() >= deadline:
                    break
                if verified is not None:
                    token, username = verified
                    stage = "saving the verified token"
                    replace_token_in_env(token, token_index=token_index, env_path=env_path)
                    log_progress("completed", result="SUCCESS", session="qr", token_saved=True)
                    print("xueqiu_relogin_success=1", flush=True)
                    return username

                state = _qr_state(page, deadline=deadline)
                if not qr_expired and ("刷新二维码" in state or "失效" in state):
                    qr_expired = True
                    log_progress("qr_expired", sequence=sequence,
                                 qr_age_seconds=round(time.monotonic() - qr_started_at, 1))

                if time.monotonic() >= next_mail_poll:
                    stage = "checking for a QR refresh reply"
                    next_mail_poll = time.monotonic() + 5
                    try:
                        refresh_id = poll_refresh_request(
                            request_id=request_id, outbound_message_id=outbound_message_id,
                            seen_message_ids=seen_message_ids, deadline=deadline,
                        )
                    except (OSError, imaplib.IMAP4.error, IdentityUnavailable) as exc:
                        last_mail_error = type(exc).__name__
                        log_progress("refresh_mail_retry", error_type=last_mail_error)
                        continue
                    if refresh_id is not None:
                        stage = "refreshing the QR login email"
                        if time.monotonic() >= deadline:
                            break
                        try:
                            _refresh_qr(page, deadline=deadline)
                            if time.monotonic() >= deadline:
                                break
                            sequence += 1
                            qr_path = _capture_qr(page, run_dir, sequence, deadline=deadline)
                            try:
                                if time.monotonic() >= deadline:
                                    break
                                request_id, outbound_message_id = send_qr_email(
                                    phone, recipient, qr_path, sequence=sequence, deadline=deadline,
                                )
                            finally:
                                qr_path.unlink(missing_ok=True)
                        except Exception:
                            if time.monotonic() >= deadline:
                                break
                            raise
                        qr_started_at = time.monotonic()
                        qr_expired = False
                        log_progress("qr_refresh_email_sent", sequence=sequence, request_id=request_id)

            log_progress("completed", result="FAILED", reason="qr_login_timeout", timeout_seconds=total_timeout_seconds)
            print("xueqiu_relogin_success=0", flush=True)
            details = []
            if last_identity_error:
                details.append(f"identity verification: {last_identity_error}")
            if last_mail_error:
                details.append(f"refresh mailbox: {last_mail_error}")
            if details:
                raise ReloginFailed("QR login timed out. " + ". ".join(details))
            return None
        except ReloginFailed:
            raise
        except Exception as exc:
            log_progress("completed", result="FAILED", stage_name=stage, error_type=type(exc).__name__)
            print("xueqiu_relogin_success=0", flush=True)
            detail = str(exc) if isinstance(exc, (AccountIdentityMismatch, IdentityUnavailable)) else type(exc).__name__
            raise ReloginFailed(f"{stage.capitalize()} failed: {detail}") from exc
        finally:
            try:
                context.close()
            except Exception as exc:
                log_progress("browser_cleanup_failed", error_type=type(exc).__name__)


def load_dotenv(path: str | Path) -> None:
    for key, value in _dotenv_entries(Path(path)).items():
        os.environ.setdefault(key, value)


def _split_slots(value: str) -> list[str]:
    return [item.strip() for item in re.split("[,;]", value)]


def _ordered_values(name: str) -> list[str]:
    value = os.environ.get(name, "")
    return _split_slots(value) if value else []


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a manual email-assisted Xueqiu re-login.")
    parser.add_argument("phone", nargs="?", help="Xueqiu login phone number")
    parser.add_argument(
        "--check-accounts",
        action="store_true",
        help=(
            "Verify each account. Request QR login for missing or rejected tokens or a phone mismatch "
            "using the configured account recipient. Report service errors and ambiguous identities."
        ),
    )
    parser.add_argument("--token-index", type=int, help="Token position in XUEQIU_TOKENS (zero-based)")
    parser.add_argument("--recipient", help="QR login email recipient for manual single-account login")
    parser.add_argument("--env-path", default=".env", help="Project dotenv path")
    parser.add_argument("--profile-dir", help="Root directory for persistent per-account login profiles")
    parser.add_argument("--timeout", type=int, default=600, help="Maximum login wait in seconds")
    return parser.parse_args(argv)


def resolve_login_target(args: argparse.Namespace) -> tuple[int, str | None]:
    phones = _ordered_values("XUEQIU_LOGIN_PHONES")
    recipients = _ordered_values("XUEQIU_LOGIN_EMAILS")
    matches = [index for index, phone in enumerate(phones) if phone == args.phone]
    if len(matches) > 1:
        raise ValueError(f"Phone appears more than once in XUEQIU_LOGIN_PHONES: {args.phone}")
    if args.token_index is not None:
        token_index = args.token_index
        if matches and token_index != matches[0]:
            raise ValueError("--token-index does not match the configured phone position")
    elif matches:
        token_index = matches[0]
    else:
        raise ValueError("Phone is not configured; provide --token-index and --recipient")
    if token_index < 0:
        raise ValueError("--token-index must be non-negative")
    recipient = args.recipient
    if not matches:
        if _slot(phones, token_index) or _slot(_token_values(), token_index):
            raise ValueError("Token slot is already assigned to another configured phone")
        if not recipient:
            raise ValueError("Unconfigured phones require an explicit --recipient")
    if recipient is None and token_index < len(recipients):
        recipient = recipients[token_index]
    if not recipient:
        raise ValueError("No recipient configured; provide --recipient")
    return token_index, recipient


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_dotenv(args.env_path)
    if args.check_accounts:
        if args.phone or args.token_index is not None or args.recipient:
            raise ValueError("--check-accounts cannot be combined with phone, --token-index, or --recipient")
        return 0 if scheduled_relogin(
            total_timeout_seconds=args.timeout,
            env_path=args.env_path,
            profile_dir=args.profile_dir,
        ) else 1
    if not args.phone:
        raise ValueError("phone is required unless --check-accounts is used")
    token_index, recipient = resolve_login_target(args)
    try:
        username = playwright_relogin(
            args.phone,
            token_index=token_index,
            recipient=recipient,
            env_path=args.env_path,
            profile_dir=args.profile_dir,
            total_timeout_seconds=args.timeout,
        )
    except ReloginFailed as exc:
        print(f"Xueqiu relogin failed: {exc}")
        return 1
    return 0 if username else 1


if __name__ == "__main__":
    raise SystemExit(main())

