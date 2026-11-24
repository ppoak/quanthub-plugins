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
        (index, phone, recipients[index])
        for index, phone in enumerate(phones)
        if index < len(recipients)
    ]


def _request_json(url: str, token: str, *, proxy: str | None, timeout: int) -> dict:
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
    opener = urllib.request.build_opener(proxy_handler)
    try:
        with opener.open(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {}
        code = payload.get("error_code")
        raise RuntimeError(
            f"identity endpoint HTTP {exc.code}"
            + (f" error_code={code}" if code is not None else "")
        ) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"identity endpoint request failed: {type(exc).__name__}") from exc


def verify_token_account(
    token: str, phone: str, *, proxy: str | None, timeout: int,
) -> str:
    account = _request_json(ACCOUNT_BIND_URL, token, proxy=proxy, timeout=timeout)
    if account.get("error_code"):
        raise RuntimeError(f"identity endpoint error_code={account['error_code']}")
    telephone = str(account.get("telephone") or "").strip()
    pattern = re.escape(telephone).replace(r"\*", r"\d")
    if not telephone or sum(char.isdigit() for char in telephone) < 4:
        raise RuntimeError("identity endpoint returned no phone")
    if not re.fullmatch(pattern, phone):
        raise AccountIdentityMismatch("Xueqiu account phone does not match the login target")
    profile = _request_json(USER_SHOW_URL, token, proxy=proxy, timeout=timeout)
    if profile.get("error_code"):
        raise RuntimeError(f"user endpoint error_code={profile['error_code']}")
    username = str(profile.get("screen_name") or profile.get("name") or "").strip()
    if not username:
        raise RuntimeError("user endpoint returned no username")
    return username


def _record_account_summary(phone: str, value: str) -> None:
    try:
        from scripts.common.scheduler import record_task_output
    except ImportError:
        return
    record_task_output(f"Xueqiu account {phone[-4:]}", value, None, operation="account")


def _token_values() -> list[str]:
    values = os.environ.get("XUEQIU_TOKENS", "").replace(";", ",")
    return [item.strip() for item in values.split(",") if item.strip()]


def scheduled_relogin(
    *, total_timeout_seconds: int = 600,
    env_path: str | Path = ".env", profile_dir: str | Path | None = None,
) -> str | None:
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
            token = tokens[token_index]
            username = verify_token_account(
                token, phone, proxy=proxy, timeout=validation_timeout,
            )
            _record_account_summary(phone, username)
            print(f"xueqiu_account_check_success phone_suffix={phone[-4:]} username={username}")
            continue
        except (IndexError, AccountIdentityMismatch, RuntimeError) as exc:
            reason = str(exc)
            print(f"xueqiu_account_check_failed phone_suffix={phone[-4:]} reason={reason}")

        print(f"xueqiu_relogin_start token_index={token_index} phone_suffix={phone[-4:]}")
        relogin_username = playwright_relogin(
            phone,
            token_index=token_index,
            recipient=recipient,
            env_path=env_path,
            profile_dir=profile_dir,
            total_timeout_seconds=total_timeout_seconds,
        )
        if not relogin_username:
            _record_account_summary(phone, f"FAILED: {reason}; relogin did not complete")
            success = False
        else:
            _record_account_summary(phone, relogin_username)
    return success


def replace_token_in_env(token: str, token_index: int = 0, env_path: str | Path = ".env") -> None:
    path = Path(env_path)
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    values: list[str] | None = None
    output: list[str] = []
    for line in lines:
        if line.startswith("XUEQIU_TOKENS="):
            raw_values = line.split("=", 1)[1].strip().strip("\"'")
            values = [item.strip() for item in raw_values.split(",")]
            while len(values) <= token_index:
                values.append("")
            values[token_index] = token
            line = "XUEQIU_TOKENS=" + ",".join(values)
        elif token_index == 0 and line.startswith("XUEQIU_TOKEN="):
            line = "XUEQIU_TOKEN=" + token
        output.append(line)
    if values is None:
        values = [""] * (token_index + 1)
        values[token_index] = token
        output.append("XUEQIU_TOKENS=" + ",".join(values))
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
    temporary.replace(path)


def log_progress(stage: str, **details: object) -> None:
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    fields = " ".join(f"{key}={value}" for key, value in details.items())
    print(f"{timestamp} stage={stage} {fields}".rstrip(), flush=True)


class AccountIdentityMismatch(RuntimeError):
    """The authenticated account does not match the requested phone."""


