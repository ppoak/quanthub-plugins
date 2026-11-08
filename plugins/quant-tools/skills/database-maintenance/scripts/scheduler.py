from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import importlib
import io
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from uuid import uuid4

SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from scripts.common.scheduler import (
    CalendarUnavailable,
    TASK_OUTPUTS,
    TradeCalendar,
    due_scheduled_jobs,
    configure_project,
    configured_data_root,
    initialize_completed_slots,
    next_run_after,
    parse_job_list,
    parse_schedule_days,
    parse_schedule_time,
    scheduler_now,
    send_task_summary,
    setup_logger,
)


@dataclass(frozen=True)
class JobSpec:
    name: str
    schedule_time: dt.time
    schedule_days: set[int]
    require_trade_date: bool
    script_path: str
    module_name: str
    build_args: Callable[[dt.date], list[str]]


class JobOutput(io.StringIO):
    def __init__(self, logger) -> None:
        super().__init__()
        self.logger = logger
        self.pending = ""

    def write(self, value: str) -> int:
        written = super().write(value)
        self.pending += value
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            if line:
                self.logger.info("%s", line.rstrip("\r"))
        return written

    def flush(self) -> None:
        if self.pending:
            self.logger.info("%s", self.pending.rstrip("\r"))
            self.pending = ""
        super().flush()


def _upstream_ready_path(repo_root: Path) -> Path:
    value = os.getenv(
        "DATABASE_MAINTENANCE_UPSTREAM_READY_FILE",
        ".tmp/database-maintenance/upstream_ready.json",
    )
    path = Path(value)
    return path if path.is_absolute() else repo_root / path


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _write_upstream_ready_marker(
    *, repo_root: Path, target_date: dt.date, job_name: str
) -> None:
    required_job = os.getenv("DATABASE_MAINTENANCE_UPSTREAM_JOB", "ricequant").strip()
    if not required_job or job_name != required_job:
        return

    path = _upstream_ready_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "success",
        "job": job_name,
        "target_date": target_date.isoformat(),
        "finished_at_utc": _utc_now_iso(),
    }
    stage = path.with_name(f".{path.name}.staging")
    stage.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    stage.replace(path)


def _run_script_main(
    *,
    job: JobSpec,
    target_date: dt.date,
    logger,
    dry_run: bool,
) -> int:
    args = job.build_args(target_date)
    script_path = SKILL_ROOT / str(job.script_path)
    if dry_run:
        logger.info("Dry run: %s %s", script_path.as_posix(), " ".join(args))
        return 0

    module = importlib.import_module(str(job.module_name))
    output = JobOutput(logger)
    previous_argv = sys.argv
    previous_stderr = sys.stderr
    task_base_logger = (
        logger.logger if isinstance(logger, logging.LoggerAdapter) else logger
    )
    previous_propagate = task_base_logger.propagate
    task_base_logger.propagate = False
    rebound_handlers: list[tuple[logging.StreamHandler, object]] = []
    for source_logger in [logging.getLogger(), *logging.Logger.manager.loggerDict.values()]:
        if not isinstance(source_logger, logging.Logger) or source_logger is task_base_logger:
            continue
        for handler in source_logger.handlers:
            if (
                isinstance(handler, logging.StreamHandler)
                and not isinstance(handler, logging.FileHandler)
                and handler.stream is previous_stderr
            ):
                rebound_handlers.append((handler, handler.stream))
                handler.setStream(output)
    sys.argv = [str(script_path), *args]
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            try:
                module.main()
            except SystemExit as exc:
                return int(exc.code or 0) if isinstance(exc.code, int) else 1
    finally:
        output.flush()
        for handler, stream in rebound_handlers:
            handler.setStream(stream)
        # Source loggers created inside main must not retain this run's stream.
        for source_logger in [logging.getLogger(), *logging.Logger.manager.loggerDict.values()]:
            if not isinstance(source_logger, logging.Logger):
                continue
            for handler in source_logger.handlers:
                if isinstance(handler, logging.StreamHandler) and handler.stream is output:
                    handler.setStream(previous_stderr)
        output.close()
        task_base_logger.propagate = previous_propagate
        sys.argv = previous_argv
    return 0


