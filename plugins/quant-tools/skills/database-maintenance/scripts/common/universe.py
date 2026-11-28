"""Stock symbols and universe selection for local daily quotes."""

from datetime import date, datetime
from pathlib import Path


def normalize_code(code: str) -> str:
    raw = code.strip().upper()
    if "." in raw:
        return raw
    if raw.startswith(("SH", "SZ", "BJ")):
        return f"{raw[2:]}.{raw[:2]}"
    if raw.startswith(("5", "6", "9")):
        return f"{raw}.SH"
    if raw.startswith(("0", "1", "2", "3")):
        return f"{raw}.SZ"
    if raw.startswith(("4", "8")):
        return f"{raw}.BJ"
    raise ValueError(f"Cannot infer normalized code from code: {code}")


def latest_quotes_day_partition(data_root: Path, trade_date: str) -> tuple[str, Path]:
    quotes_root = data_root / "quotes_day"
    if not quotes_root.exists():
        raise ValueError(f"quotes_day table does not exist: {quotes_root.as_posix()}")

    target_date = datetime.fromisoformat(trade_date).date()
    candidates: list[tuple[date, Path]] = []
    for path in quotes_root.glob("date=*"):
        if not path.is_dir():
            continue
        try:
            partition_date = datetime.fromisoformat(path.name.split("=", 1)[1]).date()
        except ValueError:
            continue
        if partition_date <= target_date and any(path.glob("*.parquet")):
            candidates.append((partition_date, path))

    if not candidates:
        raise ValueError(f"No quotes_day universe available on or before {trade_date}")
    partition_date, partition_path = max(candidates, key=lambda item: item[0])
    return partition_date.isoformat(), partition_path


def load_codes_from_quotes_day(data_root: Path, trade_date: str) -> list[str]:
    import duckdb

    universe_date, partition_path = latest_quotes_day_partition(data_root, trade_date)
    quotes_glob = (partition_path / "*.parquet").as_posix()
    print(f"universe_date={universe_date}")
    with duckdb.connect(database=":memory:") as con:
        rows = con.execute(
            """
            SELECT DISTINCT code
            FROM parquet_scan(?, union_by_name=1)
            WHERE code IS NOT NULL
            ORDER BY code
            """,
            [quotes_glob],
        ).fetchall()
    return [str(row[0]) for row in rows]
