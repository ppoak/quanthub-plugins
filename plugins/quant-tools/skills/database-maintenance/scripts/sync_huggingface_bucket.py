from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path

from common.project import configure_project, configured_data_root
from huggingface.bucket import SyncTarget, destination_uri, run_sync_target


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
    parser.add_argument(
        "--delete-remote-extra", action=argparse.BooleanOptionalAction,
        default=os.getenv("HUGGINGFACE_BACKUP_DELETE_REMOTE_EXTRA", "0").strip().lower() in {"1", "true", "yes", "on"},
        help="Delete extra remote objects. Defaults to off unless enabled in project settings.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-retries", type=int, default=int(os.getenv("HUGGINGFACE_BACKUP_MAX_RETRIES", "5")))
    parser.add_argument("--retry-sleep-seconds", type=float, default=float(os.getenv("HUGGINGFACE_BACKUP_RETRY_SLEEP_SECONDS", "60")))
    return parser.parse_args()


def resolve_data_root(repo_root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (repo_root / path).resolve()


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
