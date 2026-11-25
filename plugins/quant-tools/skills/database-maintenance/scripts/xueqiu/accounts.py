"""Named Xueqiu accounts stored in the project dotenv file."""

from __future__ import annotations

import contextlib
import ctypes
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import SplitResult, urlsplit

ACCOUNT_PREFIX = "XUEQIU_ACCOUNT_"
LEGACY_KEYS = {
    "XUEQIU_TOKENS", "XUEQIU_TOKEN", "XUEQIU_LOGIN_PHONES", "XUEQIU_LOGIN_EMAILS",
    "XUEQIU_CLASH_NODE_INDICES",
}


@dataclass(frozen=True)
class XueqiuAccount:
    id: str
    phone: str
    qr_email: str
    proxy_url: str | None = field(repr=False)
    token: str = field(repr=False)

    @property
    def key(self) -> str:
        return ACCOUNT_PREFIX + self.id


def parse_proxy_url(value: str, setting: str) -> SplitResult:
    try:
        url = urlsplit(value)
        if (
            url.scheme == "http" and url.hostname and not re.search(r"\s", value)
            and url.path in {"", "/"} and not url.query and not url.fragment
            and (url.port is None or url.port > 0)
        ):
            return url
    except ValueError:
        pass
    raise ValueError(f"{setting} requires an HTTP proxy URL (http://host:port)")


def _parse_accounts(lines: list[str]) -> dict[str, XueqiuAccount]:
    entries: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key or key.startswith("#"):
            continue
        if key in entries and key.startswith(ACCOUNT_PREFIX):
            raise ValueError(f"Duplicate Xueqiu configuration key: {key}")
        value = value.strip()
        if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
            value = value[1:-1]
        entries.setdefault(key, value)

    accounts: dict[str, XueqiuAccount] = {}
    phones: set[str] = set()
    tokens: set[str] = set()
    for key, value in entries.items():
        if not key.startswith(ACCOUNT_PREFIX):
            continue
        account_id = key[len(ACCOUNT_PREFIX):]
        if not re.fullmatch(r"[A-Z0-9_]+", account_id):
            raise ValueError("Xueqiu account IDs must contain uppercase letters, digits or underscores")
        parts = [part.strip() for part in value.split(",", 3)]
        if len(parts) != 4:
            raise ValueError(f"{key} must contain phone, QR email, proxy URL and token")
        phone, email, proxy_url, token = parts
        if not re.fullmatch(r"[0-9]{4,}", phone) or not re.fullmatch(r"[^@\s]+@[^@\s]+", email):
            raise ValueError(f"{key} requires a valid phone and QR email")
        if proxy_url:
            parse_proxy_url(proxy_url, key)
        if phone in phones or (token and token in tokens):
            raise ValueError(f"{key} duplicates another account phone or token")
        phones.add(phone)
        tokens.add(token)
        accounts[account_id] = XueqiuAccount(
            account_id, phone, email, proxy_url or None, token,
        )
    if not accounts and any(entries.get(key) or os.environ.get(key) for key in LEGACY_KEYS):
        raise ValueError("Migrate the legacy Xueqiu lists to XUEQIU_ACCOUNT_<ID> records")
    return accounts


def load_accounts(path: str | Path = ".env") -> dict[str, XueqiuAccount]:
    path = Path(path)
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    return _parse_accounts(lines)


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


def replace_account_token(path: str | Path, account: XueqiuAccount, token: str) -> None:
    path = Path(path).resolve()
    if not token or "\n" in token or "\r" in token:
        raise ValueError("Invalid replacement Xueqiu token")
    with _token_write_lock(path):
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig")
        lines = text.splitlines(keepends=True)
        accounts = _parse_accounts(lines)
        if accounts.get(account.id) != account:
            raise ValueError(f"{account.key} changed during login")
        if any(other.id != account.id and other.token == token for other in accounts.values()):
            raise ValueError(f"{account.key} received another account's token")

        for index, line in enumerate(lines):
            if line.partition("=")[0].strip() != account.key:
                continue
            value = line.rstrip("\r\n")
            newline = line[len(value):]
            phone, email, proxy_url, old_token = value.split(",", 3)
            _, _, raw_value = line.partition("=")
            quote = raw_value.lstrip()[:1]
            suffix = old_token[len(old_token.rstrip()):]
            previous = old_token.rstrip()
            if quote in {"'", '"'} and previous.endswith(quote):
                previous = previous[:-1]
                suffix = quote + suffix
            padding = previous[:len(previous) - len(previous.lstrip())]
            content = previous[len(padding):]
            suffix = content[len(content.rstrip()):] + suffix
            lines[index] = f"{phone},{email},{proxy_url},{padding}{token}{suffix}{newline}"
            break

        fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        os.close(fd)
        temporary = Path(name)
        try:
            _copy_token_file_permissions(path, temporary)
            encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
            temporary.write_bytes("".join(lines).encode(encoding))
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
