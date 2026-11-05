from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq





SKILL_ROOT = Path(__file__).resolve().parents[2]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from scripts.common.scheduler import configure_project, configured_data_root, record_task_output

from scripts.xueqiu.client import (
    ClashNodeSwitcher,
    TokenInvalidNotifier,
    XueqiuClient,
    add_xueqiu_auth_args,
    add_xueqiu_identity_args,
    create_runtime,
    load_codes_from_quotes_day,
    normalize_code,
    optional_int,
    parse_device_ids,
    parse_user_agents,
    to_xueqiu_symbol,
)



CHINA_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class CubeRebalanceRow:
    code: str
    date: str
    count_buy: int | None
    count_sell: int | None
    count_net: int | None
    count_total: int | None
    count_cube: int | None
    pre_diff: int | None
    update_time: str | None
    ingested_at: str


def timestamp_to_iso(value: Any) -> str | None:
    millis = optional_int(value)
    if millis is None:
        return None
    return datetime.fromtimestamp(millis / 1000, CHINA_TZ).isoformat()


def timestamp_to_date(value: Any) -> str | None:
    millis = optional_int(value)
    if millis is None:
        return None
    return datetime.fromtimestamp(millis / 1000, CHINA_TZ).date().isoformat()


def parse_trade_data(code: str, payload: dict[str, Any], ingested_at: str) -> list[CubeRebalanceRow]:
    data = payload.get("data") or {}
    update_time = timestamp_to_iso(data.get("update_time"))
    count_cube = optional_int(data.get("count_cube"))
    pre_diff = optional_int(data.get("pre_diff"))
    rows: list[CubeRebalanceRow] = []
    for item in data.get("count_list") or []:
        trade_date = timestamp_to_date(item.get("time"))
        if trade_date is None:
            continue
        count_buy = optional_int(item.get("count_buy"))
        count_sell = optional_int(item.get("count_sell"))
        count_net = None if count_buy is None or count_sell is None else count_buy - count_sell
        count_total = None if count_buy is None or count_sell is None else count_buy + count_sell
        rows.append(
            CubeRebalanceRow(
                code=code,
                date=trade_date,
                count_buy=count_buy,
                count_sell=count_sell,
                count_net=count_net,
                count_total=count_total,
                count_cube=count_cube,
                pre_diff=pre_diff,
                update_time=update_time,
                ingested_at=ingested_at,
            )
        )
    return rows


def fetch_one(client: XueqiuClient, code: str, sleep_seconds: float) -> list[CubeRebalanceRow]:
    normalized = normalize_code(code)
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)
    payload = client.get_cube_trade_data(to_xueqiu_symbol(normalized))
    ingested_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return parse_trade_data(normalized, payload, ingested_at)


def rows_to_frame(rows: list[CubeRebalanceRow]) -> pd.DataFrame:
    columns = [
        "code",
        "date",
        "count_buy",
        "count_sell",
        "count_net",
        "count_total",
        "count_cube",
        "pre_diff",
        "update_time",
        "ingested_at",
    ]
    if not rows:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame([row.__dict__ for row in rows])
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["update_time"] = pd.to_datetime(df["update_time"], utc=True)
    df["ingested_at"] = pd.to_datetime(df["ingested_at"], utc=True)
    for column in ["count_buy", "count_sell", "count_net", "count_total", "count_cube", "pre_diff"]:
        df[column] = pd.to_numeric(df[column], errors="coerce").astype("Int64")
    return df[columns].sort_values(["date", "code"]).reset_index(drop=True)


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
            combined = pd.concat([existing, part_df], ignore_index=True, sort=False)
            combined = (
                combined.sort_values(["code", "date", "ingested_at"])
                .groupby(["code", "date"], as_index=False, sort=True)
                .last()
            )
            for path in existing_files:
                if path != output_path:
                    path.unlink()
            part_df = combined
        table = pa.Table.from_pandas(part_df.drop(columns=["date"]), preserve_index=False)
        pq.write_table(table, output_path, compression="zstd")
        written.append(output_path)
    return written


