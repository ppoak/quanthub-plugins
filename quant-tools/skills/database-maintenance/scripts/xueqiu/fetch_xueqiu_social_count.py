from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq




SKILL_ROOT = Path(__file__).resolve().parents[2]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from scripts.common.scheduler import configure_project, configured_data_root, record_task_output

from scripts.xueqiu.client import (
    XueqiuClient,
    add_xueqiu_auth_args,
    add_xueqiu_identity_args,
    create_runtime,
    load_codes_from_quotes_day,
    normalize_code,
    parse_device_ids,
    parse_user_agents,
    to_xueqiu_symbol,
)




@dataclass(frozen=True)
class XueqiuSocialCountRow:
    code: str
    date: str
    followers: int | None
    discussions: int | None
    ingested_at: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Xueqiu social-count data into Hive-partitioned parquet.")
    parser.add_argument("--project-root", default=".", help="Target project directory.")
    parser.add_argument("--date", required=True, help="Date, format YYYY-MM-DD or YYYYMMDD.")
    parser.add_argument("--data-root", default=configured_data_root("XUEQIU_DATA_ROOT"), help="Data root directory.")
    parser.add_argument("--table-name", default="xueqiu_social_count", help="Output table name.")
    add_xueqiu_auth_args(parser)
    add_xueqiu_identity_args(parser)
    parser.add_argument("--codes", nargs="*", default=None, help="Optional code list, e.g. 000001.SZ SZ301439.")
    parser.add_argument("--limit", type=int, default=None, help="Optional limit for smoke tests.")
    parser.add_argument("--workers", type=int, default=2, help="Concurrent fetch workers.")
    parser.add_argument("--sleep", type=float, default=0.05, help="Sleep seconds between endpoint requests.")
    parser.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout seconds.")
    parser.add_argument("--retries", type=int, default=3, help="Retries per endpoint.")
    parser.add_argument("--failure-retry-rounds", type=int, default=1, help="Retry rounds for symbols that failed first pass.")
    parser.add_argument("--failure-retry-workers", type=int, default=1, help="Workers for failed-symbol retry rounds.")
    parser.add_argument("--failure-retry-sleep", type=float, default=1.0, help="Sleep seconds for failed-symbol retry rounds.")
    parser.add_argument("--failure-retry-timeout", type=float, default=30.0, help="HTTP timeout for failed-symbol retry rounds.")
    parser.add_argument("--failure-retry-retries", type=int, default=3, help="HTTP retries for failed-symbol retry rounds.")
    return parser.parse_args()


def fetch_one(
    *,
    client: XueqiuClient,
    code: str,
    trade_date: str,
    sleep_seconds: float,
) -> XueqiuSocialCountRow:
    normalized = normalize_code(code)
    xueqiu_symbol = to_xueqiu_symbol(normalized)
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)
    followers = client.get_followers(xueqiu_symbol)
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)
    discussions = client.get_discussions(xueqiu_symbol)
    return XueqiuSocialCountRow(
        code=normalized,
        date=trade_date,
        followers=followers,
        discussions=discussions,
        ingested_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    )


def rows_to_frame(rows: list[XueqiuSocialCountRow]) -> pd.DataFrame:
    df = pd.DataFrame([row.__dict__ for row in rows])
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["followers"] = pd.to_numeric(df["followers"], errors="coerce").astype("Int64")
    df["discussions"] = pd.to_numeric(df["discussions"], errors="coerce").astype("Int64")
    df["ingested_at"] = pd.to_datetime(df["ingested_at"], utc=True)
    return df.sort_values(["date", "code"]).reset_index(drop=True)


def fetch_codes(
    *,
    client: XueqiuClient,
    codes: list[str],
    trade_date: str,
    workers: int,
    sleep_seconds: float,
) -> tuple[list[XueqiuSocialCountRow], list[tuple[str, str]]]:
    if not codes:
        raise ValueError("No codes to fetch")

    rows: list[XueqiuSocialCountRow] = []
    failures: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(
                fetch_one,
                client=client,
                code=code,
                trade_date=trade_date,
                sleep_seconds=sleep_seconds,
            ): code
            for code in codes
        }
        for idx, future in enumerate(as_completed(futures), start=1):
            code = futures[future]
            try:
                rows.append(future.result())
            except Exception as exc:  # noqa: BLE001 - CLI should report and continue.
                failures.append((code, str(exc)))
            if idx % 100 == 0 or idx == len(futures):
                print(f"progress fetched={idx} total={len(futures)} rows={len(rows)} failures={len(failures)}", flush=True)
    return rows, failures


