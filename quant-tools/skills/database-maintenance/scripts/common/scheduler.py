from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import re
import smtplib
import time
import urllib.request
from email.message import EmailMessage
from contextvars import ContextVar
from html import escape
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo


EMAIL_RECIPIENT_ENV = "DATABASE_MAINTENANCE_EMAIL_TO"
DEFAULT_LOG_DIR = Path(".tmp/database-maintenance")
TASK_OUTPUTS: ContextVar[list[dict] | None] = ContextVar("task_outputs", default=None)


def record_task_output(table: str, path: str | Path, rows: int | None, operation: str = "upsert") -> None:
    """Record completed writes for the current task's notification summary."""
    outputs = TASK_OUTPUTS.get()
    if outputs is not None:
        outputs.append({"table": table, "path": str(path), "rows": rows, "operation": operation})


def scheduler_now(timezone_name: str | None = None) -> dt.datetime:
    """Return a naive wall-clock datetime in the configured business timezone."""
    name = timezone_name or os.getenv("DATABASE_MAINTENANCE_TIMEZONE", "Asia/Shanghai")
    try:
        zone = ZoneInfo(name)
    except Exception as exc:
        raise ValueError(f"Invalid DATABASE_MAINTENANCE_TIMEZONE: {name}") from exc
    return dt.datetime.now(dt.timezone.utc).astimezone(zone).replace(tzinfo=None)


WEEKDAYS = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}

SINA_TRADE_CALENDAR_URL = "https://finance.sina.com.cn/realstock/company/klc_td_sh.txt"