def run_job(
    *,
    job: JobSpec,
    target_date: dt.date,
    repo_root: Path,
    calendar: TradeCalendar,
    logger: logging.Logger,
    dry_run: bool,
) -> int:
    task_logger = logging.LoggerAdapter(
        logger,
        {
            "task": job.name,
            "run_id": uuid4().hex[:12],
            "target_date": target_date.isoformat(),
        },
    )
    if target_date.weekday() not in job.schedule_days:
        task_logger.info("state=skipped reason=outside_schedule_days")
        return 0
    if job.require_trade_date:
        try:
            is_trade_date = calendar.is_trade_date(target_date)
        except CalendarUnavailable:
            task_logger.warning("state=deferred reason=calendar_unavailable")
            return 1
        if not is_trade_date:
            task_logger.info("state=skipped reason=non_trading_date")
            return 0

    outputs = []
    output_context = TASK_OUTPUTS.set(outputs)
    started_at = scheduler_now().isoformat(sep=" ")
    error = None
    started = time.perf_counter()
    return_code = 0
    try:
        task_logger.info(
            "state=started mode=%s script=%s",
            "dry_run" if dry_run else "execute",
            job.script_path,
        )
        try:
            return_code = _run_script_main(
                job=job,
                target_date=target_date,
                logger=task_logger,
                dry_run=dry_run,
            )
            if return_code == 0 and not dry_run:
                _write_upstream_ready_marker(
                    repo_root=repo_root,
                    target_date=target_date,
                    job_name=job.name,
                )
        except Exception as exc:
            return_code = 1
            error = f"{type(exc).__name__}: {exc}"
            task_logger.exception("state=failed reason=exception")

        elapsed = time.perf_counter() - started
        state = "failed" if return_code else ("dry_run" if dry_run else "succeeded")
        log = task_logger.error if return_code else task_logger.info
        log("state=%s elapsed_seconds=%.3f exit_code=%d", state, elapsed, return_code)
        if not dry_run:
            send_task_summary(
                task=job.name, target_date=target_date.isoformat(),
                started_at=started_at, finished_at=scheduler_now().isoformat(sep=" "),
                elapsed=elapsed, outputs=outputs,
                error=error or (f"Process exited with code {return_code}" if return_code else None),
                logger=task_logger,
            )
        return return_code
    finally:
        TASK_OUTPUTS.reset(output_context)


def _eastmoney_intraday_args(target_date: dt.date) -> list[str]:
    args = [
        "--date", target_date.isoformat(),
        "--data-root", configured_data_root("EASTMONEY_DATA_ROOT"),
        "--table-name", os.getenv("EASTMONEY_INTRADAY_CHANGES_TABLE", "eastmoney_intraday_changes"),
        "--page-size", os.getenv("EASTMONEY_INTRADAY_CHANGES_PAGE_SIZE", "5000"),
        "--sleep", os.getenv("EASTMONEY_INTRADAY_CHANGES_SLEEP", "0.2"),
        "--timeout", os.getenv("EASTMONEY_INTRADAY_CHANGES_TIMEOUT", "20"),
        "--retries", os.getenv("EASTMONEY_INTRADAY_CHANGES_RETRIES", "1"),
    ]
    proxy = os.getenv("EASTMONEY_MONI_PROXY")
    if proxy:
        args.extend(["--proxy", proxy])
    return args


def _eastmoney_sentiment_args(target_date: dt.date) -> list[str]:
    args = [
        "--date", target_date.isoformat(),
        "--data-root", configured_data_root("EASTMONEY_DATA_ROOT"),
        "--table-name", os.getenv("EASTMONEY_STOCK_SENTIMENT_TABLE", "eastmoney_stock_sentiment"),
        "--workers", os.getenv("EASTMONEY_STOCK_SENTIMENT_WORKERS", "8"),
        "--sleep", os.getenv("EASTMONEY_STOCK_SENTIMENT_SLEEP", "0.2"),
        "--timeout", os.getenv("EASTMONEY_STOCK_SENTIMENT_TIMEOUT", "20"),
        "--retries", os.getenv("EASTMONEY_STOCK_SENTIMENT_RETRIES", "1"),
    ]
    limit = os.getenv("EASTMONEY_STOCK_SENTIMENT_LIMIT")
    proxy = os.getenv("EASTMONEY_MONI_PROXY")
    if limit:
        args.extend(["--limit", limit])
    if proxy:
        args.extend(["--proxy", proxy])
    return args


