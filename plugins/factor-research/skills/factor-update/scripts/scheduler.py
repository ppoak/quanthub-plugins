"""Schedule the Factor Update skill after upstream data is ready."""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_TIME = "21:00"
DEFAULT_DAYS = "weekday"
DEFAULT_TARGET = "active"
DEFAULT_LOG = ".tmp/factor_update/scheduler.log"
DEFAULT_LOCK = ".tmp/factor_update/scheduler.lock"
DEFAULT_READY = ".tmp/database-maintenance/upstream_ready.json"


def parse_clock(value: str) -> clock_time:
    try:
        parsed = datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("schedule time must use HH:MM") from exc
    return parsed


def parse_days(value: str) -> set[int]:
    names = {"mon": 0, "monday": 0, "tue": 1, "tuesday": 1,
             "wed": 2, "wednesday": 2, "thu": 3, "thursday": 3,
             "fri": 4, "friday": 4, "sat": 5, "saturday": 5,
             "sun": 6, "sunday": 6}
    value = value.strip().lower()
    if value == "all":
        return set(range(7))
    if value in {"weekday", "weekdays"}:
        return set(range(5))
    days: set[int] = set()
    for item in value.split(","):
        item = item.strip().lower()
        if item.isdigit() and 0 <= int(item) <= 6:
            days.add(int(item))
        elif item in names:
            days.add(names[item])
        else:
            raise argparse.ArgumentTypeError(f"invalid schedule day: {item}")
    if not days:
        raise argparse.ArgumentTypeError("schedule days cannot be empty")
    return days


def next_run(now: datetime, scheduled: clock_time, days: set[int]) -> datetime:
    for offset in range(8):
        candidate_date = now.date() + timedelta(days=offset)
        candidate = datetime.combine(candidate_date, scheduled, tzinfo=now.tzinfo)
        if candidate > now and candidate.weekday() in days:
            return candidate
    raise RuntimeError("no next scheduler run could be calculated")


def resolve_path(project_root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else project_root / path


def upstream_ready(project_root: Path, marker: Path, target_date: date) -> bool:
    if os.getenv("FACTOR_UPDATE_REQUIRE_UPSTREAM_READY", "0").strip() not in {"1", "true", "yes"}:
        return True
    if not marker.is_file():
        return False
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return payload.get("status") == "success" and payload.get("target_date") == target_date.isoformat()


def acquire_lock(path: Path) -> int | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None


def release_lock(path: Path, descriptor: int | None) -> None:
    if descriptor is None:
        return
    os.close(descriptor)
    path.unlink(missing_ok=True)


def run_update(args: argparse.Namespace, project_root: Path, logger: logging.Logger) -> int:
    marker = resolve_path(project_root, args.upstream_ready_file)
    target_date = args.run_date or datetime.now(ZoneInfo(args.timezone)).date()
    if not upstream_ready(project_root, marker, target_date):
        logger.info("state=waiting reason=upstream_not_ready target_date=%s marker=%s", target_date, marker)
        return 0
    lock_path = resolve_path(project_root, args.lock_file)
    descriptor = acquire_lock(lock_path)
    if descriptor is None:
        logger.warning("state=skipped reason=update_already_running lock=%s", lock_path)
        return 0
    command = [sys.executable, str(Path(__file__).with_name("run_update.py")),
               "--project-root", str(project_root), "--target", args.target]
    if args.dry_run:
        command.append("--dry-run")
    started = time.monotonic()
    logger.info("state=started target=%s dry_run=%s target_date=%s", args.target, args.dry_run, target_date)
    try:
        completed = subprocess.run(command, cwd=project_root, check=False)
        elapsed = time.monotonic() - started
        logger.info("state=%s target=%s elapsed_seconds=%.3f return_code=%s",
                    "success" if completed.returncode == 0 else "failed",
                    args.target, elapsed, completed.returncode)
        return completed.returncode
    finally:
        release_lock(lock_path, descriptor)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--target", default=os.getenv("FACTOR_UPDATE_TARGET", DEFAULT_TARGET))
    parser.add_argument("--schedule-time", type=parse_clock,
                        default=parse_clock(os.getenv("FACTOR_UPDATE_SCHEDULE_TIME", DEFAULT_TIME)))
    parser.add_argument("--schedule-days", type=parse_days,
                        default=parse_days(os.getenv("FACTOR_UPDATE_SCHEDULE_DAYS", DEFAULT_DAYS)))
    parser.add_argument("--timezone", default=os.getenv("FACTOR_UPDATE_TIMEZONE", DEFAULT_TIMEZONE))
    parser.add_argument("--upstream-ready-file",
                        default=os.getenv("FACTOR_UPDATE_UPSTREAM_READY_FILE", DEFAULT_READY))
    parser.add_argument("--lock-file", default=os.getenv("FACTOR_UPDATE_LOCK_FILE", DEFAULT_LOCK))
    parser.add_argument("--log-file", default=os.getenv("FACTOR_UPDATE_LOG_FILE", DEFAULT_LOG))
    parser.add_argument("--run-once", action="store_true", help="Run immediately and exit.")
    parser.add_argument("--date", dest="run_date", type=date.fromisoformat,
                        help="Target date for --run-once or a scheduled check.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list-jobs", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    project_root = Path(args.project_root).resolve()
    if args.list_jobs:
        days = ",".join(str(day) for day in sorted(args.schedule_days))
        print(f"factor_update\t{args.schedule_time.strftime('%H:%M')}\tdays={days}\ttarget={args.target}")
        return 0
    log_path = resolve_path(project_root, args.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=log_path, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger("factor_update.scheduler")
    if args.run_once:
        return run_update(args, project_root, logger)
    zone = ZoneInfo(args.timezone)
    logger.info("scheduler_started schedule=%s days=%s timezone=%s target=%s",
                args.schedule_time.strftime('%H:%M'), sorted(args.schedule_days), args.timezone, args.target)
    while True:
        now = datetime.now(zone)
        target = next_run(now, args.schedule_time, args.schedule_days)
        logger.info("next_run=%s", target.isoformat())
        time.sleep(max(0.0, (target - datetime.now(zone)).total_seconds()))
        run_update(args, project_root, logger)


if __name__ == "__main__":
    raise SystemExit(main())