def configure_project(argv: list[str] | None = None) -> Path:
    """Load target settings before parsing provider defaults and relative paths."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--project-root", default=".")
    args, _ = parser.parse_known_args(argv)
    root = Path(args.project_root).expanduser().resolve()
    load_env_file(root / ".env")
    os.chdir(root)
    return root


def configured_data_root(variable: str) -> str:
    return os.getenv(variable) or os.getenv("DATA") or "data"


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def parse_schedule_time(value: str, variable_name: str) -> dt.time:
    try:
        hour_text, minute_text = value.split(":", 1)
        return dt.time(hour=int(hour_text), minute=int(minute_text))
    except ValueError as exc:
        raise ValueError(f"{variable_name} must use HH:MM format") from exc


def parse_schedule_days(value: str, variable_name: str, *, default_weekday: bool = False) -> set[int]:
    text = value.strip().lower()
    if not text:
        return set(range(5)) if default_weekday else set(range(7))
    if text == "all":
        return set(range(7))
    if text in {"weekday", "weekdays", "mon-fri"}:
        return set(range(5))

    days: set[int] = set()
    for item in text.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        if "-" in item:
            start_text, end_text = [part.strip() for part in item.split("-", 1)]
            start = WEEKDAYS[start_text]
            end = WEEKDAYS[end_text]
            if start <= end:
                days.update(range(start, end + 1))
            else:
                days.update(range(start, 7))
                days.update(range(0, end + 1))
        else:
            days.add(WEEKDAYS[item])
    if not days:
        raise ValueError(f"{variable_name} does not contain valid weekdays")
    return days


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_job_list(value: str | None, all_jobs: set[str], *, variable_name: str) -> set[str]:
    if value is None or value.strip() == "":
        return set()
    text = value.strip().lower()
    if text in {"1", "true", "yes", "all"}:
        return set(all_jobs)
    if text in {"0", "false", "no", "n", "off", "none"}:
        return set()
    jobs = {item.strip() for item in text.replace(";", ",").split(",") if item.strip()}
    invalid = jobs - all_jobs
    if invalid:
        raise ValueError(f"Unsupported {variable_name} values: {sorted(invalid)}")
    return jobs


def get_email_recipient() -> str:
    return os.getenv(EMAIL_RECIPIENT_ENV, "").strip()


def send_task_summary(
    *, task: str, target_date: str, started_at: str, finished_at: str,
    elapsed: float, outputs: list[dict], error: str | None, logger: logging.Logger,
) -> None:
    """Send the same minimal result table for collection and feature tasks."""
    if error:
        for key, value in os.environ.items():
            if value and any(word in key.upper() for word in ("TOKEN", "PASSWORD", "SECRET")):
                for secret in value.replace(";", ",").split(","):
                    if len(secret.strip()) >= 6:
                        error = error.replace(secret.strip(), "[redacted]")
    rows = [
        ("Status", f"Failed: {error}" if error else "Success"),
        ("Started", started_at),
        ("Finished", finished_at),
        ("Duration", f"{elapsed:.1f} seconds"),
    ]
    for output in outputs:
        details = str(output["path"])
        if output["rows"] is not None:
            details += f"\nRows: {output['rows']}"
        rows.append((output["table"], details))
    send_scheduler_email(
        subject=f"[{'Failed' if error else 'Success'}] {task} | {target_date}",
        summary_rows=rows, logger=logger,
    )


def send_scheduler_email(
    *,
    subject: str,
    summary_rows: list[tuple[str, str]],
    logger: logging.Logger,
    notify_env: str = "DATABASE_MAINTENANCE_NOTIFY",
) -> None:
    if not parse_bool(os.getenv(notify_env, "1"), default=True):
        logger.info("Scheduler email notification disabled by %s", notify_env)
        return

    sender = os.getenv("SENDER")
    password = os.getenv("EMAIL_PASSWORD")
    smtp_server = os.getenv("SMTP_SERVER")
    smtp_port = int(os.getenv("SMTP_PORT", "465"))
    use_ssl = os.getenv("SMTP_USE_SSL", "1").lower() not in {"0", "false", "no"}
    recipient = get_email_recipient()

    missing = []
    if not sender:
        missing.append("SENDER")
    if not password:
        missing.append("EMAIL_PASSWORD")
    if not smtp_server:
        missing.append("SMTP_SERVER")
    if not recipient:
        missing.append(EMAIL_RECIPIENT_ENV)
    if missing:
        logger.warning("Scheduler email skipped; missing env vars: %s", ", ".join(missing))
        return

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content("\n".join(f"{label}: {value}" for label, value in summary_rows))
    table_rows = "".join(
        f"<tr><th scope='row' style='text-align:left;padding:8px;border:1px solid #ddd'>{escape(str(label))}</th>"
        f"<td style='padding:8px;border:1px solid #ddd;white-space:pre-wrap'>{escape(str(value))}</td></tr>"
        for label, value in summary_rows
    )
    message.add_alternative(
        "<!doctype html><html><body><table style='border-collapse:collapse;font-family:Arial,sans-serif'>"
        "<thead><tr><th>Item</th><th>Summary</th></tr></thead><tbody>"
        + table_rows + "</tbody></table></body></html>", subtype="html",
    )

    try:
        if use_ssl:
            with smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=20) as smtp:
                smtp.login(sender, password)
                smtp.send_message(message, to_addrs=[recipient])
        else:
            with smtplib.SMTP(smtp_server, smtp_port, timeout=20) as smtp:
                smtp.starttls()
                smtp.login(sender, password)
                smtp.send_message(message, to_addrs=[recipient])
    except Exception as exc:  # noqa: BLE001 - scheduler notification must not mask job result.
        logger.exception("Scheduler email notification failed: %s", exc)
        return
    logger.info("Scheduler email notification sent to %s", recipient)


def default_log_path(logger_name: str) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", logger_name).strip("._") or "scheduler"
    return DEFAULT_LOG_DIR / f"{safe_name}.log"


class TaskFormatter(logging.Formatter):
    """Format task records with timezone-aware ISO 8601 timestamps."""

    def __init__(self) -> None:
        super().__init__(
            "%(asctime)s | %(levelname)s | %(name)s | "
            "task=%(task)s run_id=%(run_id)s date=%(target_date)s | %(message)s",
            defaults={"task": "scheduler", "run_id": "-", "target_date": "-"},
        )
        self.zone = ZoneInfo(os.getenv("DATABASE_MAINTENANCE_TIMEZONE", "Asia/Shanghai"))

    def formatTime(self, record, datefmt=None) -> str:
        return dt.datetime.fromtimestamp(record.created, self.zone).isoformat(timespec="milliseconds")


def setup_logger(name: str, log_path: str | None = None) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = TaskFormatter()
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    target_log_path = Path(log_path) if log_path else default_log_path(name)
    target_log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(target_log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.database_maintenance_log_path = target_log_path
    logger.info("Logger %s writing to %s", name, target_log_path)
    return logger


class SinaDateListDecoder:
    """Decode the Sina KLC_TD_SH compressed A-share trade-date list."""

    def __init__(self, encoded: str) -> None:
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
        self.values = [alphabet.index(char) for char in encoded]
        self.char_index = 0
        self.bit_index = 0
        self.state: dict[str, int] = {}

    def read_bit(self) -> bool:
        if self.char_index >= len(self.values):
            return False
        value = bool(self.values[self.char_index] & (1 << self.bit_index))
        self.bit_index += 1
        if self.bit_index >= 6:
            self.bit_index -= 6
            self.char_index += 1
        return value

    def read_signed_length(self) -> int:
        sign_bit = self.read_bit()
        length = 1
        while self.read_bit():
            length += 1
        return length * (2 * int(sign_bit) - 1)

    def read_fields(self, widths: list[int], signed: list[int] | None = None) -> list[int]:
        signed = signed or []
        result = []
        for pos, width in enumerate(widths):
            if width <= 0:
                result.append(0)
                continue
            if self.char_index >= len(self.values):
                return result
            if width <= 30:
                value = 0
                remaining = width
                while remaining > 0:
                    chunk = min(6 - self.bit_index, remaining)
                    value |= (
                        (self.values[self.char_index] >> self.bit_index) & ((1 << chunk) - 1)
                    ) << (width - remaining)
                    self.bit_index += chunk
                    if self.bit_index >= 6:
                        self.bit_index -= 6
                        self.char_index += 1
                    remaining -= chunk
                if pos < len(signed) and signed[pos] and value >= (1 << (width - 1)):
                    value -= 1 << width
            else:
                parts = self.read_fields([30, width - 30], [0, signed[pos] if pos < len(signed) else 0])
                value = parts[0] + parts[1] * (1 << 30)
            result.append(value)
        return result

    def next_trade_date(self, step: int) -> dt.date:
        for _ in range(step):
            self.state["d"] += 1
            weekday_flag = self.state["d"] % 7
            if weekday_flag in {3, 4}:
                self.state["d"] += 5 - weekday_flag
        return dt.date(1970, 1, 1) + dt.timedelta(days=7657 + self.state["d"])

    def decode_trade_dates(self) -> list[dt.date]:
        type_code, flag = self.read_fields([12, 6])
        mode = 63 ^ flag
        if type_code != 139 or mode > 1:
            raise ValueError(f"Unsupported Sina date list encoding: type={type_code}, mode={mode}")

        dates = []
        self.state["l"] = 0
        repeat_left = -1
        self.state["d"] = self.read_fields([18])[0] - 1
        end_day = self.read_fields([18])[0]

        while self.state["d"] < end_day:
            trade_date = self.next_trade_date(1)
            if repeat_left <= 0:
                if self.read_bit():
                    self.state["l"] += self.read_signed_length()
                repeat_left = self.read_fields([3 * self.state["l"]], [0])[0] + 1
                if not dates:
                    dates.append(trade_date)
                    repeat_left -= 1
            else:
                dates.append(trade_date)
            repeat_left -= 1
        return dates


def fetch_sina_trade_dates() -> set[str]:
    request = urllib.request.Request(SINA_TRADE_CALENDAR_URL, headers={"User-Agent": "Mozilla/5.0"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=20) as response:
        text = response.read().decode("utf-8")

    match = re.search(r'var\s+datelist\s*=\s*"([^"]+)"', text)
    if not match:
        raise ValueError("Unable to find Sina datelist payload")
    trade_dates = SinaDateListDecoder(match.group(1)).decode_trade_dates()
    trade_dates.append(dt.date(1992, 5, 4))
    return {value.isoformat() for value in trade_dates}


class CalendarUnavailable(RuntimeError):
    """The trade calendar cannot be loaded yet."""


class TradeCalendar:
    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger
        self._trade_dates: set[str] | None = None
        self.retry_seconds = int(
            os.getenv("DATABASE_MAINTENANCE_CALENDAR_RETRY_SECONDS", "60").strip()
            or "60"
        )
        if self.retry_seconds <= 0:
            raise ValueError(
                "DATABASE_MAINTENANCE_CALENDAR_RETRY_SECONDS must be positive"
            )
        self._retry_after = 0.0

    @property
    def ready(self) -> bool:
        return self._trade_dates is not None

    def is_trade_date(self, target_date: dt.date) -> bool:
        if self._trade_dates is None:
            if time.monotonic() < self._retry_after:
                raise CalendarUnavailable("Trade calendar is waiting for retry")
            try:
                self._trade_dates = fetch_sina_trade_dates()
            except Exception as exc:
                self._retry_after = time.monotonic() + self.retry_seconds
                self.logger.error(
                    "Failed to load trade calendar; retry in %d seconds: %s",
                    self.retry_seconds,
                    exc,
                )
                raise CalendarUnavailable("Trade calendar could not be loaded") from exc
        target_text = target_date.isoformat()
        if target_text in self._trade_dates:
            self.logger.info("%s is an A-share trade date", target_text)
            return True
        self.logger.info("%s is not an A-share trade date", target_text)
        return False


def next_run_after(
    now: dt.datetime,
    schedule_time: dt.time,
    schedule_days: set[int],
    date_filter: Callable[[dt.date], bool] | None = None,
) -> dt.datetime:
    for offset in range(370):
        candidate_date = now.date() + dt.timedelta(days=offset)
        candidate = dt.datetime.combine(candidate_date, schedule_time)
        if candidate.weekday() in schedule_days and candidate > now and (
            date_filter is None or date_filter(candidate_date)
        ):
            return candidate
    raise RuntimeError("Unable to resolve next schedule time")


def latest_run_at_or_before(
    now: dt.datetime,
    schedule_time: dt.time,
    schedule_days: set[int],
    date_filter: Callable[[dt.date], bool] | None = None,
) -> dt.datetime | None:
    for offset in range(370):
        candidate_date = now.date() - dt.timedelta(days=offset)
        candidate = dt.datetime.combine(candidate_date, schedule_time)
        if candidate.weekday() in schedule_days and candidate <= now and (
            date_filter is None or date_filter(candidate_date)
        ):
            return candidate
    return None


def initialize_completed_slots(
    now: dt.datetime,
    schedules: dict[str, tuple[dt.time, set[int]]],
    date_filters: dict[str, Callable[[dt.date], bool] | None] | None = None,
) -> set[tuple[str, dt.datetime]]:
    completed: set[tuple[str, dt.datetime]] = set()
    for name, (schedule_time, schedule_days) in schedules.items():
        date_filter = (date_filters or {}).get(name)
        try:
            scheduled_at = latest_run_at_or_before(
                now, schedule_time, schedule_days, date_filter
            )
        except CalendarUnavailable:
            continue
        if scheduled_at is not None:
            completed.add((name, scheduled_at))
    return completed


def due_scheduled_jobs(
    now: dt.datetime,
    schedules: dict[str, tuple[dt.time, set[int]]],
    completed_slots: set[tuple[str, dt.datetime]],
    *,
    misfire_grace_seconds: int,
    date_filters: dict[str, Callable[[dt.date], bool] | None] | None = None,
) -> list[tuple[str, dt.datetime]]:
    due: list[tuple[int, str, dt.datetime]] = []
    for index, (name, (schedule_time, schedule_days)) in enumerate(schedules.items()):
        date_filter = (date_filters or {}).get(name)
        try:
            scheduled_at = latest_run_at_or_before(
                now, schedule_time, schedule_days, date_filter
            )
        except CalendarUnavailable:
            continue
        if scheduled_at is None or (name, scheduled_at) in completed_slots:
            continue
        if (now - scheduled_at).total_seconds() <= misfire_grace_seconds:
            due.append((index, name, scheduled_at))
    due.sort(key=lambda item: (item[2], item[0]))
    return [(name, scheduled_at) for _, name, scheduled_at in due]
