"""Hive date partition I/O shared by the data collection tasks."""

from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


def write_date_file(df: pd.DataFrame, output_path: Path) -> None:
    """Write one Parquet part, with date supplied by the Hive directory."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(df.drop(columns=["date"]), preserve_index=False)
    pq.write_table(table, output_path, compression="zstd")


def write_date_snapshot(df: pd.DataFrame, data_root: Path, table_name: str, columns: list[str]) -> Path:
    if df.empty:
        raise ValueError("No rows to write")
    dates = sorted(pd.to_datetime(df["date"]).dt.date.unique())
    if len(dates) != 1:
        raise ValueError(f"Expected exactly one date partition, got {dates}")
    output_path = data_root / table_name / f"date={dates[0].isoformat()}" / "data_0.parquet"
    write_date_file(df[columns], output_path)
    return output_path


def upsert_date_partitions(
    df: pd.DataFrame,
    data_root: Path,
    table_name: str,
    *,
    merge_rows: Callable[[pd.DataFrame], pd.DataFrame],
) -> list[Path]:
    """Rewrite affected dates only; the task owns merge keys and row semantics."""
    if df.empty:
        return []
    output_root = data_root / table_name
    output_root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for dt, part_df in df.groupby("date", sort=True):
        trade_date = pd.to_datetime(dt).date().isoformat()
        part_dir = output_root / f"date={trade_date}"
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
            part_df = merge_rows(pd.concat([existing, part_df], ignore_index=True, sort=False))
            for path in existing_files:
                if path != output_path:
                    path.unlink()
        write_date_file(part_df, output_path)
        written.append(output_path)
    return written
