from __future__ import annotations

import argparse
import html
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
DEFAULT_STATUS_USER_AGENTS = [
    "Xueqiu iPhone 14.91",
    "Xueqiu iPhone 14.90",
    "Xueqiu iPhone 14.89",
    "Xueqiu iPhone 14.88",
]


@dataclass(frozen=True)
class XueqiuStatusRow:
    symbol: str
    code: str
    date: str
    status_id: int
    created_at: str
    created_at_ms: int
    user_id: int | None
    screen_name: str | None
    text_plain: str
    text_length: int
    source: str | None
    reply_count: int | None
    retweet_count: int | None
    like_count: int | None
    ingested_at: str


def strip_html(text: str) -> str:
    without_tags = HTML_TAG_RE.sub(" ", text)
    decoded = html.unescape(without_tags)
    return WHITESPACE_RE.sub(" ", decoded).strip()


def status_timestamp_ms(item: dict[str, Any]) -> int | None:
    value = item.get("created_at")
    if value is None:
        return None
    return optional_int(value)


def status_to_row(
    *,
    query_symbol: str,
    code: str,
    trade_date: str,
    item: dict[str, Any],
    ingested_at: str,
) -> XueqiuStatusRow | None:
    created_at_ms = status_timestamp_ms(item)
    status_id = optional_int(item.get("id"))
    if created_at_ms is None or status_id is None:
        return None
    created_at_dt = datetime.fromtimestamp(created_at_ms / 1000, CHINA_TZ)
    text_html = str(item.get("text") or item.get("description") or "")
    text_plain = strip_html(text_html)
    user = item.get("user") or {}
    return XueqiuStatusRow(
        symbol=query_symbol,
        code=code,
        date=trade_date,
        status_id=status_id,
        created_at=created_at_dt.isoformat(),
        created_at_ms=created_at_ms,
        user_id=optional_int(user.get("id")),
        screen_name=user.get("screen_name"),
        text_plain=text_plain,
        text_length=len(text_plain),
        source=item.get("source"),
        reply_count=optional_int(item.get("reply_count") or item.get("comments_count")),
        retweet_count=optional_int(item.get("retweet_count") or item.get("retweets_count")),
        like_count=optional_int(item.get("like_count") or item.get("likes_count")),
        ingested_at=ingested_at,
    )


def fetch_status_for_code(
    *,
    tokens: list[str],
    code: str,
    trade_date: str,
    max_pages: int,
    count: int,
    timeout: float,
    retries: int,
    sleep_seconds: float,
    proxy: str | None,
    clash_switcher: ClashNodeSwitcher | None,
    notifier: TokenInvalidNotifier | None,
    user_agents: list[str],
    device_ids: list[str],
) -> tuple[list[XueqiuStatusRow], int]:
    pinned_route = None
    if clash_switcher is not None:
        pinned_route = clash_switcher.choose_random_route()
    client = XueqiuClient(
        tokens,
        host="api.xueqiu.com",
        proxy=proxy,
        clash_switcher=clash_switcher,
        notifier=notifier,
        user_agent=user_agents[0] if user_agents else DEFAULT_STATUS_USER_AGENTS[0],
        timeout=timeout,
        retries=retries,
        waf_405_message="Xueqiu request blocked by WAF with HTTP 405",
        pinned_route=pinned_route,
        user_agents=user_agents,
        device_ids=device_ids,
    )
    normalized = normalize_code(code)
    symbol = to_xueqiu_symbol(normalized)
    day_begin = datetime.fromisoformat(trade_date).replace(tzinfo=CHINA_TZ)
    day_end = day_begin + timedelta(days=1)
    rows: list[XueqiuStatusRow] = []
    pages_fetched = 0

    for page in range(1, max_pages + 1):
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
        data = client.search_status_page(symbol, page, count=count)
        pages_fetched += 1
        items = data.get("list") or data.get("data", {}).get("list") or []
        if not items:
            break

        should_stop = False
        ingested_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        for item in items:
            created_at_ms = status_timestamp_ms(item)
            if created_at_ms is None:
                continue
            created_at_dt = datetime.fromtimestamp(created_at_ms / 1000, CHINA_TZ)
            if day_begin <= created_at_dt < day_end:
                row = status_to_row(
                    query_symbol=symbol,
                    code=normalized,
                    trade_date=trade_date,
                    item=item,
                    ingested_at=ingested_at,
                )
                if row is not None:
                    rows.append(row)
            elif created_at_dt < day_begin:
                should_stop = True

        max_page = optional_int(data.get("maxPage")) or max_pages
        if should_stop or page >= min(max_pages, max_page):
            break

    return rows, pages_fetched


