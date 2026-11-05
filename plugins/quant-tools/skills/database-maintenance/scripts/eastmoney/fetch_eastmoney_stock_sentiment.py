from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    load_codes_from_quotes_day,
    normalize_code,
    to_eastmoney_symbol,
)


DEFAULT_TABLE_NAME = "eastmoney_stock_sentiment"


@dataclass(frozen=True)
class SentimentRow:
    code: str
    date: str
    tape_z: float | None
    tape_d: float | None
    tape_type: int | None
    hot_rank: int | None
    new_uid_rate: float | None
    new_uid_change_rate: float | None
    old_uid_rate: float | None
    old_uid_change_rate: float | None
    ingested_at: str


def optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def optional_percent(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("%"):
        text = text[:-1]
    parsed = optional_float(text)
    return None if parsed is None else parsed / 100.0


def parse_date(value: Any) -> str | None:
    if value is None:
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date().isoformat()


def parse_stock_card_rows(code: str, payload: dict[str, Any], ingested_at: str) -> list[SentimentRow]:
    normalized = normalize_code(code)
    rows: list[SentimentRow] = []
    for card in payload.get("data") or []:
        if not isinstance(card, dict) or card.get("cardType") != "dkkp":
            continue
        data = card.get("extendData") or {}
        if not isinstance(data, dict):
            continue
        date = parse_date(data.get("date"))
        if date is None:
            continue
        rows.append(
            SentimentRow(
                code=normalized,
                date=date,
                tape_z=optional_float(data.get("tapeZ")),
                tape_d=optional_float(data.get("tapeD")),
                tape_type=optional_int(data.get("tapeType")),
                hot_rank=None,
                new_uid_rate=None,
                new_uid_change_rate=None,
                old_uid_rate=None,
                old_uid_change_rate=None,
                ingested_at=ingested_at,
            )
        )
    return rows


def parse_profile_rows(code: str, payload: dict[str, Any], ingested_at: str) -> list[SentimentRow]:
    normalized = normalize_code(code)
    rows: list[SentimentRow] = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        date = parse_date(item.get("calcTime"))
        if date is None:
            continue
        rows.append(
            SentimentRow(
                code=normalized,
                date=date,
                tape_z=None,
                tape_d=None,
                tape_type=None,
                hot_rank=None,
                new_uid_rate=optional_percent(item.get("newUidRate")),
                new_uid_change_rate=optional_percent(item.get("newUidChangeRank")),
                old_uid_rate=optional_percent(item.get("oldUidRate")),
                old_uid_change_rate=optional_percent(item.get("oldUidChangeRank")),
                ingested_at=ingested_at,
            )
        )
    return rows


def parse_rank_rows(code: str, payload: dict[str, Any], ingested_at: str) -> list[SentimentRow]:
    normalized = normalize_code(code)
    rows: list[SentimentRow] = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        date = parse_date(item.get("calcTime"))
        if date is None:
            continue
        rows.append(
            SentimentRow(
                code=normalized,
                date=date,
                tape_z=None,
                tape_d=None,
                tape_type=None,
                hot_rank=optional_int(item.get("rank")),
                new_uid_rate=None,
                new_uid_change_rate=None,
                old_uid_rate=None,
                old_uid_change_rate=None,
                ingested_at=ingested_at,
            )
        )
    return rows


def fetch_one(client: EastMoneyDataClient, code: str, sleep_seconds: float) -> list[SentimentRow]:
    normalized = normalize_code(code)
    em_code = to_eastmoney_symbol(normalized)
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)
    ingested_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    rows: list[SentimentRow] = []
    rows.extend(parse_stock_card_rows(normalized, client.stock_card(code=em_code), ingested_at))
    rows.extend(parse_profile_rows(normalized, client.his_profile_list(code=em_code), ingested_at))
    rows.extend(parse_rank_rows(normalized, client.his_rank_list(code=em_code), ingested_at))
    return rows


