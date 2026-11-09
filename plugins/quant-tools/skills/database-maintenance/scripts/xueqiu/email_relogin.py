"""Email-assisted Xueqiu re-login helpers."""

from __future__ import annotations

import argparse
import imaplib
import os
import re
import smtplib
import time
import uuid
import base64
import json
from datetime import datetime, timezone
from email import message_from_bytes
from email.header import decode_header
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path


CODE_PATTERN = re.compile(r"(?<!\d)(\d{4,6})(?!\d)")
AUTH_ROOT = Path(".tmp/database-maintenance/xueqiu-auth")
DEFAULT_PROFILE_DIR = AUTH_ROOT / "browser"
LOGIN_STATUS_PATH = AUTH_ROOT / "login_status.json"


def _jwt_expiry(id_token: str) -> int | None:
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp"))
    except (IndexError, TypeError, ValueError, json.JSONDecodeError):
        return None


def load_login_status(path: str | Path = LOGIN_STATUS_PATH) -> list[dict]:
    status_path = Path(path)
    if not status_path.exists():
        return []
    data = json.loads(status_path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("accounts"), list):
        return [item for item in data["accounts"] if isinstance(item, dict)]
    return [data] if isinstance(data, dict) and "token_index" in data else []


def save_login_status(*, token_index: int, phone: str, cookies: list[dict], status: str) -> None:
    AUTH_ROOT.mkdir(parents=True, exist_ok=True)
    id_token = next((item["value"] for item in cookies if item["name"] == "xq_id_token"), "")
    record = {
        "token_index": token_index,
        "phone_suffix": phone[-4:],
        "status": status,
        "last_verified_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "xq_id_token_exp": _jwt_expiry(id_token),
    }
    accounts = [item for item in load_login_status() if item.get("token_index") != token_index]
    accounts.append(record)
    accounts.sort(key=lambda item: int(item.get("token_index", 0)))
    data = {"accounts": accounts}
    temporary = LOGIN_STATUS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(LOGIN_STATUS_PATH)