def rows_to_frame(rows: list[XueqiuStatusRow]) -> pd.DataFrame:
    columns = [
        "symbol",
        "code",
        "date",
        "status_id",
        "created_at",
        "created_at_ms",
        "user_id",
        "screen_name",
        "text_plain",
        "text_length",
        "source",
        "reply_count",
        "retweet_count",
        "like_count",
        "ingested_at",
    ]
    if not rows:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame([row.__dict__ for row in rows])
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["created_at"] = pd.to_datetime(df["created_at"], utc=True)
    df["ingested_at"] = pd.to_datetime(df["ingested_at"], utc=True)
    int_columns = [
        "status_id",
        "created_at_ms",
        "user_id",
        "text_length",
        "reply_count",
        "retweet_count",
        "like_count",
    ]
    for column in int_columns:
        df[column] = pd.to_numeric(df[column], errors="coerce").astype("Int64")
    return df[columns].sort_values(["code", "created_at_ms", "status_id"]).reset_index(drop=True)


def write_status_part(
    *,
    rows: list[XueqiuStatusRow],
    output_dir: Path,
    part_index: int,
) -> Path | None:
    df = rows_to_frame(rows)
    if df.empty:
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"part_{part_index:04d}.parquet"
    table = pa.Table.from_pandas(df.drop(columns=["date"]), preserve_index=False)
    pq.write_table(table, output_path, compression="zstd")
    return output_path


def clear_existing_parts(output_dir: Path) -> None:
    if not output_dir.exists():
        return
    for path in output_dir.glob("*.parquet"):
        path.unlink()


def backup_output_dir(output_dir: Path) -> Path | None:
    if not output_dir.exists():
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup_dir = output_dir.parent / f"{output_dir.name}.__backup_{timestamp}"
    shutil.move(str(output_dir), str(backup_dir))
    print(f"replace_existing_backup path={backup_dir.as_posix()}", flush=True)
    return backup_dir


def restore_output_backup(output_dir: Path, backup_dir: Path | None) -> None:
    if backup_dir is None or not backup_dir.exists():
        return
    if output_dir.exists():
        shutil.rmtree(output_dir)
    shutil.move(str(backup_dir), str(output_dir))
    print(f"replace_existing_backup_restored path={output_dir.as_posix()}", flush=True)


def remove_output_backup(backup_dir: Path | None) -> None:
    if backup_dir is not None and backup_dir.exists():
        shutil.rmtree(backup_dir)
        print(f"replace_existing_backup_removed path={backup_dir.as_posix()}", flush=True)


def latest_partition_dir(data_root: Path, table_name: str, trade_date: str) -> tuple[str, Path] | None:
    table_dir = data_root / table_name
    if not table_dir.exists():
        return None
    cutoff = pd.to_datetime(trade_date).date()
    candidates: list[tuple[str, Path]] = []
    for path in table_dir.glob("date=*"):
        if not path.is_dir():
            continue
        date_text = path.name.split("=", 1)[1]
        try:
            date_value = pd.to_datetime(date_text).date()
        except ValueError:
            continue
        if date_value <= cutoff and list(path.glob("*.parquet")):
            candidates.append((date_value.isoformat(), path))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item[0])[-1]


def read_latest_partition(data_root: Path, table_name: str, trade_date: str) -> tuple[str, pd.DataFrame] | None:
    latest = latest_partition_dir(data_root, table_name, trade_date)
    if latest is None:
        return None
    date_text, path = latest
    return date_text, pd.read_parquet(path)


