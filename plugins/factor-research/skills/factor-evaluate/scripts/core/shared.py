"""Shared contracts, resource settings and helpers for DuckDB evaluation."""

from __future__ import annotations

import math
import os
import shutil
from time import monotonic
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import duckdb
from .check import EVALUATION_FILES, evaluation_files, evaluation_files_match
from factor_protocol.artifacts import (
    copy_if_present,
    dataset_manifest,
    manifests_match,
    read_config,
    replace_column_directory,
    resolve_output_root,
    stage_column_directory,
    write_config,
)
from factor_protocol.loader import (
    load_module_from_path,
    resolve_factor_package,
    select_factor_columns,
)
from factor_protocol.paths import FactorPath, validate_identifier
from factor_protocol.storage import FactorStorage
from .selection import VARIANTS as EVALUATION_VARIANTS

# Explicit shared imports used by the flat evaluation modules.
__all__ = [
    "EVALUATION_FILES",
    "evaluation_files",
    "evaluation_files_match",
    "math",
    "os",
    "shutil",
    "dataclass",
    "date",
    "datetime",
    "timezone",
    "Path",
    "Any",
    "Iterable",
    "Mapping",
    "Sequence",
    "duckdb",
    "EVALUATION_VARIANTS",
    "copy_if_present",
    "dataset_manifest",
    "manifests_match",
    "read_config",
    "replace_column_directory",
    "resolve_output_root",
    "stage_column_directory",
    "write_config",
    "FactorPath",
    "validate_identifier",
    "load_module_from_path",
    "resolve_factor_package",
    "select_factor_columns",
    "FactorStorage",
]


TRANSACTION_COST_BPS = 5.0
TRANSACTION_COST = TRANSACTION_COST_BPS / 10_000.0

DEFAULT_EVALUATION_BEGIN = "2014-01-01"
DEFAULT_TRAIN_END = "2022-12-31"
DEFAULT_VALIDATION_END = "2024-12-31"
DEFAULT_ROLLING_MEAN_DAYS = 20
DEFAULT_NEUTRALIZATION_INDUSTRY_LEVEL = "first_industry_code"
DEFAULT_COLUMN_BATCH_SIZE = 8
DEFAULT_DATE_BATCH_DAYS = 252
# Default trading-date block for single-column evaluation.
SINGLE_COLUMN_DATE_BATCH_DAYS = 126
# Refreshes run under the nightly 1GB contract.  Keep dense multi-column
# factors on one-column, short date blocks so an UNPIVOT cannot materialize
# tens of millions of rows at once and spill the entire drive.
REFRESH_COLUMN_BATCH_SIZE = 1
REFRESH_DATE_BATCH_DAYS = 31
# Reference-only refreshes use the same short date block as normal nightly
# refreshes.  A wider block multiplies the factor/reference rank panels and
# can exceed the DuckDB memory limit for wide packages such as chip factors.
REFERENCE_REFRESH_DATE_BATCH_DAYS = 31
REFERENCE_CORRELATION_BATCH_SIZE = 4
PROBE_DATE_DAYS = 20