def _xueqiu_status_args(target_date: dt.date) -> list[str]:
    return [
        "--date", target_date.isoformat(),
        "--data-root", configured_data_root("XUEQIU_DATA_ROOT"),
        "--table-name", os.getenv("XUEQIU_STATUS_TABLE", "xueqiu_status"),
        "--workers", os.getenv("XUEQIU_STATUS_WORKERS", "2"),
        "--batch-size", os.getenv("XUEQIU_STATUS_BATCH_SIZE", "20"),
        "--sleep", os.getenv("XUEQIU_STATUS_SLEEP", "1.0"),
        "--timeout", os.getenv("XUEQIU_STATUS_TIMEOUT", "20"),
        "--retries", os.getenv("XUEQIU_STATUS_RETRIES", "1"),
        "--max-pages", os.getenv("XUEQIU_STATUS_MAX_PAGES", "20"),
        "--candidate-limit", os.getenv("XUEQIU_STATUS_CANDIDATE_LIMIT", "200"),
        "--candidate-hot-limit", os.getenv("XUEQIU_STATUS_CANDIDATE_HOT_LIMIT", "200"),
        "--candidate-social-limit", os.getenv("XUEQIU_STATUS_CANDIDATE_SOCIAL_LIMIT", "150"),
        "--candidate-cube-limit", os.getenv("XUEQIU_STATUS_CANDIDATE_CUBE_LIMIT", "100"),
    ]


def _xueqiu_social_count_args(target_date: dt.date) -> list[str]:
    return [
        "--date", target_date.isoformat(),
        "--data-root", configured_data_root("XUEQIU_DATA_ROOT"),
        "--table-name", os.getenv("XUEQIU_SOCIAL_COUNT_TABLE", "xueqiu_social_count"),
        "--workers",
        os.getenv("XUEQIU_SOCIAL_COUNT_WORKERS", os.getenv("XUEQIU_SOCIAL_WORKERS", "2")),
        "--sleep", os.getenv("XUEQIU_SOCIAL_COUNT_SLEEP", os.getenv("XUEQIU_SOCIAL_SLEEP", "0.2")),
        "--timeout",
        os.getenv("XUEQIU_SOCIAL_COUNT_TIMEOUT", os.getenv("XUEQIU_SOCIAL_TIMEOUT", "30")),
        "--retries",
        os.getenv("XUEQIU_SOCIAL_COUNT_RETRIES", os.getenv("XUEQIU_SOCIAL_RETRIES", "2")),
        "--failure-retry-rounds", os.getenv("XUEQIU_SOCIAL_COUNT_FAILURE_RETRY_ROUNDS", "1"),
        "--failure-retry-workers", os.getenv("XUEQIU_SOCIAL_COUNT_FAILURE_RETRY_WORKERS", "1"),
        "--failure-retry-sleep", os.getenv("XUEQIU_SOCIAL_COUNT_FAILURE_RETRY_SLEEP", "1.0"),
        "--failure-retry-timeout", os.getenv("XUEQIU_SOCIAL_COUNT_FAILURE_RETRY_TIMEOUT", "30"),
        "--failure-retry-retries", os.getenv("XUEQIU_SOCIAL_COUNT_FAILURE_RETRY_RETRIES", "3"),
    ]


