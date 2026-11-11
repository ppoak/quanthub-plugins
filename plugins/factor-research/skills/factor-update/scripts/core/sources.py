"""Resolve upstream calendars, dependencies and partition dates."""
from __future__ import annotations
import re
from datetime import date
from pathlib import Path
from typing import Any, Sequence
from factor_protocol import FactorStorage

def _partition_dates(root: Path) -> list[str]:
    if not root.exists():
        return []
    dates: list[str] = []
    for partition in root.glob("date=*"):
        if not partition.is_dir():
            continue
        value = partition.name.removeprefix("date=")
        try:
            date.fromisoformat(value)
        except ValueError:
            continue
        if any(
            file.is_file() and file.stat().st_size > 0
            for file in partition.rglob("*.parquet")
        ):
            dates.append(value)
    return sorted(set(dates))


def _update_source_dates(
    storage: FactorStorage,
    *,
    calendar: str,
    module: Any,
    source_factors: Sequence[str] = (),
) -> list[str]:
    if calendar != "quotes_common":
        available_dates = set(_partition_dates(storage.table_root(calendar)))
    else:
        tables = tuple(
            str(value)
            for value in getattr(
                module,
                "UPDATE_CALENDAR_TABLES",
                ("quotes_day", "quotes_min"),
            )
        )
        if not tables:
            raise ValueError("UPDATE_CALENDAR_TABLES must contain upstream table names")
        for table in tables:
            FactorStorage.identifier(table)
        date_sets = [set(_partition_dates(storage.table_root(table))) for table in tables]
        if not date_sets or any(not values for values in date_sets):
            return []
        available_dates = set.intersection(*date_sets)

    # A dependent factor cannot produce a complete row after any upstream
    # factor stops publishing partitions.  Use the common date range rather
    # than advancing the dependent factor with NULL source columns.
    for factor in source_factors:
        factor_dates = set(_partition_dates(storage.table_root(factor)))
        if not factor_dates:
            return []
        available_dates.intersection_update(factor_dates)
    return sorted(available_dates)


def _update_source_tables(module: Any, *, calendar: str) -> tuple[str, ...]:
    """Return raw datasets whose rewrites can invalidate recent outputs."""

    explicit = getattr(module, "UPDATE_SOURCE_TABLES", None)
    if explicit is None:
        if calendar == "quotes_common":
            explicit = getattr(
                module,
                "UPDATE_CALENDAR_TABLES",
                ("quotes_day", "quotes_min"),
            )
        else:
            explicit = (calendar,)
    tables = tuple(dict.fromkeys(str(value) for value in explicit))
    if not tables:
        raise ValueError("UPDATE_SOURCE_TABLES must contain upstream table names")
    for table in tables:
        FactorStorage.identifier(table)
    return tables


def _update_source_factors(module: Any) -> tuple[str, ...]:
    """Return materialized factor datasets that feed the current factor."""

    explicit = getattr(module, "UPDATE_SOURCE_FACTORS", ())
    if isinstance(explicit, str):
        explicit = (explicit,)
    factors = tuple(
        dict.fromkeys(str(value).strip() for value in explicit if str(value).strip())
    )
    for factor in factors:
        if not re.fullmatch(r"[A-Za-z0-9_]+", factor):
            raise ValueError(
                "UPDATE_SOURCE_FACTORS must contain identifier-safe factor names"
            )
    return factors


def _partition_mtime_ns(partition: Path) -> int:
    mtimes = [
        path.stat().st_mtime_ns
        for path in partition.rglob("*.parquet")
        if path.is_file()
    ]
    return max(mtimes, default=0)


def resolve_update_calendar(module: Any) -> str:
    """Return the declared upstream table or the common-table calendar."""
    return FactorStorage.identifier(getattr(module, "UPDATE_CALENDAR", "quotes_day"))