def send_code_request(phone: str = "", recipient: str | None = None) -> tuple[str, str]:
    sender = os.environ["SENDER"]
    password = os.environ["EMAIL_PASSWORD"]
    server = os.environ["SMTP_SERVER"]
    port = int(os.environ.get("SMTP_PORT", "465"))
    recipient = recipient or os.environ["DATABASE_MAINTENANCE_EMAIL_TO"]
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    request_id = uuid.uuid4().hex[:8].upper()
    suffix = phone[-4:] if phone else "unknown"
    message_id = f"<{request_id.lower()}@xueqiu-auth.local>"
    message["Message-ID"] = message_id
    message["Subject"] = f"Xueqiu login verification code | {suffix} | {request_id}"
    message.set_content(
        "Reply to this email with only the latest Xueqiu verification code.\n"
        f"Request ID: {request_id}\nPhone suffix: {suffix}\n"
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
    return request_id, message_id


def wait_for_code(timeout_seconds: int = 600, *, request_id: str, message_id: str, reply_mailbox: str | None = None) -> str:
    username = os.environ.get("XUEQIU_IMAP_USERNAME", os.environ["SENDER"])
    password = os.environ.get("XUEQIU_IMAP_PASSWORD", os.environ["EMAIL_PASSWORD"])
    server = os.environ.get("XUEQIU_IMAP_SERVER", "imap.qq.com")
    port = int(os.environ.get("XUEQIU_IMAP_PORT", "993"))
    sender = reply_mailbox or os.environ.get("XUEQIU_AUTH_EMAIL_TO", os.environ["DATABASE_MAINTENANCE_EMAIL_TO"])
    deadline = time.monotonic() + timeout_seconds
    poll = 0
    while time.monotonic() < deadline:
        poll += 1
        log_progress("checking_inbox", poll=poll)
        with imaplib.IMAP4_SSL(server, port, timeout=30) as mailbox:
            mailbox.login(username, password)
            mailbox.select("INBOX", readonly=True)
            status, data = mailbox.search(None, "FROM", f'"{sender}"')
            if status != "OK":
                raise RuntimeError("IMAP inbox search failed")
            candidates = data[0].split()
            log_progress("inbox_checked", sender_messages=len(candidates))
            for sequence_id in reversed(candidates):
                status, raw = mailbox.fetch(sequence_id, "(BODY.PEEK[])")
                if status != "OK":
                    raise RuntimeError("IMAP message fetch failed")
                message = message_from_bytes(raw[0][1])
                if parseaddr(message.get("From", ""))[1].lower() != sender.lower():
                    continue
                subject = "".join(
                    part.decode(charset or "utf-8", errors="replace") if isinstance(part, bytes) else part
                    for part, charset in decode_header(message.get("Subject", ""))
                )
                thread = f"{message.get('In-Reply-To', '')} {message.get('References', '')}"
                if request_id not in subject and message_id not in thread:
                    continue
                log_progress("reply_matched", request_id=request_id)
                for part in message.walk():
                    if part.get_content_type() not in {"text/plain", "text/html"}:
                        continue
                    if part.get_content_disposition() == "attachment":
                        continue
                    payload = part.get_payload(decode=True) or b""
                    body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    if part.get_content_type() == "text/html":
                        body = re.sub(r"<[^>]+>", "\n", body)
                    # Only accept a code on its own line, never phone suffixes in quoted requests.
                    match = re.search(r"(?m)^\s*(\d{4,6})\s*$", body)
                    if match:
                        log_progress("verification_code_received")
                        return match.group(1)
                log_progress("reply_ignored", reason="no_standalone_verification_code")
        log_progress("waiting_for_next_poll", interval_seconds=15)
        time.sleep(min(15, max(0, deadline - time.monotonic())))
    raise TimeoutError("Timed out waiting for Xueqiu verification code email")


def request_code_then_wait(timeout_seconds: int = 600, *, recipient: str | None = None) -> str:
    request_id, message_id = send_code_request(recipient=recipient)
    time.sleep(30)
    return wait_for_code(timeout_seconds, request_id=request_id, message_id=message_id, reply_mailbox=recipient)


def configured_login_targets() -> list[tuple[int, str, str]]:
    phones = _ordered_values("XUEQIU_LOGIN_PHONES")
    recipients = _ordered_values("XUEQIU_LOGIN_EMAILS")
    return [
        (index, phone, recipients[index])
        for index, phone in enumerate(phones)
        if index < len(recipients)
    ]


def expiring_login_targets(threshold_days: int = 5) -> list[tuple[int, str, str]]:
    now = int(datetime.now(timezone.utc).timestamp())
    threshold = now + threshold_days * 24 * 60 * 60
    records = {int(item["token_index"]): item for item in load_login_status() if "token_index" in item}
    targets = []
    for token_index, phone, recipient in configured_login_targets():
        record = records.get(token_index)
        if not record:
            continue
        if record.get("status") == "relogin_required":
            targets.append((token_index, phone, recipient))
            continue
        expiry = record.get("xq_id_token_exp")
        if expiry is not None and int(expiry) <= threshold:
            targets.append((token_index, phone, recipient))
    return targets


def scheduled_relogin(*, threshold_days: int = 5, total_timeout_seconds: int = 600) -> bool:
    targets = expiring_login_targets(threshold_days)
    if not targets:
        print("xueqiu_relogin_targets=0")
        return True
    success = True
    for token_index, phone, recipient in targets:
        print(f"xueqiu_relogin_start token_index={token_index} phone_suffix={phone[-4:]}")
        if not playwright_relogin(
            phone,
            token_index=token_index,
            recipient=recipient,
            total_timeout_seconds=total_timeout_seconds,
        ):
            success = False
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


def playwright_relogin(
    phone: str,
    *,
    token_index: int = 0,
    recipient: str | None = None,
    env_path: str | Path = ".env",
    profile_dir: str | Path = DEFAULT_PROFILE_DIR,
    total_timeout_seconds: int = 600,
) -> bool:
    from playwright.sync_api import sync_playwright

    log_progress("started", phone_suffix=phone[-4:], token_index=token_index,
                 timeout_seconds=total_timeout_seconds)
    if total_timeout_seconds <= 0:
        raise ValueError("Login timeout must be positive")
    proxy = os.environ.get("XUEQIU_PROXY")
    attempt = 0
    deadline = time.monotonic() + total_timeout_seconds
    with sync_playwright() as playwright:
        while time.monotonic() < deadline:
            attempt += 1
            log_progress("sending_email", attempt=attempt)
            request_id, message_id = send_code_request(phone, recipient=recipient)
            log_progress("email_sent", request_id=request_id, sms_delay_seconds=30)
            time.sleep(min(30, max(0, deadline - time.monotonic())))
            remaining = max(1, int(deadline - time.monotonic()))
            log_progress("launching_browser")
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir),
                executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe",
                headless=False,
                proxy={"server": proxy} if proxy else None,
            )
            stage = "opening_login_page"
            log_progress(stage)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto("https://www.xueqiu.com/account/login", wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(1_500)
                stage = "filling_phone_and_agreement"
                log_progress(stage)
                page.locator("input[name=telephone]").first.fill(phone)
                agreement = page.locator("label.modal__login__automatic_new:visible").first
                agreement.locator("i").click()
                if "nochecked" in (agreement.locator("i").get_attribute("class") or ""):
                    raise RuntimeError("Xueqiu agreement checkbox was not checked")
                stage = "requesting_sms"
                log_progress(stage)
                page.get_by_text("发送验证码", exact=True).filter(visible=True).first.click()
                stage = "waiting_for_email_reply"
                log_progress(stage, remaining_seconds=remaining)
                try:
                    code = wait_for_code(remaining, request_id=request_id, message_id=message_id, reply_mailbox=recipient)
                except TimeoutError:
                    log_progress("failed", reason="email_reply_timeout")
                    break
                stage = "submitting_code"
                log_progress(stage)
                page.locator("input[placeholder=请输入验证码]:visible").first.fill(code)
                page.locator(".modal__login__btn:visible").first.click()
                page.wait_for_timeout(4_000)
                cookies = context.cookies(["https://xueqiu.com"])
                token = next((item["value"] for item in cookies if item["name"] == "xq_a_token"), "")
                if token:
                    stage = "saving_token"
                    log_progress(stage)
                    replace_token_in_env(token, token_index=token_index, env_path=env_path)
                    save_login_status(token_index=token_index, phone=phone, cookies=cookies, status="valid")
                    log_progress("completed", result="SUCCESS", token_saved=True)
                    print("xueqiu_relogin_success=1", flush=True)
                    return True
                log_progress("attempt_failed", reason="token_not_returned")
            except Exception as exc:
                log_progress("attempt_failed", failed_stage=stage, error_type=type(exc).__name__)

            finally:
                context.close()
            if time.monotonic() < deadline:
                log_progress("retrying", remaining_seconds=max(0, int(deadline - time.monotonic())))
    save_login_status(token_index=token_index, phone=phone, cookies=[], status="relogin_required")
    log_progress("completed", result="FAILED", reason="login_not_completed_before_deadline")
    print("xueqiu_relogin_success=0", flush=True)
    return False


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
        "--check-expiring",
        action="store_true",
        help="Relogin configured accounts whose ID token expires within the threshold.",
    )
    parser.add_argument("--token-index", type=int, help="Token position in XUEQIU_TOKENS (zero-based)")
    parser.add_argument("--recipient", help="Email address used for the verification-code reply")
    parser.add_argument("--env-path", default=".env", help="Project dotenv path")
    parser.add_argument("--profile-dir", help="Persistent Playwright browser profile directory")
    parser.add_argument("--timeout", type=int, default=600, help="Maximum login wait in seconds")
    parser.add_argument("--threshold-days", type=int, default=5, help="Expiry threshold for --check-expiring")
    return parser.parse_args(argv)