def _xueqiu_hot_rank_args(target_date: dt.date) -> list[str]:
    return [
        "--date", target_date.isoformat(),
        "--data-root", configured_data_root("XUEQIU_DATA_ROOT"),
        "--table-name", os.getenv("XUEQIU_HOT_RANK_TABLE", "xueqiu_hot_rank"),
        "--timeout", os.getenv("XUEQIU_HOT_RANK_TIMEOUT", os.getenv("XUEQIU_SOCIAL_TIMEOUT", "20")),
        "--retries", os.getenv("XUEQIU_HOT_RANK_RETRIES", os.getenv("XUEQIU_SOCIAL_RETRIES", "1")),
        "--hot-rank-types", *os.getenv("XUEQIU_HOT_RANK_TYPES", "12 22").replace(",", " ").split(),
    ]


def _xueqiu_cube_args(target_date: dt.date) -> list[str]:
    return [
        "--date", target_date.isoformat(),
        "--data-root", configured_data_root("XUEQIU_DATA_ROOT"),
        "--table-name", os.getenv("XUEQIU_CUBE_REBALANCE_TABLE", "xueqiu_cube_rebalance"),
        "--workers", os.getenv("XUEQIU_CUBE_WORKERS", "2"),
        "--sleep", os.getenv("XUEQIU_CUBE_SLEEP", "0.2"),
        "--timeout", os.getenv("XUEQIU_CUBE_TIMEOUT", "20"),
        "--retries", os.getenv("XUEQIU_CUBE_RETRIES", "1"),
        "--failure-retry-rounds", os.getenv("XUEQIU_CUBE_FAILURE_RETRY_ROUNDS", "1"),
        "--failure-retry-workers", os.getenv("XUEQIU_CUBE_FAILURE_RETRY_WORKERS", "1"),
        "--failure-retry-sleep", os.getenv("XUEQIU_CUBE_FAILURE_RETRY_SLEEP", "2.0"),
        "--failure-retry-timeout", os.getenv("XUEQIU_CUBE_FAILURE_RETRY_TIMEOUT", "30"),
        "--failure-retry-retries", os.getenv("XUEQIU_CUBE_FAILURE_RETRY_RETRIES", "5"),
    ]


def _xueqiu_relogin_args(_target_date: dt.date) -> list[str]:
    return [
        "--check-expiring",
        "--threshold-days", os.getenv("XUEQIU_RELOGIN_THRESHOLD_DAYS", "5"),
        "--timeout", os.getenv("XUEQIU_RELOGIN_TIMEOUT", "600"),
    ]


def _huggingface_args(_target_date: dt.date) -> list[str]:
    args = [
        "--data-root", configured_data_root("HUGGINGFACE_BACKUP_DATA_ROOT"),
        "--bucket-id",
        os.getenv("HUGGINGFACE_BUCKET_ID", os.getenv("HUGGINGFACE_BACKUP_BUCKET_ID", "")),
        "--quotes-min-start-date",
        os.getenv("HUGGINGFACE_BACKUP_QUOTES_MIN_START_DATE", "2015-01-01"),
        "--max-retries", os.getenv("HUGGINGFACE_BACKUP_MAX_RETRIES", "5"),
        "--retry-sleep-seconds", os.getenv("HUGGINGFACE_BACKUP_RETRY_SLEEP_SECONDS", "60"),
    ]
    remote_prefix = os.getenv("HUGGINGFACE_BACKUP_REMOTE_PREFIX")
    token_env = os.getenv("HUGGINGFACE_TOKEN_ENV")
    if remote_prefix:
        args.extend(["--remote-prefix", remote_prefix])
    if token_env:
        args.extend(["--token-env", token_env])
    delete_remote_extra = os.getenv("HUGGINGFACE_BACKUP_DELETE_REMOTE_EXTRA", "0")
    if delete_remote_extra.strip().lower() in {"1", "true", "yes", "on"}:
        args.append("--delete-remote-extra")
    return args


