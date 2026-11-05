from __future__ import annotations
import sys

import argparse
import datetime as dt
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[2]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from scripts.common.scheduler import configure_project, configured_data_root, record_task_output


@dataclass(frozen=True)
class SyncTarget:
    name: str
    source: Path
    destination: str
    include: str | None = None


FULL_TABLES = [
    "quotes_day",
    "index_quotes_day",
    "index_weights",
    "financial_report",
    "industry_mapping",
    "instruments_info",
    "ex_factor",
    "eastmoney_stock_sentiment",
    "eastmoney_intraday_changes",
    "xueqiu_social_count",
    "xueqiu_hot_rank",
    "xueqiu_status",
    "xueqiu_cube_rebalance",
]

ALL_TABLES = [*FULL_TABLES, "quotes_min"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync QuantHub database parquet tables to a Hugging Face Storage Bucket.")
    parser.add_argument("--project-root", default=".", help="Target project directory.")
    parser.add_argument("--data-root", default=configured_data_root("HUGGINGFACE_BACKUP_DATA_ROOT"))
    parser.add_argument("--bucket-id", default=os.getenv("HUGGINGFACE_BUCKET_ID") or os.getenv("HUGGINGFACE_BACKUP_BUCKET_ID"))
    parser.add_argument("--remote-prefix", default=os.getenv("HUGGINGFACE_BACKUP_REMOTE_PREFIX", "tables"))
    parser.add_argument("--quotes-min-start-date", default=os.getenv("HUGGINGFACE_BACKUP_QUOTES_MIN_START_DATE", "2015-01-01"))
    parser.add_argument("--token-env", default=os.getenv("HUGGINGFACE_TOKEN_ENV", "HUGGINGFACE_TOKEN"))
    parser.add_argument("--include-table", action="append", choices=ALL_TABLES)
    parser.add_argument("--delete-remote-extra", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-retries", type=int, default=int(os.getenv("HUGGINGFACE_BACKUP_MAX_RETRIES", "5")))
    parser.add_argument("--retry-sleep-seconds", type=float, default=float(os.getenv("HUGGINGFACE_BACKUP_RETRY_SLEEP_SECONDS", "60")))
    return parser.parse_args()


def hf_executable() -> str:
    exe_name = "hf.exe" if os.name == "nt" else "hf"
    candidate = Path(sys.executable).with_name(exe_name)
    if candidate.exists():
        return str(candidate)
    return "hf"


def resolve_data_root(repo_root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (repo_root / path).resolve()


def destination_uri(bucket_id: str, remote_prefix: str, table: str) -> str:
    prefix = remote_prefix.strip("/")
    return f"hf://buckets/{bucket_id}/{prefix}/{table}" if prefix else f"hf://buckets/{bucket_id}/{table}"


def build_targets(data_root: Path, bucket_id: str, remote_prefix: str, start_date: dt.date, selected_tables: set[str]) -> list[SyncTarget]:
    targets: list[SyncTarget] = []
    for table in FULL_TABLES:
        if selected_tables and table not in selected_tables:
            continue
        source = data_root / table
        if source.exists():
            targets.append(SyncTarget(name=table, source=source, destination=destination_uri(bucket_id, remote_prefix, table)))
        else:
            print(f"Skipping missing table directory: {source.as_posix()}")

    if not selected_tables or "quotes_min" in selected_tables:
        source = data_root / "quotes_min"
        if source.exists():
            current_year = dt.date.today().year
            for year in range(start_date.year, current_year + 1):
                include = f"date={year}-*/**"
                targets.append(
                    SyncTarget(
                        name=f"quotes_min_{year}",
                        source=source,
                        destination=destination_uri(bucket_id, remote_prefix, "quotes_min"),
                        include=include,
                    )
                )
        else:
            print(f"Skipping missing table directory: {source.as_posix()}")
    return targets


def run_sync_target(target: SyncTarget, *, token: str, dry_run: bool, delete_remote_extra: bool, max_retries: int, retry_sleep_seconds: float) -> None:
    command = [
        hf_executable(),
        "buckets",
        "sync",
        target.source.as_posix(),
        target.destination,
        "--token",
        token,
    ]
    if target.include:
        command.extend(["--include", target.include])
    if dry_run:
        command.append("--dry-run")
    if delete_remote_extra:
        command.append("--delete")

    for attempt in range(1, max_retries + 1):
        started_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
        print(f"SYNC_START name={target.name} attempt={attempt} started_at_utc={started_at}")
        process = subprocess.run(command, text=True)
        if process.returncode == 0:
            finished_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
            if not dry_run:
                record_task_output(target.name, target.destination, None, "remote backup sync")
            print(f"SYNC_DONE name={target.name} attempt={attempt} finished_at_utc={finished_at}")
            return
        print(f"SYNC_FAIL name={target.name} attempt={attempt} return_code={process.returncode}")
        if attempt < max_retries:
            print(f"Sleeping {retry_sleep_seconds:.1f}s before retrying {target.name}")
            time.sleep(retry_sleep_seconds)
    raise RuntimeError(f"Sync failed after {max_retries} attempts: {target.name}")


def main() -> None:
    repo_root = configure_project()
    args = parse_args()
    if not args.bucket_id:
        raise ValueError("Missing bucket id. Set HUGGINGFACE_BUCKET_ID or pass --bucket-id.")
    token = os.getenv(args.token_env)
    if not token:
        raise ValueError(f"Missing required environment variable: {args.token_env}")
    if args.max_retries <= 0:
        raise ValueError("--max-retries must be positive")
    if args.retry_sleep_seconds < 0:
        raise ValueError("--retry-sleep-seconds must be non-negative")

    data_root = resolve_data_root(repo_root, args.data_root)
    start_date = dt.date.fromisoformat(args.quotes_min_start_date)
    selected_tables = set(args.include_table or [])
    targets = build_targets(data_root, args.bucket_id, args.remote_prefix, start_date, selected_tables)
    print(f"Bucket: {args.bucket_id}")
    print(f"Remote prefix: {args.remote_prefix.strip('/')}")
    print(f"Data root: {data_root.as_posix()}")
    print(f"Targets: {len(targets)}")
    for target in targets:
        suffix = f" include={target.include}" if target.include else ""
        print(f"Target {target.name}: {target.source.as_posix()} -> {target.destination}{suffix}")
    for target in targets:
        run_sync_target(
            target,
            token=token,
            dry_run=args.dry_run,
            delete_remote_extra=args.delete_remote_extra,
            max_retries=args.max_retries,
            retry_sleep_seconds=args.retry_sleep_seconds,
        )


if __name__ == "__main__":
    main()
