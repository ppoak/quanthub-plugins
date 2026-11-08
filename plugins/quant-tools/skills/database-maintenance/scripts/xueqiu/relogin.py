"""Run a manual email-assisted Xueqiu re-login."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from email_relogin import DEFAULT_PROFILE_DIR, playwright_relogin


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
    parser.add_argument("phone", help="Xueqiu login phone number")
    parser.add_argument("--token-index", type=int, help="Token position in XUEQIU_TOKENS (zero-based)")
    parser.add_argument("--recipient", help="Email address used for the verification-code reply")
    parser.add_argument("--env-path", default=".env", help="Project dotenv path")
    parser.add_argument("--profile-dir", help="Persistent Playwright browser profile directory")
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