def build_jobs() -> dict[str, JobSpec]:
    jobs = {
        "eastmoney_intraday_changes": JobSpec(
            name="eastmoney_intraday_changes",
            schedule_time=parse_schedule_time(
                os.getenv("EASTMONEY_INTRADAY_CHANGES_SCHEDULE_TIME", "15:05"),
                "EASTMONEY_INTRADAY_CHANGES_SCHEDULE_TIME",
            ),
            schedule_days=parse_schedule_days(
                os.getenv("EASTMONEY_INTRADAY_CHANGES_SCHEDULE_DAYS", "weekday"),
                "EASTMONEY_INTRADAY_CHANGES_SCHEDULE_DAYS",
                default_weekday=True,
            ),
            require_trade_date=True,
            script_path="scripts/eastmoney/fetch_eastmoney_intraday_changes.py",
            module_name="scripts.eastmoney.fetch_eastmoney_intraday_changes",
            build_args=_eastmoney_intraday_args,
        ),
        "xueqiu_cube_rebalance": JobSpec(
            name="xueqiu_cube_rebalance",
            schedule_time=parse_schedule_time(
                os.getenv("XUEQIU_CUBE_REBALANCE_SCHEDULE_TIME", "15:30"),
                "XUEQIU_CUBE_REBALANCE_SCHEDULE_TIME",
            ),
            schedule_days=parse_schedule_days(
                os.getenv("XUEQIU_CUBE_REBALANCE_SCHEDULE_DAYS", "weekday"),
                "XUEQIU_CUBE_REBALANCE_SCHEDULE_DAYS",
                default_weekday=True,
            ),
            require_trade_date=True,
            script_path="scripts/xueqiu/fetch_xueqiu_cube_rebalance.py",
            module_name="scripts.xueqiu.fetch_xueqiu_cube_rebalance",
            build_args=_xueqiu_cube_args,
        ),
        "xueqiu_relogin": JobSpec(
            name="xueqiu_relogin",
            schedule_time=parse_schedule_time(
                os.getenv("XUEQIU_RELOGIN_SCHEDULE_TIME", "02:00"),
                "XUEQIU_RELOGIN_SCHEDULE_TIME",
            ),
            schedule_days=parse_schedule_days(
                os.getenv("XUEQIU_RELOGIN_SCHEDULE_DAYS", "all"),
                "XUEQIU_RELOGIN_SCHEDULE_DAYS",
            ),
            require_trade_date=False,
            script_path="scripts/xueqiu/email_relogin.py",
            module_name="scripts.xueqiu.email_relogin",
            build_args=_xueqiu_relogin_args,
        ),
        "ricequant": JobSpec(
            name="ricequant",
            schedule_time=parse_schedule_time(
                os.getenv("RICEQUANT_SCHEDULE_TIME", "19:30"), "RICEQUANT_SCHEDULE_TIME"
            ),
            schedule_days=parse_schedule_days(
                os.getenv("RICEQUANT_SCHEDULE_DAYS", "weekday"),
                "RICEQUANT_SCHEDULE_DAYS",
                default_weekday=True,
            ),
            require_trade_date=True,
            script_path="scripts/ricequant/fetch_ricequant.py",
            module_name="scripts.ricequant.fetch_ricequant",
            build_args=lambda _target_date: [],
        ),
        "eastmoney_stock_sentiment": JobSpec(
            name="eastmoney_stock_sentiment",
            schedule_time=parse_schedule_time(
                os.getenv("EASTMONEY_SCHEDULE_TIME", "22:00"), "EASTMONEY_SCHEDULE_TIME"
            ),
            schedule_days=parse_schedule_days(
                os.getenv("EASTMONEY_SCHEDULE_DAYS", "all"), "EASTMONEY_SCHEDULE_DAYS"
            ),
            require_trade_date=False,
            script_path="scripts/eastmoney/fetch_eastmoney_stock_sentiment.py",
            module_name="scripts.eastmoney.fetch_eastmoney_stock_sentiment",
            build_args=_eastmoney_sentiment_args,
        ),
        "xueqiu_social_count": JobSpec(
            name="xueqiu_social_count",
            schedule_time=parse_schedule_time(
                os.getenv("XUEQIU_SOCIAL_COUNT_SCHEDULE_TIME", "22:30"),
                "XUEQIU_SOCIAL_COUNT_SCHEDULE_TIME",
            ),
            schedule_days=parse_schedule_days(
                os.getenv("XUEQIU_SCHEDULE_DAYS", "all"),
                "XUEQIU_SCHEDULE_DAYS",
            ),
            require_trade_date=False,
            script_path="scripts/xueqiu/fetch_xueqiu_social_count.py",
            module_name="scripts.xueqiu.fetch_xueqiu_social_count",
            build_args=_xueqiu_social_count_args,
        ),
        "xueqiu_hot_rank": JobSpec(
            name="xueqiu_hot_rank",
            schedule_time=parse_schedule_time(
                os.getenv("XUEQIU_HOT_RANK_SCHEDULE_TIME", "23:00"),
                "XUEQIU_HOT_RANK_SCHEDULE_TIME",
            ),
            schedule_days=parse_schedule_days(
                os.getenv("XUEQIU_SCHEDULE_DAYS", "all"),
                "XUEQIU_SCHEDULE_DAYS",
            ),
            require_trade_date=False,
            script_path="scripts/xueqiu/fetch_xueqiu_hot_rank.py",
            module_name="scripts.xueqiu.fetch_xueqiu_hot_rank",
            build_args=_xueqiu_hot_rank_args,
        ),
        "xueqiu_status": JobSpec(
            name="xueqiu_status",
            schedule_time=parse_schedule_time(
                os.getenv("XUEQIU_STATUS_SCHEDULE_TIME", "01:30"),
                "XUEQIU_STATUS_SCHEDULE_TIME",
            ),
            schedule_days=parse_schedule_days(
                os.getenv("XUEQIU_SCHEDULE_DAYS", "all"),
                "XUEQIU_SCHEDULE_DAYS",
            ),
            require_trade_date=False,
            script_path="scripts/xueqiu/fetch_xueqiu_status.py",
            module_name="scripts.xueqiu.fetch_xueqiu_status",
            build_args=_xueqiu_status_args,
        ),
        "huggingface_backup": JobSpec(
            name="huggingface_backup",
            schedule_time=parse_schedule_time(
                os.getenv("HUGGINGFACE_BACKUP_SCHEDULE_TIME", "04:00"),
                "HUGGINGFACE_BACKUP_SCHEDULE_TIME",
            ),
            schedule_days=parse_schedule_days(
                os.getenv("HUGGINGFACE_BACKUP_SCHEDULE_DAYS", "all"),
                "HUGGINGFACE_BACKUP_SCHEDULE_DAYS",
            ),
            require_trade_date=False,
            script_path="scripts/huggingface/sync_huggingface_bucket.py",
            module_name="scripts.huggingface.sync_huggingface_bucket",
            build_args=_huggingface_args,
        ),
    }

    return jobs