def filter_rows_for_date(rows: list[SentimentRow], target_date: str | None) -> list[SentimentRow]:
    if target_date is None:
        return rows
    return [row for row in rows if row.date == target_date]


def rows_to_frame(rows: list[SentimentRow], *, target_date: str | None = None) -> pd.DataFrame:
    columns = [
        "code",
        "date",
        "tape_z",
        "tape_d",
        "tape_type",
        "hot_rank",
        "new_uid_rate",
        "new_uid_change_rate",
        "old_uid_rate",
        "old_uid_change_rate",
        "ingested_at",
    ]
    if not rows:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame([row.__dict__ for row in rows])
    df["date"] = pd.to_datetime(df["date"]).dt.date
    if target_date is not None:
        target = pd.to_datetime(target_date).date()
        df = df[df["date"] == target].copy()
        if df.empty:
            return pd.DataFrame(columns=columns)
    df["ingested_at"] = pd.to_datetime(df["ingested_at"], utc=True)
    for column in ["tape_z", "tape_d", "new_uid_rate", "new_uid_change_rate", "old_uid_rate", "old_uid_change_rate"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    for column in ["tape_type", "hot_rank"]:
        df[column] = pd.to_numeric(df[column], errors="coerce").astype("Int64")
    return merge_duplicate_rows(df[columns])


def merge_duplicate_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    value_columns = [column for column in df.columns if column not in ("code", "date", "ingested_at")]
    df = df.sort_values(["code", "date", "ingested_at"])
    grouped = df.groupby(["code", "date"], as_index=False, sort=True)
    merged = grouped[value_columns].last()
    ingested = grouped["ingested_at"].max()
    return merged.merge(ingested, on=["code", "date"], how="left").sort_values(["date", "code"]).reset_index(drop=True)


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


def build_client(args: argparse.Namespace) -> EastMoneyDataClient:
    client = EastMoneyDataClient.from_env()
    return EastMoneyDataClient(
        user_id=args.user_id or client.user_id,
        ut_token=args.ut_token or client.ut_token,
        ct_token=args.ct_token or client.ct_token,
        app_user_agent=args.user_agent or client.app_user_agent,
        webview_user_agent=args.webview_user_agent or client.webview_user_agent,
        stock_card_client_version=args.stock_card_client_version or client.stock_card_client_version,
        platform=client.platform,
        device_id=args.device_id or client.device_id,
        em_gt=args.em_gt or client.em_gt,
        proxy=args.proxy or None,
        timeout=args.timeout,
        retries=args.retries,
        retry_sleep=args.retry_sleep,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch EastMoney stock sentiment data into Hive parquet.")
    parser.add_argument("--project-root", default=".", help="Target project directory.")
    parser.add_argument("--date", required=True, help="Universe date, format YYYY-MM-DD or YYYYMMDD.")
    parser.add_argument("--data-root", default=configured_data_root("EASTMONEY_DATA_ROOT"), help="Data root directory.")
    parser.add_argument("--table-name", default=DEFAULT_TABLE_NAME, help="Output table name under data root.")
    parser.add_argument("--codes", nargs="*", default=None, help="Optional code list, e.g. 600519.SH SZ300308.")
    parser.add_argument("--limit", type=int, default=None, help="Optional limit for smoke tests.")
    parser.add_argument("--workers", type=int, default=8, help="Concurrent stock workers.")
    parser.add_argument("--sleep", type=float, default=0.2, help="Sleep seconds before each stock request batch.")
    parser.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout seconds.")
    parser.add_argument("--retries", type=int, default=1, help="Retries per request.")
    parser.add_argument("--retry-sleep", type=float, default=1.0, help="Retry backoff base seconds.")
    parser.add_argument(
        "--all-history",
        action="store_true",
        help="Write every historical date returned by EastMoney. Default writes only --date.",
    )
    parser.add_argument("--failure-retry-rounds", type=int, default=3, help="Retry rounds for initially failed symbols.")
    parser.add_argument("--failure-retry-sleep", type=float, default=2.0, help="Sleep seconds before retrying failed symbols.")
    parser.add_argument("--proxy", default=None, help="Optional HTTP(S) proxy URL.")
    parser.add_argument(
        "--user-id",
        default=None,
        help="Optional EastMoney stockCard uid. Prefer EASTMONEY_STOCK_CARD_USER_ID env var.",
    )
    parser.add_argument("--ut-token", default=None, help="EastMoney UT token. Prefer EASTMONEY_MONI_UT_TOKEN env var.")
    parser.add_argument("--ct-token", default=None, help="EastMoney CT token. Prefer EASTMONEY_MONI_CT_TOKEN env var.")
    parser.add_argument("--user-agent", default=None, help="EastMoney app User-Agent.")
    parser.add_argument("--webview-user-agent", default=None, help="EastMoney webview User-Agent.")
    parser.add_argument("--device-id", default=None, help="EastMoney app device id.")
    parser.add_argument("--em-gt", default=None, help="EastMoney EM-GT header value.")
    parser.add_argument("--stock-card-client-version", default=None, help="EastMoney stockCard client version.")
    return parser.parse_args()


def main() -> str:
    configure_project()
    args = parse_args()
    universe_date = pd.to_datetime(args.date).date().isoformat()
    data_root = Path(args.data_root)
    client = build_client(args)

    codes = args.codes
    if codes is None:
        codes = load_codes_from_quotes_day(data_root, universe_date)
    codes = [normalize_code(code) for code in codes]
    if args.limit is not None:
        codes = codes[: args.limit]

    print(f"fetch_start universe_date={universe_date} symbols={len(codes)} workers={args.workers}", flush=True)
    target_row_date = None if args.all_history else universe_date
    rows: list[SentimentRow] = []
    failures: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(fetch_one, client, code, args.sleep): code for code in codes}
        for idx, future in enumerate(as_completed(futures), start=1):
            code = futures[future]
            try:
                rows.extend(filter_rows_for_date(future.result(), target_row_date))
            except Exception as exc:  # noqa: BLE001 - CLI should report and continue.
                failures.append((code, str(exc)))
            if idx % 100 == 0 or idx == len(futures):
                print(f"progress fetched={idx} total={len(futures)} rows={len(rows)} failures={len(failures)}", flush=True)

    for retry_round in range(1, max(0, args.failure_retry_rounds) + 1):
        if not failures:
            break
        retry_codes = [code for code, _message in failures]
        print(f"retry_start round={retry_round} symbols={len(retry_codes)}", flush=True)
        time.sleep(max(0.0, args.failure_retry_sleep))
        retry_failures: list[tuple[str, str]] = []
        for idx, code in enumerate(retry_codes, start=1):
            try:
                rows.extend(filter_rows_for_date(fetch_one(client, code, args.sleep), target_row_date))
            except Exception as exc:  # noqa: BLE001 - retry should retain per-symbol diagnostics.
                retry_failures.append((code, str(exc)))
            print(
                f"retry_progress round={retry_round} fetched={idx} total={len(retry_codes)} "
                f"rows={len(rows)} failures={len(retry_failures)}",
                flush=True,
            )
        failures = retry_failures

    if failures:
        for code, message in failures[:20]:
            print(f"failed code={code} error={message}", flush=True)
        raise RuntimeError(f"Failed to fetch {len(failures)} of {len(codes)} symbols")

    df = rows_to_frame(rows, target_date=target_row_date)
    written = write_upsert_partitioned(df, data_root, args.table_name)
    record_task_output(args.table_name, data_root / args.table_name, len(df))
    summary = f"table={args.table_name}; rows={len(df)}; partitions={len(written)}"
    print(f"write_done {summary}", flush=True)
    return summary


if __name__ == "__main__":
    main()