def resolve_login_target(args: argparse.Namespace) -> tuple[int, str | None]:
    phones = _ordered_values("XUEQIU_LOGIN_PHONES")
    recipients = _ordered_values("XUEQIU_LOGIN_EMAILS")
    matches = [index for index, phone in enumerate(phones) if phone == args.phone]
    if len(matches) > 1:
        raise ValueError(f"Phone appears more than once in XUEQIU_LOGIN_PHONES: {args.phone}")
    if args.token_index is not None:
        token_index = args.token_index
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
    if args.check_expiring:
        if args.phone or args.token_index is not None or args.recipient:
            raise ValueError("--check-expiring cannot be combined with phone, --token-index, or --recipient")
        return 0 if scheduled_relogin(
            threshold_days=args.threshold_days,
            total_timeout_seconds=args.timeout,
        ) else 1
    if not args.phone:
        raise ValueError("phone is required unless --check-expiring is used")
    token_index, recipient = resolve_login_target(args)
    profile_dir = args.profile_dir or str(Path(args.env_path).resolve().parent / DEFAULT_PROFILE_DIR)
    success = playwright_relogin(
        args.phone,
        token_index=token_index,
        recipient=recipient,
        env_path=args.env_path,
        profile_dir=profile_dir,
        total_timeout_seconds=args.timeout,
    )
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())