def filter_enabled_jobs(jobs: dict[str, JobSpec]) -> dict[str, JobSpec]:
    value = os.getenv("DATABASE_MAINTENANCE_ENABLED_JOBS", "all").strip().lower()
    if value in {"", "all"}:
        return jobs
    enabled = parse_job_list(value, set(jobs), variable_name="DATABASE_MAINTENANCE_ENABLED_JOBS")
    return {name: job for name, job in jobs.items() if name in enabled}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the unified QuantHub database maintenance scheduler."
    )
    parser.add_argument("--project-root", default=".", help="Target project directory.")
    parser.add_argument("--list-jobs", action="store_true", help="Print configured jobs and exit.")
    parser.add_argument(
        "--run-once",
        nargs="*",
        metavar="JOB",
        help="Run selected jobs once and exit. Omit JOB after --run-once to run all enabled jobs.",
    )
    parser.add_argument("--date", help="Target date for --run-once jobs, in YYYY-MM-DD format.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log jobs without executing ingestion.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    repo_root = configure_project(argv)
    args = parse_args(argv)
    if args.list_jobs:
        for job in filter_enabled_jobs(build_jobs()).values():
            print(
                f"{job.name}\t{job.schedule_time.strftime('%H:%M')}\t"
                f"days={','.join(str(day) for day in sorted(job.schedule_days))}\t"
                f"trade_date_required={int(job.require_trade_date)}"
            )
        return
    logger = setup_logger("DatabaseMaintenanceScheduler")
    all_jobs = build_jobs()
    jobs = filter_enabled_jobs(all_jobs)
    calendar = TradeCalendar(logger)
    timezone_name = os.getenv("DATABASE_MAINTENANCE_TIMEZONE", "Asia/Shanghai")
    logger.info(
        "Database maintenance scheduler started enabled_jobs=%s repo_root=%s timezone=%s",
        ",".join(jobs),
        repo_root,
        timezone_name,
    )
    target_date = (
        dt.date.fromisoformat(args.date)
        if args.date
        else scheduler_now(timezone_name).date()
    )

    if args.run_once is not None:
        selected_names = set(args.run_once) if args.run_once else set(jobs)
        invalid = selected_names - set(jobs)
        if invalid:
            raise ValueError(f"Unsupported or disabled job names: {sorted(invalid)}")
        exit_code = 0
        for name in jobs:
            if name in selected_names:
                exit_code = max(
                    exit_code,
                    run_job(
                        job=jobs[name],
                        target_date=target_date,
                        repo_root=repo_root,
                        calendar=calendar,
                        logger=logger,
                        dry_run=args.dry_run,
                    ),
                )
        raise SystemExit(exit_code)

    run_on_start = parse_job_list(
        os.getenv("DATABASE_MAINTENANCE_RUN_ON_START", "0"),
        set(jobs),
        variable_name="DATABASE_MAINTENANCE_RUN_ON_START",
    )
    if run_on_start:
        today = scheduler_now(timezone_name).date()
        for name in jobs:
            if name in run_on_start:
                run_job(
                    job=jobs[name],
                    target_date=today,
                    repo_root=repo_root,
                    calendar=calendar,
                    logger=logger,
                    dry_run=args.dry_run,
                )

    calendar_required = any(job.require_trade_date for job in jobs.values())
    schedules = {name: (job.schedule_time, job.schedule_days) for name, job in jobs.items()}
    date_filters = {
        name: calendar.is_trade_date if job.require_trade_date else None
        for name, job in jobs.items()
    }
    completed_slots = initialize_completed_slots(
        scheduler_now(timezone_name), schedules, date_filters
    )
    misfire_grace_seconds = int(os.getenv("DATABASE_MAINTENANCE_MISFIRE_GRACE_SECONDS", "21600"))

    while True:
        now = scheduler_now(timezone_name)
        due_jobs = due_scheduled_jobs(
            now,
            schedules,
            completed_slots,
            misfire_grace_seconds=misfire_grace_seconds,
            date_filters=date_filters,
        )
        if due_jobs:
            for job_name, scheduled_at in due_jobs:
                logger.info(
                    "Running due database maintenance job=%s scheduled_at=%s",
                    job_name,
                    scheduled_at.isoformat(sep=" "),
                )
                run_job(
                    job=jobs[job_name],
                    target_date=scheduled_at.date(),
                    repo_root=repo_root,
                    calendar=calendar,
                    logger=logger,
                    dry_run=args.dry_run,
                )
                completed_slots.add((job_name, scheduled_at))
            time.sleep(60)
            continue

        next_jobs = {}
        for name, job in jobs.items():
            try:
                next_jobs[name] = next_run_after(
                    now, job.schedule_time, job.schedule_days, date_filters[name]
                )
            except CalendarUnavailable:
                continue
        if not next_jobs:
            time.sleep(calendar.retry_seconds)
            continue
        job_name, target = min(next_jobs.items(), key=lambda item: item[1])
        if calendar_required and not calendar.ready:
            seconds = max(0.0, (target - scheduler_now(timezone_name)).total_seconds())
            time.sleep(min(seconds, calendar.retry_seconds))
            continue
        logger.info(
            "Next database maintenance job=%s scheduled at %s",
            job_name,
            target.isoformat(sep=" "),
        )
        while True:
            seconds = (target - scheduler_now(timezone_name)).total_seconds()
            if seconds <= 0:
                break
            time.sleep(min(seconds, 300))


if __name__ == "__main__":
    main()