def write_hive_partition(df: pd.DataFrame, data_root: Path, table_name: str, columns: list[str]) -> Path:
    if df.empty:
        raise ValueError("No rows to write")
    dates = sorted(pd.to_datetime(df["date"]).dt.date.unique())
    if len(dates) != 1:
        raise ValueError(f"Expected exactly one date partition, got {dates}")
    trade_date = dates[0].isoformat()
    output_dir = data_root / table_name / f"date={trade_date}"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "data_0.parquet"
    ordered = df[columns].drop(columns=["date"]).copy()
    table = pa.Table.from_pandas(ordered, preserve_index=False)
    pq.write_table(table, output_path, compression="zstd")
    return output_path


def main() -> str:
    configure_project()
    args = parse_args()
    trade_date = pd.to_datetime(args.date).date().isoformat()
    data_root = Path(args.data_root)
    tokens, proxy, clash_switcher, notifier = create_runtime(args)
    user_agents = parse_user_agents(args)
    device_ids = parse_device_ids(args)
    print(f"user_agent_pool={len(user_agents)} device_id_pool={len(device_ids)} route_identity_binding=1", flush=True)

    codes = args.codes
    if codes is None:
        codes = load_codes_from_quotes_day(data_root, trade_date)
    codes = [normalize_code(code) for code in codes]
    if args.limit is not None:
        codes = codes[: args.limit]

    print(f"fetch_start table={args.table_name} date={trade_date} symbols={len(codes)} workers={args.workers}")
    client = XueqiuClient(
        tokens,
        host="stock.xueqiu.com",
        proxy=proxy,
        clash_switcher=clash_switcher,
        notifier=notifier,
        user_agent=user_agents[0] if user_agents else "Xueqiu iPhone 14.91",
        device_id=None,
        timeout=args.timeout,
        retries=args.retries,
        user_agents=user_agents,
        device_ids=device_ids,
    )
    rows, failures = fetch_codes(
        client=client,
        codes=codes,
        trade_date=trade_date,
        workers=args.workers,
        sleep_seconds=args.sleep,
    )
    all_rows = rows
    remaining_failures = failures
    for retry_round in range(1, max(0, args.failure_retry_rounds) + 1):
        if not remaining_failures:
            break
        retry_codes = [code for code, _ in remaining_failures]
        retry_client = XueqiuClient(
            tokens,
            host="stock.xueqiu.com",
            proxy=proxy,
            clash_switcher=clash_switcher,
            notifier=notifier,
            user_agent=user_agents[0] if user_agents else "Xueqiu iPhone 14.91",
            device_id=None,
            timeout=args.failure_retry_timeout,
            retries=args.failure_retry_retries,
            user_agents=user_agents,
            device_ids=device_ids,
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
            trade_date=trade_date,
            workers=args.failure_retry_workers,
            sleep_seconds=args.failure_retry_sleep,
        )
        all_rows.extend(retry_rows)

    df = rows_to_frame(all_rows)
    output_path = write_hive_partition(
        df,
        data_root,
        args.table_name,
        ["code", "date", "followers", "discussions", "ingested_at"],
    )
    summary = f"date={trade_date}; table={args.table_name}; symbols={len(codes)}; rows={len(df)}"
    record_task_output(args.table_name, output_path, len(df), "partition replacement")
    print(f"write_done table={args.table_name} path={output_path.as_posix()} rows={len(df)}")
    if remaining_failures:
        for code, message in remaining_failures[:20]:
            print(f"failed code={code} error={message}")
        raise RuntimeError(f"Failed to fetch {len(remaining_failures)} of {len(codes)} symbols after retry")
    print(summary)
    return summary


if __name__ == "__main__":
    main()