@dataclass(frozen=True)
class EvaluationSettings:
    """User-adjustable evaluation defaults loaded from the project dotenv."""

    begin: str = DEFAULT_EVALUATION_BEGIN
    train_end: str = DEFAULT_TRAIN_END
    validation_end: str = DEFAULT_VALIDATION_END
    horizon_days: int = 5
    execution_lag_days: int = 1
    price_col: str = "close_post"
    quantiles: int = 10
    top_percentile: float = 1.0
    rolling_mean_days: int = DEFAULT_ROLLING_MEAN_DAYS
    neutralization_industry_level: str = DEFAULT_NEUTRALIZATION_INDUSTRY_LEVEL
    threads: int = 2
    memory_limit: str = "4GB"
    temp_directory: str = ".tmp/factor_evaluation"
    workers: int = 1
    intra_column_workers: int = 1
    worker_column_group_size: int = 1
    column_batch_size: int | None = None
    date_batch_days: int | None = None
    default_column_batch_size: int = DEFAULT_COLUMN_BATCH_SIZE
    default_date_batch_days: int = DEFAULT_DATE_BATCH_DAYS
    single_column_date_batch_days: int = SINGLE_COLUMN_DATE_BATCH_DAYS
    refresh_column_batch_size: int = REFRESH_COLUMN_BATCH_SIZE
    refresh_date_batch_days: int = REFRESH_DATE_BATCH_DAYS
    reference_refresh_date_batch_days: int = REFERENCE_REFRESH_DATE_BATCH_DAYS
    reference_correlation_batch_size: int = REFERENCE_CORRELATION_BATCH_SIZE
    probe_date_days: int = PROBE_DATE_DAYS

    @classmethod
    def from_project_root(cls, project_root: str | Path) -> "EvaluationSettings":
        root = Path(project_root).expanduser().resolve()
        dotenv = _read_dotenv(root / ".env")

        def value(name: str, default: str) -> str:
            configured = os.environ.get(name, dotenv.get(name, default)).strip()
            return configured or default

        def integer(name: str, default: int, *, optional: bool = False) -> int | None:
            raw = value(name, "" if optional else str(default))
            return None if optional and not raw else int(raw)

        def number(name: str, default: float) -> float:
            return float(value(name, str(default)))

        settings = cls(
            begin=value("FACTOR_EVALUATE_BEGIN", DEFAULT_EVALUATION_BEGIN),
            train_end=value("FACTOR_EVALUATE_TRAIN_END", DEFAULT_TRAIN_END),
            validation_end=value(
                "FACTOR_EVALUATE_VALIDATION_END", DEFAULT_VALIDATION_END
            ),
            horizon_days=integer("FACTOR_EVALUATE_HORIZON_DAYS", 5),
            execution_lag_days=integer("FACTOR_EVALUATE_EXECUTION_LAG_DAYS", 1),
            price_col=value("FACTOR_EVALUATE_PRICE_COL", "close_post"),
            quantiles=integer("FACTOR_EVALUATE_QUANTILES", 10),
            top_percentile=number("FACTOR_EVALUATE_TOP_PERCENTILE", 1.0),
            rolling_mean_days=integer(
                "FACTOR_EVALUATE_ROLLING_MEAN_DAYS", DEFAULT_ROLLING_MEAN_DAYS
            ),
            neutralization_industry_level=value(
                "FACTOR_EVALUATE_NEUTRALIZATION_INDUSTRY_LEVEL",
                DEFAULT_NEUTRALIZATION_INDUSTRY_LEVEL,
            ),
            threads=integer("FACTOR_EVALUATE_THREADS", 2),
            memory_limit=value("FACTOR_EVALUATE_MEMORY_LIMIT", "4GB"),
            temp_directory=value(
                "FACTOR_EVALUATE_TEMP_DIRECTORY", ".tmp/factor_evaluation"
            ),
            workers=integer("FACTOR_EVALUATE_WORKERS", 1),
            intra_column_workers=integer("FACTOR_EVALUATE_INTRA_COLUMN_WORKERS", 1),
            worker_column_group_size=integer(
                "FACTOR_EVALUATE_WORKER_COLUMN_GROUP_SIZE", 1
            ),
            column_batch_size=integer(
                "FACTOR_EVALUATE_COLUMN_BATCH_SIZE", 0, optional=True
            ),
            date_batch_days=integer(
                "FACTOR_EVALUATE_DATE_BATCH_DAYS", 0, optional=True
            ),
            default_column_batch_size=integer(
                "FACTOR_EVALUATE_DEFAULT_COLUMN_BATCH_SIZE", DEFAULT_COLUMN_BATCH_SIZE
            ),
            default_date_batch_days=integer(
                "FACTOR_EVALUATE_DEFAULT_DATE_BATCH_DAYS", DEFAULT_DATE_BATCH_DAYS
            ),
            single_column_date_batch_days=integer(
                "FACTOR_EVALUATE_SINGLE_COLUMN_DATE_BATCH_DAYS",
                SINGLE_COLUMN_DATE_BATCH_DAYS,
            ),
            refresh_column_batch_size=integer(
                "FACTOR_EVALUATE_REFRESH_COLUMN_BATCH_SIZE", REFRESH_COLUMN_BATCH_SIZE
            ),
            refresh_date_batch_days=integer(
                "FACTOR_EVALUATE_REFRESH_DATE_BATCH_DAYS", REFRESH_DATE_BATCH_DAYS
            ),
            reference_refresh_date_batch_days=integer(
                "FACTOR_EVALUATE_REFERENCE_REFRESH_DATE_BATCH_DAYS",
                REFERENCE_REFRESH_DATE_BATCH_DAYS,
            ),
            reference_correlation_batch_size=integer(
                "FACTOR_EVALUATE_REFERENCE_CORRELATION_BATCH_SIZE",
                REFERENCE_CORRELATION_BATCH_SIZE,
            ),
            probe_date_days=integer("FACTOR_EVALUATE_PROBE_DATE_DAYS", PROBE_DATE_DAYS),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        positive = {
            "horizon_days": self.horizon_days,
            "quantiles": self.quantiles,
            "rolling_mean_days": self.rolling_mean_days,
            "threads": self.threads,
            "workers": self.workers,
            "intra_column_workers": self.intra_column_workers,
            "worker_column_group_size": self.worker_column_group_size,
            "default_column_batch_size": self.default_column_batch_size,
            "default_date_batch_days": self.default_date_batch_days,
            "single_column_date_batch_days": self.single_column_date_batch_days,
            "refresh_column_batch_size": self.refresh_column_batch_size,
            "refresh_date_batch_days": self.refresh_date_batch_days,
            "reference_refresh_date_batch_days": self.reference_refresh_date_batch_days,
            "reference_correlation_batch_size": self.reference_correlation_batch_size,
            "probe_date_days": self.probe_date_days,
        }
        if self.execution_lag_days < 0:
            raise ValueError("FACTOR_EVALUATE_EXECUTION_LAG_DAYS must be non-negative")
        if any(int(value) <= 0 for value in positive.values()):
            raise ValueError("factor evaluation integer settings must be positive")
        if self.column_batch_size is not None and self.column_batch_size <= 0:
            raise ValueError("FACTOR_EVALUATE_COLUMN_BATCH_SIZE must be positive")
        if self.date_batch_days is not None and self.date_batch_days <= 0:
            raise ValueError("FACTOR_EVALUATE_DATE_BATCH_DAYS must be positive")
        if not math.isfinite(self.top_percentile) or not 0 < self.top_percentile <= 50:
            raise ValueError(
                "FACTOR_EVALUATE_TOP_PERCENTILE must be in the interval (0, 50]"
            )
        if self.neutralization_industry_level not in {
            "first_industry_code",
            "second_industry_code",
            "third_industry_code",
        }:
            raise ValueError("invalid FACTOR_EVALUATE_NEUTRALIZATION_INDUSTRY_LEVEL")


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if entry.startswith("export "):
            entry = entry[7:].lstrip()
        key, separator, raw_value = entry.partition("=")
        if not separator:
            continue
        raw_value = raw_value.strip()
        if raw_value and raw_value[0] in "\"'":
            quote = raw_value[0]
            end = raw_value.find(quote, 1)
            if end >= 0:
                raw_value = raw_value[1:end]
        else:
            raw_value = raw_value.split(" #", maxsplit=1)[0].strip()
        values[key.strip()] = raw_value
    return values


IC_COLUMNS = ("date", "variant", "ic", "observations")
CORRELATION_COLUMNS = (
    "date",
    "variant",
    "reference_factor",
    "correlation",
    "observations",
)
RETURNS_COLUMNS = (
    "date",
    "variant",
    "portfolio_id",
    "layer",
    "gross_return",
    "net_return_5bps",
    "gross_return_nav",
    "net_return_5bps_nav",
    "turnover",
    "holdings",
    "observations",
    "top_gross_return",
    "top_net_return_5bps",
    "top_gross_return_nav",
    "top_net_return_5bps_nav",
    "top_turnover",
    "top_holdings",
    "top_observations",
)
EVALUATION_COLUMNS = {
    "ic": IC_COLUMNS,
    "returns": RETURNS_COLUMNS,
    "correlation": CORRELATION_COLUMNS,
}
_RETURNS_SQL_TYPES = {
    "date": "DATE",
    "variant": "VARCHAR",
    "portfolio_id": "INTEGER",
    "layer": "INTEGER",
    "holdings": "BIGINT",
    "observations": "BIGINT",
    "top_holdings": "BIGINT",
    "top_observations": "BIGINT",
}
_EMPTY_METRIC_TYPES = {
    "ic": (
        ("date", "DATE"),
        ("variant", "VARCHAR"),
        ("ic", "DOUBLE"),
        ("observations", "BIGINT"),
    ),
    "returns": tuple(
        (column, _RETURNS_SQL_TYPES.get(column, "DOUBLE")) for column in RETURNS_COLUMNS
    ),
    "correlation": (
        ("date", "DATE"),
        ("variant", "VARCHAR"),
        ("reference_factor", "VARCHAR"),
        ("correlation", "DOUBLE"),
        ("observations", "BIGINT"),
    ),
}
_STAGE_TABLE_DEFINITIONS = {
    "_evaluation_ic": (
        "factor_column VARCHAR",
        "date DATE",
        "variant VARCHAR",
        "ic DOUBLE",
        "observations BIGINT",
    ),
    "_evaluation_returns": (
        "factor_column VARCHAR",
        "date DATE",
        "variant VARCHAR",
        "portfolio_id INTEGER",
        "layer INTEGER",
        "gross_return DOUBLE",
        "net_return_5bps DOUBLE",
        "turnover DOUBLE",
        "holdings BIGINT",
        "observations BIGINT",
        "top_high_gross_return DOUBLE",
        "top_high_net_return_5bps DOUBLE",
        "top_high_turnover DOUBLE",
        "top_high_holdings BIGINT",
        "top_high_observations BIGINT",
        "top_low_gross_return DOUBLE",
        "top_low_net_return_5bps DOUBLE",
        "top_low_turnover DOUBLE",
        "top_low_holdings BIGINT",
        "top_low_observations BIGINT",
    ),
    "_evaluation_correlation": (
        "factor_column VARCHAR",
        "date DATE",
        "variant VARCHAR",
        "reference_factor VARCHAR",
        "correlation DOUBLE",
        "observations BIGINT",
    ),
    "_layer_nav_seed": (
        "factor_column VARCHAR",
        "variant VARCHAR",
        "portfolio_id INTEGER",
        "layer INTEGER",
        "gross_seed DOUBLE",
        "net_seed DOUBLE",
    ),
    "_top_nav_seed": (
        "factor_column VARCHAR",
        "variant VARCHAR",
        "portfolio_id INTEGER",
        "gross_seed DOUBLE",
        "net_seed DOUBLE",
    ),
    "_evaluation_selection": (
        "factor_column VARCHAR",
        "variant VARCHAR",
        "direction_sign INTEGER",
        "train_mean_ic DOUBLE",
        "train_ic_days BIGINT",
        "validation_mean_ic DOUBLE",
        "validation_stddev_ic DOUBLE",
        "validation_ic_ir DOUBLE",
        "validation_ic_days BIGINT",
        "direction_consistent BOOLEAN",
        "selected BOOLEAN",
    ),
}


@dataclass(frozen=True)
class EvaluationConfig:
    factor_name: str | None = None
    storage_factor_name: str | None = None
    factor_data: str | Path | None = None
    factor_module: str | None = None
    begin: str | None = DEFAULT_EVALUATION_BEGIN
    train_end: str = DEFAULT_TRAIN_END
    validation_end: str = DEFAULT_VALIDATION_END
    end: str | None = None
    horizon_days: int = 5
    execution_lag_days: int = 1
    price_col: str = "close_post"
    quantiles: int = 10
    top_percentile: float = 1.0
    rolling_mean_days: int = DEFAULT_ROLLING_MEAN_DAYS
    neutralization_industry_level: str = DEFAULT_NEUTRALIZATION_INDUSTRY_LEVEL
    reference_factors: tuple[str, ...] | None = None
    factor_columns: tuple[str, ...] | None = None
    data_root: str | Path | None = None
    output_dir: str | Path = "artifacts/factor_zoo"
    factor_zoo_root: str | Path | None = None
    threads: int = 2
    memory_limit: str | None = "4GB"
    temp_directory: str | Path = ".tmp/factor_evaluation"
    column_batch_size: int | None = None
    date_batch_days: int | None = None
    intra_column_workers: int = 1
    tasks: tuple[str, ...] = ("ic", "portfolio", "correlation")
    rebuild: bool = False
    project_root: str | Path | None = None
    settings: EvaluationSettings | None = None

    def validate(self) -> None:
        if not self.tasks or set(self.tasks) - {"ic", "portfolio", "correlation"}:
            raise ValueError("tasks must select ic, portfolio or correlation")
        if self.factor_data is not None:
            if self.factor_name is not None or self.storage_factor_name is not None:
                raise ValueError("factor_data cannot be combined with a package or storage name")
        elif self.factor_name is not None:
            validate_identifier(self.factor_name, "factor_name")
        else:
            raise ValueError("factor_data or factor_name is required")
        if self.storage_factor_name is not None:
            validate_identifier(self.storage_factor_name, "storage_factor_name")
        if self.horizon_days <= 0:
            raise ValueError("horizon_days must be positive")
        if self.execution_lag_days < 0:
            raise ValueError("execution_lag_days must be non-negative")
        if self.quantiles < 2:
            raise ValueError("quantiles must be at least 2")
        if self.rolling_mean_days <= 0:
            raise ValueError("rolling_mean_days must be positive")
        if self.neutralization_industry_level not in {
            "first_industry_code",
            "second_industry_code",
            "third_industry_code",
        }:
            raise ValueError(
                "neutralization_industry_level must be one of first_industry_code, "
                "second_industry_code, third_industry_code"
            )
        if not math.isfinite(float(self.top_percentile)):
            raise ValueError("top_percentile must be finite")
        if not 0.0 < float(self.top_percentile) <= 50.0:
            raise ValueError("top_percentile must be in the interval (0, 50]")
        if self.threads <= 0:
            raise ValueError("threads must be positive")
        if self.column_batch_size is not None and self.column_batch_size <= 0:
            raise ValueError("column_batch_size must be positive")
        if self.date_batch_days is not None and self.date_batch_days <= 0:
            raise ValueError("date_batch_days must be positive")
        if self.intra_column_workers <= 0:
            raise ValueError("intra_column_workers must be positive")
        boundaries = {
            "begin": self.begin,
            "train_end": self.train_end,
            "validation_end": self.validation_end,
            "end": self.end,
        }
        parsed: dict[str, date] = {}
        for label, value in boundaries.items():
            if value is None:
                continue
            try:
                parsed[label] = date.fromisoformat(str(value))
            except ValueError as exc:
                raise ValueError(f"{label} must use YYYY-MM-DD format") from exc
        if not parsed["train_end"] < parsed["validation_end"]:
            raise ValueError("train_end must be earlier than validation_end")
        if "begin" in parsed and not parsed["begin"] <= parsed["train_end"]:
            raise ValueError("begin must not be later than train_end")
        if "end" in parsed and not parsed["validation_end"] < parsed["end"]:
            raise ValueError("validation_end must be earlier than end")


@dataclass(frozen=True)
class _ColumnState:
    factor_column: str
    output_dir: Path
    config: dict[str, Any]
    cutoff: str
    incremental: bool
    reference_refresh: bool = False


def _chunks(values: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _is_oom(exc: BaseException) -> bool:
    return (
        isinstance(exc, duckdb.OutOfMemoryException)
        or "out of memory" in str(exc).lower()
    )


def _parse_memory_bytes(value: str | None) -> int | None:
    if value is None:
        return None
    normalized = str(value).strip().upper().replace("IB", "B")
    units = {
        "B": 1,
        "KB": 1_000,
        "MB": 1_000_000,
        "GB": 1_000_000_000,
        "TB": 1_000_000_000_000,
    }
    for unit in ("TB", "GB", "MB", "KB", "B"):
        if normalized.endswith(unit):
            try:
                return int(float(normalized[: -len(unit)].strip()) * units[unit])
            except ValueError:
                return None
    return None


def _partition_dates(root: Path) -> list[str]:
    dates = [
        path.name[5:]
        for path in root.glob("date=*")
        if path.is_dir() and len(path.name) == 15
    ]
    return sorted(set(dates))


def _sql_list(storage: FactorStorage, values: Sequence[str]) -> str:
    return ", ".join(storage.sql_literal(value) for value in values)


def _empty_metric_query(metric: str) -> str:
    columns = _EMPTY_METRIC_TYPES[metric]
    selected = ", ".join(
        f"CAST(NULL AS {sql_type}) AS {column}" for column, sql_type in columns
    )
    return f"SELECT {selected} WHERE FALSE"


def print_progress(label: str, completed: int, total: int, started: float, *, failed: int = 0) -> None:
    """Report measured phase progress; failures remain separate from successes."""
    processed = completed + failed
    elapsed = monotonic() - started
    remaining = f"{elapsed / processed * (total - processed):.1f}s" if processed else "pending"
    percent = processed / total if total else 1.0
    print(f"{label}: {percent:.1%} | completed={completed}/{total} | failed={failed} | "
          f"elapsed={elapsed:.1f}s | ETA={remaining}", flush=True)
