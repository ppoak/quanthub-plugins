"""Email-assisted Xueqiu re-login helpers."""

from __future__ import annotations

import argparse
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
from pathlib import Path
from typing import Callable


AUTH_ROOT = Path(".tmp/database-maintenance/xueqiu-auth")
DEFAULT_PROFILE_DIR = AUTH_ROOT / "browser"
ACCOUNT_BIND_URL = "https://xueqiu.com/account/bind/show.json"
USER_SHOW_URL = "https://xueqiu.com/user/show.json"


def send_qr_email(
    phone: str,
    recipient: str,
    qr_path: str | Path,
    *,
    sequence: int,
) -> tuple[str, str]:
    """Send one newly generated QR image and return its request identifiers."""
    sender = os.environ["SENDER"]
    password = os.environ["EMAIL_PASSWORD"]
    server = os.environ["SMTP_SERVER"]
    port = int(os.environ.get("SMTP_PORT", "465"))
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    request_id = uuid.uuid4().hex[:8].upper()
    suffix = phone[-4:]
    message["Message-ID"] = make_msgid()
    message["Subject"] = f"Xueqiu QR login | {suffix} | {request_id} | {sequence}"
    message.set_content(
        "Please scan the attached Xueqiu login QR code with the Xueqiu App and confirm login.\n"
        "If the QR code has expired or you need a new one, reply to this email with a single line: 刷新\n"
        f"Request ID: {request_id}\nPhone suffix: {suffix}\n"
    )
    qr_file = Path(qr_path)
    message.add_attachment(
        qr_file.read_bytes(), maintype="image", subtype="png",
        filename=f"xueqiu-login-{suffix}-{sequence}.png",
    )
    use_ssl = os.environ.get("SMTP_USE_SSL", "1").lower() not in {"0", "false", "no"}
    if use_ssl:
        with smtplib.SMTP_SSL(server, port, timeout=30) as smtp:
            smtp.login(sender, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(server, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(sender, password)
            smtp.send_message(message)
    return request_id, message["Message-ID"] or ""


def _message_text(message) -> str:
    chunks: list[str] = []
    for part in message.walk():
        if part.get_content_type() not in {"text/plain", "text/html"}:
            continue
        if part.get_content_disposition() == "attachment":
            continue
        payload = part.get_payload(decode=True) or b""
        body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if part.get_content_type() == "text/html":
            body = re.sub(r"<[^>]+>", "\n", body)
        chunks.append(body)
    return "\n".join(chunks)


def _is_refresh_reply(body: str) -> bool:
    """Accept an explicit refresh command, excluding quoted email instructions."""
    return bool(re.search(r"(?m)^\s*(?:刷新|refresh|重新发送|重发二维码)\s*$", body, re.IGNORECASE))


def poll_refresh_request(
    *,
    request_ids: set[str],
    seen_message_ids: set[str],
) -> str | None:
    """Return the matching request id when a user replies with a refresh command."""
    username = os.environ.get("XUEQIU_IMAP_USERNAME", os.environ["SENDER"])
    password = os.environ.get("XUEQIU_IMAP_PASSWORD", os.environ["EMAIL_PASSWORD"])
    server = os.environ.get("XUEQIU_IMAP_SERVER", "imap.qq.com")
    port = int(os.environ.get("XUEQIU_IMAP_PORT", "993"))
    with imaplib.IMAP4_SSL(server, port, timeout=30) as mailbox:
        mailbox.login(username, password)
        mailbox.select("INBOX", readonly=True)
        candidates: set[bytes] = set()
        for request_id in request_ids:
            status, data = mailbox.search(None, "HEADER", "Subject", request_id)
            if status != "OK":
                raise RuntimeError("IMAP inbox search failed")
            candidates.update(data[0].split())
        for sequence_id in sorted(candidates, key=int, reverse=True):
            status, raw = mailbox.fetch(sequence_id, "(BODY.PEEK[])")
            if status != "OK":
                raise RuntimeError("IMAP message fetch failed")
            message = message_from_bytes(raw[0][1])
            message_id = message.get("Message-ID", "")
            if message_id and message_id in seen_message_ids:
                continue
            if message_id:
                seen_message_ids.add(message_id)
            subject = "".join(
                part.decode(charset or "utf-8", errors="replace") if isinstance(part, bytes) else part
                for part, charset in decode_header(message.get("Subject", ""))
            )
            thread = f"{message.get('In-Reply-To', '')} {message.get('References', '')}"
            matched_id = next(
                (request_id for request_id in request_ids
                 if re.search(rf"(?<![A-Za-z0-9]){re.escape(request_id)}(?![A-Za-z0-9])", subject)
                 or request_id in thread),
                None,
            )
            if matched_id and _is_refresh_reply(_message_text(message)):
                return matched_id
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
    try:
        from scripts.common.scheduler import record_task_output
    except ImportError:
        return
    record_task_output(f"Xueqiu account {phone[-4:]}", value, None, operation="account")


def _token_values() -> list[str]:
    return _ordered_values("XUEQIU_TOKENS") or _ordered_values("XUEQIU_TOKEN")


def scheduled_relogin(
    *, total_timeout_seconds: int = 600,
    env_path: str | Path = ".env", profile_dir: str | Path | None = None,
) -> bool:
    targets = configured_login_targets()
    if not targets:
        print("xueqiu_relogin_targets=0")
        return True
    tokens = _token_values()
    proxy = os.environ.get("XUEQIU_PROXY")
    validation_timeout = int(os.environ.get("XUEQIU_RELOGIN_VERIFY_TIMEOUT", "20"))
    success = True
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


def replace_token_in_env(token: str, token_index: int = 0, env_path: str | Path = ".env") -> None:
    path = Path(env_path)
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    values: list[str] | None = None
    legacy_token = ""
    output: list[str] = []
    for line in lines:
        if line.startswith("XUEQIU_TOKENS="):
            raw_values = line.split("=", 1)[1].strip().strip("\"'")
            values = _split_slots(raw_values)
            while len(values) <= token_index:
                values.append("")
            values[token_index] = token
            line = "XUEQIU_TOKENS=" + ",".join(values)
        elif line.startswith("XUEQIU_TOKEN="):
            legacy_token = line.split("=", 1)[1].strip().strip("\"'")
            if token_index == 0:
                line = "XUEQIU_TOKEN=" + token
        output.append(line)
    if values is None:
        values = [""] * (token_index + 1)
        values[0] = legacy_token
        values[token_index] = token
        output.append("XUEQIU_TOKENS=" + ",".join(values))
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
    temporary.replace(path)
    os.environ["XUEQIU_TOKENS"] = ",".join(values)
    if token_index == 0:
        os.environ["XUEQIU_TOKEN"] = token


def log_progress(stage: str, **details: object) -> None:
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    fields = " ".join(f"{key}={value}" for key, value in details.items())
    print(f"{timestamp} stage={stage} {fields}".rstrip(), flush=True)


def _session_cookie(context) -> str:
    return next(
        (item["value"] for item in context.cookies(["https://xueqiu.com"]) if item["name"] == "xq_a_token"),
        "",
    )


def session_token(
    context, phone: str, *, timeout_ms: int, deadline: float | None = None,
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
        return None
    return token, username


def _open_qr_login(page) -> None:
    page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=30_000)
    page.get_by_text("二维码登录", exact=True).filter(visible=True).first.click(timeout=30_000)


def _qr_wrapper(page):
    wrapper = page.locator('[class*="index_qrcode__wrapper"]').first
    wrapper.wait_for(state="visible", timeout=20_000)
    return wrapper


def _capture_qr(page, output_dir: Path, sequence: int) -> Path:
    wrapper = _qr_wrapper(page)
    canvas = wrapper.locator("canvas").first
    canvas.wait_for(state="visible", timeout=20_000)
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
        timeout=20_000,
    )
    box = canvas.bounding_box()
    if box is None:
        raise RuntimeError("Xueqiu login QR code bounds are unavailable")
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"qrcode-{sequence}.png"
    page.screenshot(
        path=str(path),
        clip={
            "x": max(0, box["x"] - 16),
            "y": max(0, box["y"] - 16),
            "width": box["width"] + 32,
            "height": box["height"] + 32,
        },
    )
    os.chmod(path, 0o600)
    return path


def _qr_state(page) -> str:
    try:
        return _qr_wrapper(page).inner_text(timeout=1_000).strip()
    except Exception:
        return ""


def _refresh_qr(page) -> None:
    previous_image = _qr_wrapper(page).locator("canvas").first.evaluate("canvas => canvas.toDataURL()")
    refresh = page.get_by_text("点击刷新二维码", exact=True).filter(visible=True).first
    try:
        refresh.click(timeout=2_000)
    except Exception:
        page.reload(wait_until="domcontentloaded", timeout=30_000)
        page.get_by_text("二维码登录", exact=True).filter(visible=True).first.click(timeout=30_000)
    page.wait_for_function(
        """previous => {
            const wrapper = document.querySelector('[class*="index_qrcode__wrapper"]');
            if (!wrapper || /刷新二维码|失效/.test(wrapper.innerText)) return false;
            const canvas = wrapper.querySelector('canvas');
            return canvas && canvas.toDataURL() !== previous;
        }""",
        arg=previous_image,
        timeout=20_000,
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
    request_ids: set[str] = set()
    seen_message_ids: set[str] = set()
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
                verified = session_token(context, phone, timeout_ms=30_000)
            except AccountIdentityMismatch:
                stage = "discarding the existing session"
                existing_pages = list(context.pages)
                page = context.new_page()
                for existing_page in existing_pages:
                    existing_page.close()
                context.clear_cookies()
                devtools = context.new_cdp_session(page)
                try:
                    devtools.send("Storage.clearDataForOrigin", {
                        "origin": "https://xueqiu.com", "storageTypes": "all",
                    })
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
                request_id, _ = send_qr_email(phone, recipient, qr_path, sequence=sequence)
            finally:
                qr_path.unlink(missing_ok=True)
            request_ids.add(request_id)
            qr_started_at = time.monotonic()
            deadline = qr_started_at + total_timeout_seconds
            log_progress("qr_email_sent", sequence=sequence, request_id=request_id)

            next_identity_poll = 0.0
            last_identity_error = None
            while time.monotonic() < deadline:
                stage = "waiting for QR login"
                page.wait_for_timeout(1_000)
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

                state = _qr_state(page)
                if not qr_expired and ("刷新二维码" in state or "失效" in state):
                    qr_expired = True
                    log_progress("qr_expired", sequence=sequence,
                                 qr_age_seconds=round(time.monotonic() - qr_started_at, 1))

                if time.monotonic() >= next_mail_poll:
                    stage = "checking for a QR refresh reply"
                    next_mail_poll = time.monotonic() + 5
                    refresh_id = poll_refresh_request(
                        request_ids=request_ids,
                        seen_message_ids=seen_message_ids,
                    )
                    if refresh_id is not None:
                        if time.monotonic() >= deadline:
                            break
                        stage = "refreshing the QR login email"
                        _refresh_qr(page)
                        sequence += 1
                        qr_path = _capture_qr(page, run_dir, sequence)
                        try:
                            request_id, _ = send_qr_email(phone, recipient, qr_path, sequence=sequence)
                        finally:
                            qr_path.unlink(missing_ok=True)
                        request_ids.add(request_id)
                        qr_started_at = time.monotonic()
                        qr_expired = False
                        log_progress("qr_refresh_email_sent", sequence=sequence, request_id=request_id)

            log_progress("completed", result="FAILED", reason="qr_login_timeout", timeout_seconds=total_timeout_seconds)
            print("xueqiu_relogin_success=0", flush=True)
            if last_identity_error:
                raise ReloginFailed(f"QR login timed out after identity verification failed: {last_identity_error}")
            return None
        except ReloginFailed:
            raise
        except Exception as exc:
            log_progress("completed", result="FAILED", stage_name=stage, error_type=type(exc).__name__)
            print("xueqiu_relogin_success=0", flush=True)
            detail = str(exc) if isinstance(exc, (AccountIdentityMismatch, IdentityUnavailable)) else type(exc).__name__
            raise ReloginFailed(f"{stage.capitalize()} failed: {detail}") from exc
        finally:
            context.close()


def load_dotenv(path: str | Path) -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


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
        help="Verify every configured account and relogin accounts whose identity check fails.",
    )
    parser.add_argument("--token-index", type=int, help="Token position in XUEQIU_TOKENS (zero-based)")
    parser.add_argument("--recipient", help="Recipient of the QR login email")
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