def load_status_candidate_codes(
    *,
    data_root: Path,
    trade_date: str,
    candidate_limit: int,
    social_limit: int,
    hot_limit: int,
    cube_limit: int,
) -> list[str]:
    candidates: list[str] = []

    def add_codes(rows: list[tuple[str]]) -> int:
        added = 0
        seen = set(candidates)
        for (code,) in rows:
            normalized = normalize_code(str(code))
            if normalized not in seen:
                candidates.append(normalized)
                seen.add(normalized)
                added += 1
            if len(candidates) >= candidate_limit:
                break
        return added

    if hot_limit > 0:
        latest_hot = read_latest_partition(data_root, "xueqiu_hot_rank", trade_date)
        if latest_hot is not None:
            hot_date, hot_df = latest_hot
            hot_df = hot_df.sort_values(["rank_type", "source_rank"], na_position="last")
            rows = [(str(code),) for code in hot_df["code"].head(hot_limit).tolist()]
            print(f"candidate_source=xueqiu_hot_rank date={hot_date} rows={len(rows)} added={add_codes(rows)}")

    if social_limit > 0 and len(candidates) < candidate_limit:
        latest_social = read_latest_partition(data_root, "xueqiu_social_count", trade_date)
        if latest_social is not None:
            social_date, social_df = latest_social
            social_df = social_df.sort_values(["discussions", "followers"], ascending=[False, False], na_position="last")
            rows = [(str(code),) for code in social_df["code"].head(social_limit).tolist()]
            print(f"candidate_source=xueqiu_social_count date={social_date} rows={len(rows)} added={add_codes(rows)}")

    if cube_limit > 0 and len(candidates) < candidate_limit:
        latest_cube = read_latest_partition(data_root, "xueqiu_cube_rebalance", trade_date)
        if latest_cube is not None:
            cube_date, cube_df = latest_cube
            cube_df = cube_df.assign(abs_count_net=cube_df["count_net"].abs())
            cube_df = cube_df.sort_values(["count_total", "abs_count_net"], ascending=[False, False], na_position="last")
            rows = [(str(code),) for code in cube_df["code"].head(cube_limit).tolist()]
            print(f"candidate_source=xueqiu_cube_rebalance date={cube_date} rows={len(rows)} added={add_codes(rows)}")

    if len(candidates) > candidate_limit:
        candidates = candidates[:candidate_limit]
    print(f"candidate_mode=1 candidate_count={len(candidates)} candidate_limit={candidate_limit}")
    return candidates


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch Xueqiu per-status comments for a trade date into Hive-partitioned parquet."
    )
    parser.add_argument("--project-root", default=".", help="Target project directory.")
    parser.add_argument("--date", required=True, help="Trade date, format YYYY-MM-DD or YYYYMMDD.")
    parser.add_argument("--data-root", default=configured_data_root("XUEQIU_DATA_ROOT"), help="Data root directory.")
    parser.add_argument("--table-name", default="xueqiu_status", help="Output table name under data root.")
    add_xueqiu_auth_args(parser, default_clash_group="GLOBAL")
    parser.add_argument("--codes", nargs="*", default=None, help="Optional code list, e.g. 688008.SH SZ002384.")
    parser.add_argument("--limit", type=int, default=None, help="Optional limit for smoke tests.")
    parser.add_argument("--full-universe", action="store_true", help="Fetch all stocks from quotes_day instead of candidate mode.")
    parser.add_argument("--candidate-limit", type=int, default=500, help="Maximum stocks to fetch in candidate mode.")
    parser.add_argument("--candidate-social-limit", type=int, default=300, help="Top discussions stocks from xueqiu_social_count.")
    parser.add_argument("--candidate-hot-limit", type=int, default=300, help="Hot-rank stocks from xueqiu_hot_rank.")
    parser.add_argument("--candidate-cube-limit", type=int, default=200, help="Top cube-rebalance stocks from latest available xueqiu_cube_rebalance date.")
    add_xueqiu_identity_args(parser)
    parser.add_argument("--start-index", type=int, default=0, help="Zero-based start index in the resolved code list.")
    parser.add_argument("--part-index", type=int, default=0, help="Starting output part index.")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent symbol workers.")
    parser.add_argument("--batch-size", type=int, default=100, help="Symbols per output part.")
    parser.add_argument("--count", type=int, default=20, help="Requested rows per page. Xueqiu currently returns 20.")
    parser.add_argument("--max-pages", type=int, default=50, help="Maximum pages per symbol.")
    parser.add_argument("--sleep", type=float, default=0.02, help="Sleep seconds before each page request.")
    parser.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout seconds.")
    parser.add_argument("--retries", type=int, default=3, help="Retries per page.")
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="Replace an existing date partition. Without this flag, existing partitions fail fast.",
    )
    return parser.parse_args()


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
        if args.full_universe:
            codes = load_codes_from_quotes_day(data_root, trade_date)
        else:
            codes = load_status_candidate_codes(
                data_root=data_root,
                trade_date=trade_date,
                candidate_limit=args.candidate_limit,
                social_limit=args.candidate_social_limit,
                hot_limit=args.candidate_hot_limit,
                cube_limit=args.candidate_cube_limit,
            )
            if not codes:
                print("candidate_mode_empty=1 fallback=full_universe")
                codes = load_codes_from_quotes_day(data_root, trade_date)
    codes = [normalize_code(code) for code in codes]
    if args.limit is not None:
        codes = codes[: args.limit]
    if args.start_index:
        codes = codes[args.start_index :]

    output_dir = data_root / args.table_name / f"date={trade_date}"
    backup_dir = None
    had_existing_output = output_dir.exists()
    existing_parts = list(output_dir.glob("*.parquet")) if output_dir.exists() else []
    if existing_parts and not args.replace_existing:
        raise FileExistsError(
            f"Refusing to replace existing partition without --replace-existing: {output_dir.as_posix()}"
        )
    if args.replace_existing:
        backup_dir = backup_output_dir(output_dir)

    try:
        print(
            f"fetch_start date={trade_date} symbols={len(codes)} workers={args.workers} "
            f"max_pages={args.max_pages} start_index={args.start_index} part_index={args.part_index}",
            flush=True,
        )

        total_rows = 0
        total_pages = 0
        failures: list[tuple[str, str]] = []
        part_index = args.part_index

        for batch_begin in range(0, len(codes), args.batch_size):
            batch_codes = codes[batch_begin : batch_begin + args.batch_size]
            batch_rows: list[XueqiuStatusRow] = []
            with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
                futures = {
                    executor.submit(
                        fetch_status_for_code,
                        tokens=tokens,
                        code=code,
                        trade_date=trade_date,
                        max_pages=args.max_pages,
                        count=args.count,
                        timeout=args.timeout,
                        retries=args.retries,
                        sleep_seconds=args.sleep,
                        proxy=proxy,
                        clash_switcher=clash_switcher,
                        notifier=notifier,
                        user_agents=user_agents,
                        device_ids=device_ids,
                    ): code
                    for code in batch_codes
                }
                for future in as_completed(futures):
                    code = futures[future]
                    try:
                        rows, pages = future.result()
                        batch_rows.extend(rows)
                        total_pages += pages
                    except Exception as exc:  # noqa: BLE001 - CLI should report and continue.
                        failures.append((code, str(exc)))

            output_path = write_status_part(rows=batch_rows, output_dir=output_dir, part_index=part_index)
            total_rows += len(batch_rows)
            done = min(batch_begin + len(batch_codes), len(codes))
            print(
                f"progress symbols={done}/{len(codes)} rows={total_rows} pages={total_pages} "
                f"failures={len(failures)} part={output_path.as_posix() if output_path else 'empty'}",
                flush=True,
            )
            waf_failures = [failure for failure in failures if "blocked by WAF" in failure[1]]
            if waf_failures:
                code, message = waf_failures[0]
                raise RuntimeError(f"Stopped after WAF block at code={code}: {message}")
            part_index += 1

        if failures:
            for code, message in failures[:20]:
                print(f"failed code={code} error={message}")
            raise RuntimeError(f"Failed to fetch {len(failures)} of {len(codes)} symbols")

        remove_output_backup(backup_dir)
        record_task_output(args.table_name, output_dir, total_rows, "partition replacement")
        summary = f"path={output_dir.as_posix()}; rows={total_rows}; pages={total_pages}; parts={part_index}"
        print(f"write_done {summary}", flush=True)
        return summary
    except Exception:
        if backup_dir is None and not had_existing_output and output_dir.exists():
            shutil.rmtree(output_dir)
            print(f"partial_output_removed path={output_dir.as_posix()}", flush=True)
        else:
            restore_output_backup(output_dir, backup_dir)
        raise


if __name__ == "__main__":
    main()
