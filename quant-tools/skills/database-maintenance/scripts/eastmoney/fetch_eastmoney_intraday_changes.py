from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq





SKILL_ROOT = Path(__file__).resolve().parents[2]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from scripts.common.scheduler import configure_project, configured_data_root, record_task_output

from scripts.eastmoney.client import (
    EastMoneyDataClient,
    STOCK_CHANGE_TYPE_NAMES,
    STOCK_CHANGE_TYPES,
    normalize_code,
)


DEFAULT_TABLE_NAME = "eastmoney_intraday_changes"


@dataclass(frozen=True)
class IntradayChangeRow:
    code: str
    date: str
    event_time: str
    name: str
    change_type: str
    change_type_code: str
    source_rank: int
    market_flag: str
    related_info: str
    ingested_at: str


def parse_event_time(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.endswith(".0"):
        text = text[:-2]
    text = text.zfill(6)
    if len(text) != 6 or not text.isdigit():
        return text
    return f"{text[0:2]}:{text[2:4]}:{text[4:6]}"


def parse_change_rows(
    *,
    trade_date: str,
    payload: dict[str, Any],
    ingested_at: str,
    fallback_change_type: str | None = None,
) -> list[IntradayChangeRow]:
    data = payload.get("data") or {}
    raw_rows = data.get("allstock") if isinstance(data, dict) else []
    if raw_rows is None:
        raw_rows = []
    if not isinstance(raw_rows, list):
        raise ValueError("Unexpected allstock payload for EastMoney intraday changes")

    rows: list[IntradayChangeRow] = []
    for idx, item in enumerate(raw_rows, start=1):
        if isinstance(item, dict):
            raw_code = str(item.get("c") or "").strip()
            raw_type_code = str(item.get("t") or "").strip()
            event_time = item.get("tm")
            name = item.get("n")
            market_flag = item.get("m")
            related_info = item.get("i")
        elif isinstance(item, list) and len(item) >= 6:
            raw_code = str(item[1] or "").strip()
            raw_type_code = str(item[4] or "").strip()
            event_time = item[0]
            name = item[3]
            market_flag = item[2]
            related_info = item[5]
        else:
            continue
        if not raw_code:
            continue
        change_type = STOCK_CHANGE_TYPE_NAMES.get(raw_type_code) or fallback_change_type
        if not change_type:
            continue
        rows.append(
            IntradayChangeRow(
                code=normalize_code(raw_code),
                date=trade_date,
                event_time=parse_event_time(event_time),
                name=str(name or ""),
                change_type=change_type,
                change_type_code=raw_type_code,
                source_rank=idx,
                market_flag=str(market_flag or ""),
                related_info=str(related_info or ""),
                ingested_at=ingested_at,
            )
        )
    return rows


def fetch_all_changes(
    client: EastMoneyDataClient,
    *,
    trade_date: str,
    page_size: int,
    sleep_seconds: float,
    change_types: list[str] | None = None,
) -> list[IntradayChangeRow]:
    ingested_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    resolved_types = change_types or list(STOCK_CHANGE_TYPES)
    rows: list[IntradayChangeRow] = []
    total_count: int | None = None
    page_index = 0
    while True:
        if page_index > 0 and sleep_seconds > 0:
            time.sleep(sleep_seconds)
        payload = client.stock_changes_page(
            change_types=resolved_types,
            page_index=page_index,
            page_size=page_size,
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        raw_rows = data.get("allstock") if isinstance(data, dict) else []
        if total_count is None:
            try:
                total_count = int(data.get("tc") or 0)
            except (TypeError, ValueError):
                total_count = 0
        parsed = parse_change_rows(
            trade_date=trade_date,
            payload=payload,
            ingested_at=ingested_at,
        )
        rows.extend(parsed)
        print(f"fetched page_index={page_index} page_rows={len(parsed)} total_rows={len(rows)} total_count={total_count}")
        if not raw_rows:
            break
        if total_count and len(rows) >= total_count:
            break
        if len(raw_rows) < page_size:
            break
        page_index += 1
    counts = pd.Series([row.change_type for row in rows], dtype="object").value_counts().to_dict() if rows else {}
    print(f"fetch_done change_types={len(resolved_types)} rows={len(rows)} counts={counts}")
    return rows


def rows_to_frame(rows: list[IntradayChangeRow]) -> pd.DataFrame:
    columns = [
        "code",
        "date",
        "event_time",
        "name",
        "change_type",
        "change_type_code",
        "source_rank",
        "market_flag",
        "related_info",
        "ingested_at",
    ]
    if not rows:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame([row.__dict__ for row in rows])
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["source_rank"] = pd.to_numeric(df["source_rank"], errors="coerce").astype("Int64")
    df["ingested_at"] = pd.to_datetime(df["ingested_at"], utc=True)
    return merge_duplicate_rows(df[columns])


def merge_duplicate_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    key_columns = ["code", "date", "event_time", "change_type", "source_rank"]
    value_columns = [column for column in df.columns if column not in (*key_columns, "ingested_at")]
    df = df.sort_values([*key_columns, "ingested_at"])
    grouped = df.groupby(key_columns, as_index=False, sort=True)
    merged = grouped[value_columns].last()
    ingested = grouped["ingested_at"].max()
    return merged.merge(ingested, on=key_columns, how="left").sort_values(
        ["date", "event_time", "change_type", "source_rank", "code"]
    ).reset_index(drop=True)


def write_upsert_partitioned(df: pd.DataFrame, data_root: Path, table_name: str) -> list[Path]:
    if df.empty:
        return []
    output_root = data_root / table_name
    output_root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for dt, part_df in df.groupby("date", sort=True):
        trade_date = pd.to_datetime(dt).date().isoformat()
        part_dir = output_root / f"date={trade_date}"
        part_dir.mkdir(parents=True, exist_ok=True)
        output_path = part_dir / "data_0.parquet"
        existing_files = sorted(part_dir.glob("*.parquet"))
        if existing_files:
            existing_parts = []
            for path in existing_files:
                existing_part = pd.read_parquet(path)
                if "date" not in existing_part.columns:
                    existing_part["date"] = trade_date
                existing_parts.append(existing_part)
            existing = pd.concat(existing_parts, ignore_index=True)
            existing["date"] = pd.to_datetime(existing["date"]).dt.date
            part_df = merge_duplicate_rows(pd.concat([existing, part_df], ignore_index=True, sort=False))
            for path in existing_files:
                if path != output_path:
                    path.unlink()
        table = pa.Table.from_pandas(part_df.drop(columns=["date"]), preserve_index=False)
        pq.write_table(table, output_path, compression="zstd")
        written.append(output_path)
    return written


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch EastMoney intraday stock changes into Hive parquet.")
    parser.add_argument("--project-root", default=".", help="Target project directory.")
    parser.add_argument("--date", required=True, help="Trade date, format YYYY-MM-DD or YYYYMMDD.")
    parser.add_argument("--data-root", default=configured_data_root("EASTMONEY_DATA_ROOT"), help="Data root directory.")
    parser.add_argument("--table-name", default=DEFAULT_TABLE_NAME, help="Output table name under data root.")
    parser.add_argument("--page-size", type=int, default=5000, help="EastMoney page size per change type.")
    parser.add_argument("--sleep", type=float, default=0.2, help="Sleep seconds between change-type requests.")
    parser.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout seconds.")
    parser.add_argument("--retries", type=int, default=1, help="Retries per request.")
    parser.add_argument("--retry-sleep", type=float, default=1.0, help="Retry backoff base seconds.")
    parser.add_argument("--proxy", default=None, help="Optional HTTP(S) proxy URL.")
    parser.add_argument("--change-types", nargs="*", default=None, help="Optional subset of Chinese change type names.")
    return parser.parse_args()


def main() -> str:
    configure_project()
    args = parse_args()
    trade_date = pd.to_datetime(args.date).date().isoformat()
    client = EastMoneyDataClient.from_env()
    if args.proxy:
        client = EastMoneyDataClient.from_env()
        client = EastMoneyDataClient(
            user_id=client.user_id,
            ut_token=client.ut_token,
            ct_token=client.ct_token,
            app_user_agent=client.app_user_agent,
            webview_user_agent=client.webview_user_agent,
            stock_card_client_version=client.stock_card_client_version,
            platform=client.platform,
            device_id=client.device_id,
            em_gt=client.em_gt,
            proxy=args.proxy,
            timeout=args.timeout,
            retries=args.retries,
            retry_sleep=args.retry_sleep,
        )
    else:
        client.timeout = args.timeout
        client.retries = args.retries
        client.retry_sleep = args.retry_sleep

    rows = fetch_all_changes(
        client,
        trade_date=trade_date,
        page_size=args.page_size,
        sleep_seconds=args.sleep,
        change_types=args.change_types,
    )
    df = rows_to_frame(rows)
    written = write_upsert_partitioned(df, Path(args.data_root), args.table_name)
    record_task_output(args.table_name, Path(args.data_root) / args.table_name, len(df))
    summary = f"table={args.table_name}; rows={len(df)}; partitions={len(written)}"
    print(f"write_done {summary}")
    return summary


if __name__ == "__main__":
    main()
