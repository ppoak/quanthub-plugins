"""Email-assisted Xueqiu re-login helpers."""

from __future__ import annotations

import argparse
import ssl
import imaplib
import os
import re
import smtplib
import hashlib
import time
import uuid
from datetime import datetime
from email import message_from_bytes
from email.header import decode_header
from email.message import EmailMessage
from email.utils import make_msgid
from html.parser import HTMLParser
from pathlib import Path


from common.project import load_env_file

from xueqiu.accounts import XueqiuAccount, load_accounts, replace_account_token
from xueqiu.login import (
    AccountIdentityMismatch,
    AuthenticationFailed,
    IdentityUnavailable,
    browser_proxy_options,
    capture_qr,
    clear_browser_session,
    open_qr_login,
    qr_state,
    refresh_qr,
    request_timeout,
    session_token,
    verify_token_account,
)


AUTH_ROOT = Path(".tmp/database-maintenance/xueqiu-auth")
DEFAULT_PROFILE_DIR = AUTH_ROOT / "browser"


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
    timeout = request_timeout(30, deadline)
    connection = (
        smtplib.SMTP_SSL(server, port, timeout=timeout, context=tls)
        if use_ssl else smtplib.SMTP(server, port, timeout=timeout)
    )
    with connection as smtp:
        if not use_ssl:
            smtp.sock.settimeout(request_timeout(30, deadline))
            smtp.starttls(context=tls)
        smtp.sock.settimeout(request_timeout(30, deadline))
        smtp.login(sender, password)
        smtp.sock.settimeout(request_timeout(30, deadline))
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
        server, port, timeout=request_timeout(30, deadline), ssl_context=ssl.create_default_context(),
    ) as mailbox:
        mailbox.sock.settimeout(request_timeout(30, deadline))
        mailbox.login(username, password)
        mailbox.sock.settimeout(request_timeout(30, deadline))
        mailbox.select("INBOX", readonly=True)
        candidates: set[bytes] = set()
        for header, value in (
            ("Subject", request_id), ("In-Reply-To", outbound_message_id), ("References", outbound_message_id),
        ):
            mailbox.sock.settimeout(request_timeout(30, deadline))
            status, data = mailbox.uid("SEARCH", None, "HEADER", header, f'"{value}"')
            if status != "OK":
                raise imaplib.IMAP4.error("IMAP inbox search failed")
            candidates.update(data[0].split())
        for uid in sorted(candidates - seen_message_ids, key=int, reverse=True):
            mailbox.sock.settimeout(request_timeout(30, deadline))
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


class ReloginFailed(RuntimeError):
    """The login flow failed at a reported stage."""


def _report_account_failure(account: XueqiuAccount, reason: str) -> None:
    label = f"{account.id} ({account.phone[-4:]})"
    print(f"xueqiu_account_result account={label} FAILED: {reason}", flush=True)


def check_accounts(
    *, total_timeout_seconds: int = 600,
    env_path: str | Path = ".env", profile_dir: str | Path | None = None,
) -> bool:
    accounts = load_accounts(env_path)
    if not accounts:
        print("xueqiu_relogin_skipped reason=no_configured_accounts")
        return True
    proxy = os.environ.get("XUEQIU_PROXY")
    validation_timeout = int(os.environ.get("XUEQIU_RELOGIN_VERIFY_TIMEOUT", "20"))
    phones = {account.phone for account in accounts.values()}
    success = True
    for account in accounts.values():
        print(f"xueqiu_account_check account={account.id} phone_suffix={account.phone[-4:]}")
        try:
            if not account.token:
                raise AuthenticationFailed("Missing token")
            username = verify_token_account(
                account, proxy=proxy, timeout=validation_timeout, phones=phones,
            )
        except AuthenticationFailed as exc:
            reason = str(exc)
            try:
                print(f"xueqiu_relogin_start account={account.id} reason={reason}")
                username = playwright_relogin(
                    account, env_path=env_path, profile_dir=profile_dir,
                    total_timeout_seconds=total_timeout_seconds,
                )
                if username is None:
                    raise ReloginFailed("QR login timed out")
            except Exception as exc:
                detail = str(exc) if isinstance(exc, ReloginFailed) else f"Browser setup or cleanup failed ({type(exc).__name__})"
                _report_account_failure(account, f"{reason}. Relogin: {detail}")
                success = False
                continue
        except Exception as exc:
            detail = str(exc) if isinstance(exc, IdentityUnavailable) else type(exc).__name__
            _report_account_failure(account, f"Account verification: {detail}")
            success = False
            continue
        print(f"xueqiu_account_check_success account={account.id} username={username}")
    return success