def fetch_codes(
    *,
    client: XueqiuClient,
    codes: list[str],
    workers: int,
    sleep_seconds: float,
) -> tuple[list[CubeRebalanceRow], list[tuple[str, str]]]:
    rows: list[CubeRebalanceRow] = []
    failures: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(fetch_one, client, code, sleep_seconds): code for code in codes}
        for idx, future in enumerate(as_completed(futures), start=1):
            code = futures[future]
            try:
                rows.extend(future.result())
            except Exception as exc:  # noqa: BLE001 - CLI should report and continue.
                failures.append((code, str(exc)))
            if idx % 100 == 0 or idx == len(futures):
                print(f"progress fetched={idx} total={len(futures)} rows={len(rows)} failures={len(failures)}", flush=True)
    return rows, failures


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Xueqiu cube rebalance trade counts.")
    parser.add_argument("--project-root", default=".", help="Target project directory.")
    parser.add_argument("--date", required=True, help="Universe date, format YYYY-MM-DD or YYYYMMDD.")
    parser.add_argument("--data-root", default=configured_data_root("XUEQIU_DATA_ROOT"), help="Data root directory.")
    parser.add_argument("--table-name", default="xueqiu_cube_rebalance", help="Output table name under data root.")
    add_xueqiu_auth_args(parser)
    add_xueqiu_identity_args(parser)
    parser.add_argument("--codes", nargs="*", default=None, help="Optional code list, e.g. 002384.SZ SH688008.")
    parser.add_argument("--limit", type=int, default=None, help="Optional limit for smoke tests.")
    parser.add_argument("--workers", type=int, default=2, help="Concurrent symbol workers.")
    parser.add_argument("--sleep", type=float, default=0.05, help="Sleep seconds before each symbol request.")
    parser.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout seconds.")
    parser.add_argument("--retries", type=int, default=3, help="Retries per request.")
    parser.add_argument("--failure-retry-rounds", type=int, default=1, help="Retry rounds for symbols that failed first pass.")
    parser.add_argument("--failure-retry-workers", type=int, default=1, help="Workers for failed-symbol retry rounds.")
    parser.add_argument("--failure-retry-sleep", type=float, default=2.0, help="Sleep seconds for failed-symbol retry rounds.")
    parser.add_argument("--failure-retry-timeout", type=float, default=30.0, help="HTTP timeout for failed-symbol retry rounds.")
    parser.add_argument("--failure-retry-retries", type=int, default=5, help="HTTP retries for failed-symbol retry rounds.")
    return parser.parse_args()


def main() -> str:
    configure_project()
    args = parse_args()
    universe_date = pd.to_datetime(args.date).date().isoformat()
    data_root = Path(args.data_root)
    tokens, proxy, clash_switcher, notifier = create_runtime(args)
    user_agents = parse_user_agents(args)
    device_ids = parse_device_ids(args)
    print(f"user_agent_pool={len(user_agents)} device_id_pool={len(device_ids)} route_identity_binding=1", flush=True)

    codes = args.codes
    if codes is None:
        codes = load_codes_from_quotes_day(data_root, universe_date)
    codes = [normalize_code(code) for code in codes]
    if args.limit is not None:
        codes = codes[: args.limit]

    client = XueqiuClient(
        tokens,
        host="api.xueqiu.com",
        proxy=proxy,
        clash_switcher=clash_switcher,
        notifier=notifier,
        accept_language="zh-CN,zh-Hans;q=0.9",
        user_agents=user_agents,
        device_ids=device_ids,
        timeout=args.timeout,
        retries=args.retries,
    )
    print(f"fetch_start universe_date={universe_date} symbols={len(codes)} workers={args.workers}")
    rows, failures = fetch_codes(client=client, codes=codes, workers=args.workers, sleep_seconds=args.sleep)
    all_rows = rows
    remaining_failures = failures
    for retry_round in range(1, max(0, args.failure_retry_rounds) + 1):
        if not remaining_failures:
            break
        retry_codes = [code for code, _ in remaining_failures]
        retry_client = XueqiuClient(
            tokens,
            host="api.xueqiu.com",
            proxy=proxy,
            clash_switcher=clash_switcher,
            notifier=notifier,
            accept_language="zh-CN,zh-Hans;q=0.9",
            user_agents=user_agents,
            device_ids=device_ids,
            timeout=args.failure_retry_timeout,
            retries=args.failure_retry_retries,
        )
        print(
            f"retry_start round={retry_round} symbols={len(retry_codes)} "
            f"workers={args.failure_retry_workers} sleep={args.failure_retry_sleep} "
            f"retries={args.failure_retry_retries}",
            flush=True,
        )
        retry_rows, remaining_failures = fetch_codes(
            client=retry_client,
            codes=retry_codes,
            workers=args.failure_retry_workers,
            sleep_seconds=args.failure_retry_sleep,
        )
        all_rows.extend(retry_rows)

    df = rows_to_frame(all_rows)
    written = write_upsert_partitioned(df, data_root, args.table_name)
    record_task_output(args.table_name, data_root / args.table_name, len(df))
    summary = f"table={args.table_name}; rows={len(df)}; partitions={len(written)}"
    print(f"write_done {summary}")
    if remaining_failures:
        for code, message in remaining_failures[:20]:
            print(f"failed code={code} error={message}")
        raise RuntimeError(f"Failed to fetch {len(remaining_failures)} of {len(codes)} symbols after retry")
    return summary


if __name__ == "__main__":
    main()
