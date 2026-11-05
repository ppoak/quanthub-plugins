from __future__ import annotations

import argparse
import sys
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
    normalize_code,
    optional_int,
    optional_int_from_float,
    parse_device_ids,
    parse_user_agents,
)




RANK_TYPE_LABELS = {
    12: "a_share_1h_heat",
    22: "a_share_24h_heat",
}


@dataclass(frozen=True)
class XueqiuHotRankRow:
    code: str
    date: str
    rank_type: int
    rank_type_label: str
    source_rank: int
    value: int | None
    value_increment: int | None
    rank_change: int | None
    ingested_at: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Xueqiu hot-rank data into Hive-partitioned parquet.")
    parser.add_argument("--project-root", default=".", help="Target project directory.")
    parser.add_argument("--date", required=True, help="Date, format YYYY-MM-DD or YYYYMMDD.")
    parser.add_argument("--data-root", default=configured_data_root("XUEQIU_DATA_ROOT"), help="Data root directory.")
    parser.add_argument("--table-name", default="xueqiu_hot_rank", help="Output table name.")
    add_xueqiu_auth_args(parser)
    add_xueqiu_identity_args(parser)
    parser.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout seconds.")
    parser.add_argument("--retries", type=int, default=3, help="Retries per endpoint.")
    parser.add_argument("--hot-size", type=int, default=100, help="Hot stock list size. Xueqiu currently caps it at 100.")
    parser.add_argument("--hot-rank-types", nargs="*", type=int, default=[12, 22], help="Xueqiu hot stock list type values.")
    return parser.parse_args()


def fetch_hot_rank(
    *,
    client: XueqiuClient,
    trade_date: str,
    rank_types: list[int],
    hot_size: int,
) -> pd.DataFrame:
    rows: list[XueqiuHotRankRow] = []
    for rank_type in rank_types:
        items = client.get_hot_stock_items(rank_type=rank_type, size=hot_size)
        print(f"hot_rank rank_type={rank_type} rows={len(items)}")
        ingested_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        for source_rank, item in enumerate(items, start=1):
            symbol = item.get("symbol")
            if symbol is None:
                continue
            try:
                code = normalize_code(str(symbol))
            except ValueError:
                continue
            if not code.endswith((".SH", ".SZ", ".BJ")):
                continue
            rows.append(
                XueqiuHotRankRow(
                    code=code,
                    date=trade_date,
                    rank_type=rank_type,
                    rank_type_label=RANK_TYPE_LABELS.get(rank_type, f"type_{rank_type}"),
                    source_rank=source_rank,
                    value=optional_int_from_float(item.get("value")),
                    value_increment=optional_int_from_float(item.get("increment")),
                    rank_change=optional_int(item.get("rank_change")),
                    ingested_at=ingested_at,
                )
            )
    columns = [
        "code",
        "date",
        "rank_type",
        "rank_type_label",
        "source_rank",
        "value",
        "value_increment",
        "rank_change",
        "ingested_at",
    ]
    if not rows:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame([row.__dict__ for row in rows])
    df["date"] = pd.to_datetime(df["date"]).dt.date
    for column in ("rank_type", "source_rank", "value", "value_increment", "rank_change"):
        df[column] = pd.to_numeric(df[column], errors="coerce").astype("Int64")
    df["ingested_at"] = pd.to_datetime(df["ingested_at"], utc=True)
    return df[columns].sort_values(["date", "rank_type", "source_rank", "code"]).reset_index(drop=True)


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
    target_date = pd.to_datetime(args.date).date().isoformat()
    data_root = Path(args.data_root)
    tokens, proxy, clash_switcher, notifier = create_runtime(args)
    user_agents = parse_user_agents(args)
    device_ids = parse_device_ids(args)
    print(f"user_agent_pool={len(user_agents)} device_id_pool={len(device_ids)} route_identity_binding=1", flush=True)
    print(f"fetch_start table={args.table_name} date={target_date} rank_types={args.hot_rank_types}")

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
    df = fetch_hot_rank(
        client=client,
        trade_date=target_date,
        rank_types=args.hot_rank_types,
        hot_size=args.hot_size,
    )
    if df.empty:
        summary = f"date={target_date}; table={args.table_name}; rows=0"
        record_task_output(args.table_name, data_root / args.table_name, 0, "skipped: empty input")
        print(f"write_skipped table={args.table_name} rows=0")
        print(summary)
        return summary

    output_path = write_hive_partition(
        df,
        data_root,
        args.table_name,
        [
            "code",
            "date",
            "rank_type",
            "rank_type_label",
            "source_rank",
            "value",
            "value_increment",
            "rank_change",
            "ingested_at",
        ],
    )
    summary = f"date={target_date}; table={args.table_name}; rows={len(df)}"
    record_task_output(args.table_name, output_path, len(df), "partition replacement")
    print(f"write_done table={args.table_name} path={output_path.as_posix()} rows={len(df)}")
    print(summary)
    return summary


if __name__ == "__main__":
    main()