def log_progress(stage: str, **details: object) -> None:
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    fields = " ".join(f"{key}={value}" for key, value in details.items())
    print(f"{timestamp} stage={stage} {fields}".rstrip(), flush=True)


def playwright_relogin(
    account: XueqiuAccount,
    *,
    env_path: str | Path = ".env",
    profile_dir: str | Path | None = None,
    total_timeout_seconds: int = 600,
) -> str | None:
    from playwright.sync_api import sync_playwright

    phone, recipient = account.phone, account.qr_email
    accounts = load_accounts(env_path)
    if accounts.get(account.id) != account:
        raise ReloginFailed(f"{account.key} changed before browser login")
    phones = {item.phone for item in accounts.values()}
    log_progress("started", phone_suffix=phone[-4:], account=account.id,
                 timeout_seconds=total_timeout_seconds, login_mode="qr_email")
    if total_timeout_seconds <= 0:
        raise ValueError("Login timeout must be positive")
    proxy = account.proxy_url or os.environ.get("XUEQIU_PROXY")
    browser_proxy = browser_proxy_options(proxy)
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
                proxy=browser_proxy,
                args=None if browser_proxy else ["--no-proxy-server"],
            )
        except Exception as exc:
            raise ReloginFailed(f"Launching the browser profile failed ({type(exc).__name__})") from exc
        stage = "browser session verification"
        try:
            try:
                verified = session_token(
                    context, account, timeout_ms=30_000, phones=phones, reject_session=True,
                )
            except AuthenticationFailed:
                stage = "discarding the existing session"
                clear_browser_session(context)
                verified = None
            if verified is not None:
                token, username = verified
                stage = "saving the verified token"
                replace_account_token(env_path, account, token)
                log_progress("completed", result="SUCCESS", session="reused", token_saved=True)
                print("xueqiu_relogin_success=1", flush=True)
                return username

            stage = "opening QR login"
            page = context.pages[-1] if context.pages else context.new_page()
            open_qr_login(page)
            sequence = 1
            qr_path = capture_qr(page, run_dir, sequence)
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
                        verified = session_token(
                            context, account, timeout_ms=15_000, phones=phones, deadline=deadline,
                        )
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
                    replace_account_token(env_path, account, token)
                    log_progress("completed", result="SUCCESS", session="qr", token_saved=True)
                    print("xueqiu_relogin_success=1", flush=True)
                    return username

                state = qr_state(page, deadline=deadline)
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
                            refresh_qr(page, deadline=deadline)
                            if time.monotonic() >= deadline:
                                break
                            sequence += 1
                            qr_path = capture_qr(page, run_dir, sequence, deadline=deadline)
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run an email-assisted Xueqiu re-login.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("phone", nargs="?", help="Registered Xueqiu login phone")
    target.add_argument("--account", help="Registered XUEQIU_ACCOUNT_<ID> suffix")
    target.add_argument(
        "--check-accounts", action="store_true",
        help="Verify each account. Request QR login for missing or rejected tokens or a phone mismatch.",
    )
    parser.add_argument("--project-root", default=".", help="Target project directory.")
    parser.add_argument("--env-path", default=".env", help="Dotenv path relative to the project directory, or an absolute path.")
    parser.add_argument("--profile-dir", help="Root directory for persistent per-account login profiles")
    parser.add_argument("--timeout", type=int, help="Maximum login wait in seconds (default: XUEQIU_RELOGIN_TIMEOUT or 600).")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    os.chdir(Path(args.project_root).expanduser())
    path = Path(args.env_path)
    load_env_file(path)
    try:
        timeout = args.timeout if args.timeout is not None else int(os.getenv("XUEQIU_RELOGIN_TIMEOUT", "600"))
        if args.check_accounts:
            return 0 if check_accounts(
                total_timeout_seconds=timeout, env_path=path, profile_dir=args.profile_dir,
            ) else 1
        accounts = load_accounts(path)
        account = (
            accounts.get(args.account) if args.account is not None else
            next((item for item in accounts.values() if item.phone == args.phone), None)
        )
        if account is None:
            raise ValueError("Xueqiu account is not registered in the project dotenv file")
        username = playwright_relogin(
            account, env_path=path, profile_dir=args.profile_dir, total_timeout_seconds=timeout,
        )
    except (ReloginFailed, ValueError) as exc:
        print(f"Xueqiu relogin failed: {exc}")
        return 1
    return 0 if username else 1


if __name__ == "__main__":
    raise SystemExit(main())