def session_token(context, phone: str, *, timeout_ms: int) -> tuple[str, str] | None:
    """Return a token only when the current session belongs to the target phone."""
    from playwright.sync_api import Error, TimeoutError

    cookies = context.cookies(["https://xueqiu.com"])
    token = next((item["value"] for item in cookies if item["name"] == "xq_a_token"), "")
    if not token:
        return None
    try:
        identity = context.request.get(ACCOUNT_BIND_URL, timeout=timeout_ms)
    except Error as exc:
        network_error = re.search(
            r"\b(?:ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|ENETUNREACH|EHOSTUNREACH)\b",
            str(exc),
        )
        if not isinstance(exc, TimeoutError) and network_error is None:
            raise
        log_progress("identity_request_retry", error_type=type(exc).__name__,
                     network_error=network_error.group() if network_error else "TIMEOUT")
        return None
    if identity.status in {400, 401, 403}:
        return None
    if not identity.ok:
        raise RuntimeError("Xueqiu account identity request failed")
    account = identity.json()
    if account.get("error_code"):
        return None
    telephone = str(account.get("telephone") or "").strip()
    if not telephone:
        return None
    pattern = re.escape(telephone).replace(r"\*", r"\d")
    if sum(char.isdigit() for char in telephone) < 4 or not re.fullmatch(pattern, phone):
        raise AccountIdentityMismatch("Xueqiu account phone does not match the login target")
    profile = context.request.get(USER_SHOW_URL, timeout=timeout_ms)
    if not profile.ok:
        raise RuntimeError("Xueqiu user identity request failed")
    profile_data = profile.json()
    username = str(profile_data.get("screen_name") or profile_data.get("name") or "").strip()
    if not username:
        raise RuntimeError("Xueqiu user identity is missing username")
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
) -> bool:
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
        context = playwright.chromium.launch_persistent_context(
            str(account_profile),
            channel="chrome",
            headless=False,
            proxy={"server": proxy} if proxy else None,
        )
        try:
            verified = session_token(
                context, phone,
                timeout_ms=30_000,
            )
            if verified is not None:
                token, username = verified
                replace_token_in_env(token, token_index=token_index, env_path=env_path)
                log_progress("completed", result="SUCCESS", session="reused", token_saved=True)
                print("xueqiu_relogin_success=1", flush=True)
                return username

            page = context.pages[0] if context.pages else context.new_page()
            _open_qr_login(page)
            sequence = 1
            qr_path = _capture_qr(page, run_dir, sequence)
            request_id, _ = send_qr_email(phone, recipient, qr_path, sequence=sequence)
            qr_path.unlink(missing_ok=True)
            request_ids.add(request_id)
            qr_started_at = time.monotonic()
            deadline = qr_started_at + total_timeout_seconds
            log_progress("qr_email_sent", sequence=sequence, request_id=request_id)

            next_identity_poll = 0.0
            while time.monotonic() < deadline:
                page.wait_for_timeout(1_000)
                if time.monotonic() >= deadline:
                    break
                verified = None
                if time.monotonic() >= next_identity_poll:
                    verified = session_token(
                        context, phone,
                        timeout_ms=min(15_000, max(1, int((deadline - time.monotonic()) * 1_000))),
                    )
                    next_identity_poll = time.monotonic() + 5
                if verified is not None:
                    token, username = verified
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
                    next_mail_poll = time.monotonic() + 5
                    refresh_id = poll_refresh_request(
                        request_ids=request_ids,
                        seen_message_ids=seen_message_ids,
                    )
                    if refresh_id is not None:
                        _refresh_qr(page)
                        sequence += 1
                        qr_path = _capture_qr(page, run_dir, sequence)
                        request_id, _ = send_qr_email(phone, recipient, qr_path, sequence=sequence)
                        qr_path.unlink(missing_ok=True)
                        request_ids.add(request_id)
                        qr_started_at = time.monotonic()
                        qr_expired = False
                        log_progress("qr_refresh_email_sent", sequence=sequence, request_id=request_id)

            log_progress("completed", result="FAILED", reason="qr_login_timeout", timeout_seconds=total_timeout_seconds)
            print("xueqiu_relogin_success=0", flush=True)
            return None
        except AccountIdentityMismatch:
            log_progress("completed", result="FAILED", reason="account_identity_mismatch")
            print("xueqiu_relogin_success=0", flush=True)
            return None
        except Exception as exc:
            log_progress("completed", result="FAILED", reason="runtime_error", error_type=type(exc).__name__)
            print("xueqiu_relogin_success=0", flush=True)
            return None
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


def _ordered_values(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a manual email-assisted Xueqiu re-login.")
    parser.add_argument("phone", nargs="?", help="Xueqiu login phone number")
    parser.add_argument(
        "--check-accounts",
        action="store_true",
        help="Verify every configured account and relogin accounts whose identity check fails.",
    )
    parser.add_argument("--token-index", type=int, help="Token position in XUEQIU_TOKENS (zero-based)")
    parser.add_argument("--recipient", help="Email address used for the verification-code reply")
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
    if recipient is None:
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
    success = playwright_relogin(
        args.phone,
        token_index=token_index,
        recipient=recipient,
        env_path=args.env_path,
        profile_dir=args.profile_dir,
        total_timeout_seconds=args.timeout,
    )
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())

