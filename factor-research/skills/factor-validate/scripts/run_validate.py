"""Run independent factor validation and write standardized README results."""

from __future__ import annotations

import argparse
import inspect
import math
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import pandas as pd
from factor_protocol import (
    resolve_artifacts_root,
    resolve_data_root,
    resolve_factor_data_root,
    resolve_factor_zoo_root,
)
from factor_protocol.artifacts import (
    copy_if_present,
    dataset_manifest,
    factor_column_output,
    manifest_reference,
    manifests_match,
    read_config,
    replace_column_directory,
    stage_column_directory,
    write_config,
)
from factor_protocol.fingerprint import factor_definition_fingerprint
from factor_protocol.identity import require_identity
from factor_protocol.loader import load_module_from_path, resolve_factor_package
from factor_protocol.protocol import FactorValidationDefinition, resolve_factor_columns
from factor_protocol.readme import set_acceptance
from factor_protocol.storage import FactorStorage


DEFAULT_VALIDATION_LOOKBACK_DAYS = 800
DEFAULT_VALIDATION_SAMPLE_CODES = 100
DEFAULT_VALIDATION_SAMPLE_DATES = 5
VALIDATION_SOURCE_BATCH_DAYS = 20
DEFAULT_VALIDATION_TOLERANCE = 1e-10
DEFAULT_VALIDATION_RELATIVE_TOLERANCE = 1e-8
DEFAULT_VALIDATION_THREADS = 2


RESULTS_START_MARKER = "<!-- factor-validate:results:start -->"
RESULTS_END_MARKER = "<!-- factor-validate:results:end -->"
VALIDATION_HEADING = "## 6. 因子验证"


def _markdown_cell(value: Any) -> str:
    if value is None or value == "":
        return "—"
    return (
        str(value)
        .replace("|", "\\|")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _markdown_number(value: Any) -> str:
    if value is None or not np.isfinite(float(value)):
        return "—"
    return f"{float(value):.6g}"


def render_validation_results(report: dict[str, Any]) -> str:
    lines = []
    headers = (
        "测试日期",
        "样本日期区间 / 数据覆盖截止日",
        "因子列",
        "交易日 / 代码数 / 审计行",
        "覆盖率",
        "空值不一致数",
        "超差数",
        "重复键",
        "最大绝对误差",
        "状态",
        "备注",
    )
    lines.extend(
        (
            "| " + " | ".join(headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
        )
    )
    for column in report["columns"]:
        validation = column["validation"]
        metrics = validation["metrics"]
        notes = validation.get("error") or metrics.get("failure_reasons")
        watermark = column["watermark"] or "—"
        interval = (
            f"{validation['sample_begin']}..{validation['sample_end']} / {watermark}"
        )
        counts = (
            f"{metrics['audit_date_count']} / {metrics['audit_code_count']}"
            f" / {metrics['audit_rows']}"
        )
        values = (
            report["test_date"],
            interval,
            f"`{column['factor_column']}`",
            counts,
            _markdown_number(metrics["coverage"]),
            metrics["null_mismatches"],
            metrics["tolerance_violations"],
            metrics["duplicate_keys"],
            _markdown_number(metrics["max_abs_diff"]),
            validation["status"],
            notes,
        )
        lines.append(
            "| " + " | ".join(_markdown_cell(value) for value in values) + " |"
        )
    return "\n".join(lines)


def _section_6_bounds(lines: list[str]) -> tuple[int, int]:
    starts = [i for i, line in enumerate(lines) if line.strip() == VALIDATION_HEADING]
    if len(starts) != 1:
        raise ValueError(
            "factor README must contain exactly one section 6 for validation"
        )
    start = starts[0]
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    return start, end


def _results_block_bounds(section: list[str]) -> tuple[int, int]:
    starts = [
        i for i, line in enumerate(section) if line.strip() == RESULTS_START_MARKER
    ]
    ends = [i for i, line in enumerate(section) if line.strip() == RESULTS_END_MARKER]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise ValueError("factor README validation markers are incomplete or ambiguous")
    return starts[0], ends[0] + 1


def validate_readme_target(readme_path: Path) -> None:
    lines = readme_path.read_text(encoding="utf-8").splitlines()
    start, end = _section_6_bounds(lines)
    _results_block_bounds(lines[start:end])


def update_factor_readme(readme_path: Path, report: dict[str, Any]) -> None:
    content = readme_path.read_bytes().decode("utf-8")
    newline = "\r\n" if "\r\n" in content else "\n"
    lines = content.splitlines(keepends=True)
    start, end = _section_6_bounds(lines)
    section = lines[start:end]
    bounds = _results_block_bounds(section)
    block_lines = [
        RESULTS_START_MARKER,
        *render_validation_results(report).splitlines(),
        RESULTS_END_MARKER,
    ]
    block = newline.join(block_lines) + newline
    section[bounds[0] : bounds[1]] = [block]
    lines[start:end] = section
    updated = "".join(lines)
    complete = (len(report["columns"]) == report["declared_column_count"]
                and report["declared_column_count"] > 0
                and len({column["factor_column"] for column in report["columns"]})
                    == report["declared_column_count"])
    passed = complete and report["status"] == "pass" and all(
        column["validation"]["status"] == "pass" for column in report["columns"])
    updated = set_acceptance(updated, 4, passed)
    _write_atomic(readme_path, updated)


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_project_env(project_root: Path) -> dict[str, str]:
    env_path = project_root / ".env"
    if not env_path.is_file():
        return {}

    values: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if entry.startswith("export "):
            entry = entry[7:].lstrip()
        name, separator, value = entry.partition("=")
        if not separator:
            continue
        value = value.strip()
        if value and value[0] in "\"'":
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end >= 0 else value[1:]
        else:
            value = value.split(" #", maxsplit=1)[0].strip()
        values[name.strip()] = value
    return values


def _validation_settings(project_root: Path) -> dict[str, Any]:
    dotenv = _read_project_env(project_root)

    def configured(name: str, default: str) -> str:
        return (os.environ.get(name, "").strip()
                or dotenv.get(name, "").strip() or default)

    memory_limit = configured("FACTOR_VALIDATE_MEMORY_LIMIT", "")
    temp_directory = configured("FACTOR_VALIDATE_TEMP_DIRECTORY", "")
    return {
        "lookback_days": int(
            configured(
                "FACTOR_VALIDATE_LOOKBACK_DAYS",
                str(DEFAULT_VALIDATION_LOOKBACK_DAYS),
            )
        ),
        "max_sample_codes": int(
            configured(
                "FACTOR_VALIDATE_MAX_SAMPLE_CODES",
                str(DEFAULT_VALIDATION_SAMPLE_CODES),
            )
        ),
        "max_sample_dates": int(
            configured(
                "FACTOR_VALIDATE_MAX_SAMPLE_DATES",
                str(DEFAULT_VALIDATION_SAMPLE_DATES),
            )
        ),
        "tolerance": float(
            configured(
                "FACTOR_VALIDATE_TOLERANCE",
                str(DEFAULT_VALIDATION_TOLERANCE),
            )
        ),
        "relative_tolerance": float(
            configured(
                "FACTOR_VALIDATE_RELATIVE_TOLERANCE",
                str(DEFAULT_VALIDATION_RELATIVE_TOLERANCE),
            )
        ),
        "threads": max(
            int(
                configured(
                    "FACTOR_VALIDATE_THREADS",
                    str(DEFAULT_VALIDATION_THREADS),
                )
            ),
            1,
        ),
        "memory_limit": memory_limit or None,
        "temp_directory": temp_directory or None,
    }


def _resolve_project_path(path: str | Path, project_root: Path) -> Path:
    resolved = Path(path).expanduser()
    return resolved if resolved.is_absolute() else project_root / resolved


def validate_tolerances(tolerance: float, relative_tolerance: float) -> None:
    """Require finite, nonnegative error bounds before running a validator."""
    for name, value in (
        ("tolerance", tolerance),
        ("relative_tolerance", relative_tolerance),
    ):
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be finite and nonnegative")


@dataclass(frozen=True)
class ValidationConfig:
    """Configuration shared by the native factor validators."""

    factor_name: str
    begin: str
    end: str
    lookback_days: int = DEFAULT_VALIDATION_LOOKBACK_DAYS
    codes: tuple[str, ...] | None = None
    tolerance: float = DEFAULT_VALIDATION_TOLERANCE
    relative_tolerance: float = DEFAULT_VALIDATION_RELATIVE_TOLERANCE
    data_root: str | Path | None = None
    output_dir: str | Path | None = None
    factor_zoo_root: str | Path | None = None
    threads: int = DEFAULT_VALIDATION_THREADS
    factor_column: str | None = None
    storage_factor_name: str | None = None
    source_table: str = "quotes_day"
    max_sample_codes: int = DEFAULT_VALIDATION_SAMPLE_CODES
    max_sample_dates: int = DEFAULT_VALIDATION_SAMPLE_DATES
    memory_limit: str | None = None
    temp_directory: str | Path | None = None
    project_root: str | Path | None = None

    def __post_init__(self) -> None:
        validate_tolerances(self.tolerance, self.relative_tolerance)


def _supported_kwargs(
    func: Callable[..., Any], kwargs: dict[str, Any]
) -> dict[str, Any]:
    signature = inspect.signature(func)
    if any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    ):
        return {key: value for key, value in kwargs.items() if value is not None}
    return {
        key: value
        for key, value in kwargs.items()
        if value is not None and key in signature.parameters
    }


def call_validator(func: Callable[..., Any], kwargs: dict[str, Any]) -> Any:
    """Call a factor validator with its declared parameters."""

    filtered = _supported_kwargs(func, kwargs)
    signature = inspect.signature(func)
    missing = [
        name
        for name, parameter in signature.parameters.items()
        if parameter.default is inspect.Parameter.empty
        and parameter.kind
        not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        and name not in filtered
    ]
    if missing:
        raise TypeError(
            f"{func.__name__} has unsupported required parameters: {', '.join(missing)}"
        )
    return func(**filtered)


def _expected_column(
    frame: pd.DataFrame,
    factor_name: str,
    factor_column: str,
    *,
    allow_generic: bool = False,
) -> str:
    inferred = [
        column for column in frame.columns if str(column).startswith("expected_")
    ]
    candidates = [
        f"expected_{factor_column}",
    ]
    if allow_generic or len(inferred) == 1:
        candidates.extend((f"expected_{factor_name}", "expected"))
    for candidate in candidates:
        if candidate in frame.columns:
            return candidate
    if len(inferred) == 1:
        return str(inferred[0])
    raise ValueError(
        "validation audit must provide an expected column mapped to "
        f"{factor_column}; generic expected columns are only allowed for a single-column audit"
    )


def _read_source(
    storage: FactorStorage,
    input_columns: Sequence[str],
    cfg: ValidationConfig,
) -> pd.DataFrame:
    source_begin = (
        (pd.Timestamp(cfg.begin) - pd.Timedelta(days=max(int(cfg.lookback_days), 0)))
        .date()
        .isoformat()
    )
    source_columns = tuple(dict.fromkeys(("code", "date", *input_columns)))
    selected = ", ".join(storage.identifier(column) for column in source_columns)
    code_filter = ""
    if cfg.codes:
        values = ", ".join(storage.sql_literal(code) for code in cfg.codes)
        code_filter = f"AND CAST(code AS VARCHAR) IN ({values})"
    source_table = storage.identifier(cfg.source_table)
    root = storage.table_root(source_table)
    dates = _partition_dates(root, source_begin, cfg.end)
    frames: list[pd.DataFrame] = []
    for offset in range(0, len(dates), VALIDATION_SOURCE_BATCH_DAYS):
        batch_dates = dates[offset : offset + VALIDATION_SOURCE_BATCH_DAYS]
        source = _partition_scan_source(root, batch_dates)
        if source is None:
            continue
        batch_begin, batch_end = batch_dates[0], batch_dates[-1]
        frame = storage.df(f"""
            SELECT {selected}
            FROM {source}
            WHERE CAST(date AS DATE) >= CAST({storage.sql_literal(batch_begin)} AS DATE)
              AND CAST(date AS DATE) <= CAST({storage.sql_literal(batch_end)} AS DATE)
              {code_filter}
            ORDER BY date, code
            """)
        if not frame.empty:
            frames.append(frame)
    if frames:
        return pd.concat(frames, ignore_index=True)
    if root.exists() and any(root.glob("date=*")):
        return pd.DataFrame(columns=list(source_columns))
    # Preserve the normal empty/missing-data diagnostic when a synthetic test
    # storage or an unpartitioned fixture does not expose date directories.
    storage.register_data_view(source_table)
    filters = [
        f"CAST(date AS DATE) >= CAST({storage.sql_literal(source_begin)} AS DATE)",
        f"CAST(date AS DATE) <= CAST({storage.sql_literal(cfg.end)} AS DATE)",
    ]
    if cfg.codes:
        values = ", ".join(storage.sql_literal(code) for code in cfg.codes)
        filters.append(f"CAST(code AS VARCHAR) IN ({values})")
    return storage.df(
        f"SELECT {selected} FROM {source_table} WHERE {' AND '.join(filters)} ORDER BY date, code"
    )


def _partition_scan_source(root: Path, dates: Sequence[str] | None) -> str | None:
    """Return a DuckDB scan over only the requested Hive date partitions."""

    if not dates:
        return None
    paths: list[str] = []
    for date in dates:
        partition = root / f"date={date!s}"
        if not partition.is_dir():
            continue
        paths.extend(
            file.as_posix()
            for file in sorted(partition.rglob("*.parquet"), key=lambda p: p.as_posix())
            if file.is_file()
        )
    if not paths:
        return None
    literals = ", ".join("'" + path.replace("'", "''") + "'" for path in paths)
    return f"parquet_scan([{literals}], HIVE_PARTITIONING=1, union_by_name=1)"


def _partition_dates(root: Path, begin: str, end: str) -> list[str]:
    begin_date = pd.Timestamp(begin).date().isoformat()
    end_date = pd.Timestamp(end).date().isoformat()
    return sorted(
        {
            path.name.removeprefix("date=")
            for path in root.glob("date=*")
            if path.is_dir()
            and begin_date <= path.name.removeprefix("date=") <= end_date
        }
    )


def _validation_factor_query(
    storage: FactorStorage,
    columns: Sequence[str],
    *,
    begin: str,
    end: str,
    codes: Sequence[str] | None,
    dates: Sequence[str] | None,
) -> str:
    """Build a factor read that cannot fall back to the full history scan."""

    source = _partition_scan_source(storage.factor_root, dates)
    if source is None:
        if storage.factor_root.exists() and any(storage.factor_root.glob("date=*")):
            empty_columns = [
                "CAST(NULL AS VARCHAR) AS code",
                "CAST(NULL AS DATE) AS date",
                *[
                    f"CAST(NULL AS DOUBLE) AS {storage.identifier(column)}"
                    for column in columns
                ],
            ]
            return "SELECT " + ", ".join(empty_columns) + " WHERE FALSE"
        filters = [
            f"CAST(date AS DATE) >= CAST({storage.sql_literal(begin)} AS DATE)",
            f"CAST(date AS DATE) <= CAST({storage.sql_literal(end)} AS DATE)",
        ]
        if codes:
            values = ", ".join(storage.sql_literal(str(code)) for code in codes)
            filters.append(f"CAST(code AS VARCHAR) IN ({values})")
        return storage.select_sql(
            storage.factor_name,
            columns=("code", "date", *columns),
            where=" AND ".join(filters),
            order_by=("date", "code"),
        )
    selected = ["CAST(code AS VARCHAR) AS code", "CAST(date AS DATE) AS date"]
    available = {
        row[0] for row in storage.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()
    }
    for column in columns:
        identifier = storage.identifier(column)
        if column in available:
            selected.append(f"CAST({identifier} AS DOUBLE) AS {identifier}")
        else:
            # Factor schemas can legitimately evolve across materialized
            # partitions.  Keep a typed NULL column for a requested column
            # that is absent from the bounded target partitions so the
            # canonical audit reports coverage/stored-column diagnostics
            # instead of aborting the whole source with a DuckDB binder error.
            selected.append(f"CAST(NULL AS DOUBLE) AS {identifier}")
    filters = [
        f"CAST(date AS DATE) >= CAST({storage.sql_literal(begin)} AS DATE)",
        f"CAST(date AS DATE) <= CAST({storage.sql_literal(end)} AS DATE)",
    ]
    if codes:
        values = ", ".join(storage.sql_literal(str(code)) for code in codes)
        filters.append(f"CAST(code AS VARCHAR) IN ({values})")
    return f"""
        SELECT {", ".join(selected)}
        FROM {source}
        WHERE {" AND ".join(filters)}
        ORDER BY date, code
    """


def _select_validation_codes(
    storage: FactorStorage,
    *,
    begin: str,
    end: str,
    limit: int,
    dates: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """Select a deterministic, bounded validation sample without pandas.

    Validation is a reproducibility audit, not a full-history recalculation.
    Independent validators receive bounded frames and must never
    be handed every security in a production-sized sample.  Prefer codes that
    are actually materialized for the requested dates, then fall back to the
    quote universe so an uninitialized factor still fails with a useful audit.
    """

    if limit <= 0:
        return ()
    begin_sql = storage.sql_literal(begin)
    end_sql = storage.sql_literal(end)
    query = f"""
        SELECT CAST(code AS VARCHAR) AS code
        FROM {{source}}
        WHERE CAST(date AS DATE) >= CAST({begin_sql} AS DATE)
          AND CAST(date AS DATE) <= CAST({end_sql} AS DATE)
          AND code IS NOT NULL
          {{date_filter}}
        GROUP BY CAST(code AS VARCHAR)
        ORDER BY code
        LIMIT {int(limit)}
    """
    roots = (storage.factor_root, storage.data_root / "quotes_day")
    for root in roots:
        candidate_dates = (
            tuple(dates) if dates else tuple(_partition_dates(root, begin, end))
        )
        source = _partition_scan_source(root, candidate_dates)
        if source is None:
            continue
        date_filter = ""
        if candidate_dates:
            values = ", ".join(
                storage.sql_literal(str(value)) for value in candidate_dates
            )
            date_filter = f"AND CAST(date AS DATE) IN ({values})"
        try:
            rows = storage.execute(
                query.format(source=source, date_filter=date_filter)
            ).fetchall()
            if rows:
                return tuple(str(row[0]) for row in rows)
        except Exception:  # noqa: BLE001, S112 - fall back to the next sample source.
            continue
    return ()


def _bounded_validation_codes(
    codes: Sequence[str] | None,
    *,
    limit: int,
) -> tuple[str, ...]:
    """Normalize an explicit validation sample without allowing an unbounded one.

    The public ``--codes`` option is useful for reproducing a particular audit,
    but it must not turn the independent pandas recomputation into a full
    universe load.  Preserve the caller's order, remove duplicates, and apply
    the same cap used by automatic code selection.
    """

    if not codes or limit <= 0:
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for value in codes:
        code = str(value)
        if code in seen:
            continue
        seen.add(code)
        result.append(code)
        if len(result) >= int(limit):
            break
    return tuple(result)


def _select_validation_dates(
    storage: FactorStorage,
    *,
    begin: str,
    end: str,
    limit: int,
) -> tuple[str, ...]:
    """Select the latest available partition dates within the requested interval."""

    if limit <= 0:
        return ()
    for root in (storage.factor_root, storage.data_root / "quotes_day"):
        dates = list(reversed(_partition_dates(root, begin, end)))[: int(limit)]
        if dates:
            return tuple(sorted(dates))
    return ()


def _summary(
    audit: pd.DataFrame,
    *,
    stored_col: str,
    expected_col: str,
    tolerance: float,
    relative_tolerance: float = DEFAULT_VALIDATION_RELATIVE_TOLERANCE,
) -> pd.DataFrame:
    validate_tolerances(tolerance, relative_tolerance)
    expected_values = pd.to_numeric(
        audit.get(expected_col, pd.Series(dtype=float)), errors="coerce"
    )
    stored_values = pd.to_numeric(
        audit.get(stored_col, pd.Series(dtype=float)), errors="coerce"
    )
    both_null = expected_values.isna() & stored_values.isna()
    matched = (
        (stored_values.notna() & expected_values.notna()) | both_null
        if stored_col in audit and expected_col in audit
        else pd.Series(False, index=audit.index)
    )
    if "sample_key" in audit:
        matched &= audit["sample_key"].eq("both")
    diffs = (
        audit.loc[matched, "abs_diff"]
        if "abs_diff" in audit
        else pd.Series(dtype=float)
    )
    expected_matched = expected_values.loc[matched]
    stored_matched = stored_values.loc[matched]
    scales = pd.concat([expected_matched.abs(), stored_matched.abs()], axis=1).max(
        axis=1
    )
    allowed_diffs = np.maximum(
        float(tolerance),
        float(relative_tolerance) * scales,
    )
    finite_diff_mask = diffs.notna() & allowed_diffs.notna()
    tolerance_violations = int(((diffs > allowed_diffs) & finite_diff_mask).sum())
    duplicate_keys = (
        int(audit.duplicated(["date", "code"]).sum())
        if {"date", "code"}.issubset(audit.columns)
        else len(audit)
    )
    finite_expected_values = expected_values.dropna()
    finite_stored_values = stored_values.dropna()
    expected_non_null = int(finite_expected_values.size)
    stored_non_null = int(finite_stored_values.size)
    finite_expected = (
        bool(np.isfinite(finite_expected_values).all()) if expected_non_null else True
    )
    finite_stored = (
        bool(np.isfinite(finite_stored_values).all()) if stored_non_null else True
    )
    max_abs_diff = float(diffs.max()) if not diffs.empty else np.nan
    coverage = float(matched.sum() / len(audit)) if len(audit) else 0.0
    null_mismatches = int((expected_values.isna() ^ stored_values.isna()).sum())
    status = "pass"
    reasons: list[str] = []
    if audit.empty:
        status = "fail"
        reasons.append("empty_audit")
    if duplicate_keys:
        status = "fail"
        reasons.append("duplicate_keys")
    if not finite_expected or not finite_stored:
        status = "fail"
        reasons.append("non_finite")
    if expected_non_null == 0:
        # Matching NULLs alone is not evidence of an independent
        # recomputation.  Surface an insufficient warm-up/sample instead of
        # silently passing an all-null audit.
        status = "fail"
        reasons.append("sample_insufficient")
    if coverage < 1.0:
        status = "fail"
        reasons.append("coverage")
    if tolerance_violations:
        status = "fail"
        reasons.append("tolerance")
    return pd.DataFrame(
        [
            {
                "audit_rows": len(audit),
                "audit_date_count": int(audit["date"].nunique())
                if "date" in audit
                else 0,
                "audit_code_count": int(audit["code"].nunique())
                if "code" in audit
                else 0,
                "matched_rows": int(matched.sum()),
                "null_mismatches": null_mismatches,
                "expected_non_null": expected_non_null,
                "stored_non_null": stored_non_null,
                "coverage": coverage,
                "duplicate_keys": duplicate_keys,
                "max_abs_diff": max_abs_diff,
                "mean_abs_diff": float(diffs.mean()) if not diffs.empty else np.nan,
                "max_allowed_diff": (
                    float(allowed_diffs[finite_diff_mask].max())
                    if bool(finite_diff_mask.any())
                    else np.nan
                ),
                "tolerance_violations": tolerance_violations,
                "relative_tolerance": float(relative_tolerance),
                "status": status,
                "failure_reasons": ",".join(reasons),
            }
        ]
    )


def _compute_standard_audit(
    *,
    storage: FactorStorage,
    module: Any,
    cfg: ValidationConfig,
    compute_validation_audit: Callable[[pd.DataFrame, Any], pd.DataFrame],
    compute_validation_audits: (
        Callable[[pd.DataFrame, Any], pd.DataFrame] | None
    ) = None,
    validation_input_columns: Sequence[str],
    factor_columns: Sequence[str] | None = None,
) -> Path:
    """Write bounded independent expected values for the canonical audit."""

    package = resolve_factor_package(
        cfg.factor_name,
        project_root=Path(cfg.project_root or Path.cwd()),
        output_dir=cfg.output_dir,
        factor_zoo_root=cfg.factor_zoo_root,
    )
    package.dist_dir.mkdir(parents=True, exist_ok=True)
    available_columns = tuple(factor_columns or resolve_factor_columns(module))
    columns = (
        (cfg.factor_column,) if cfg.factor_column is not None else available_columns
    )
    if not columns:
        raise ValueError("factor package declares no factor columns")
    unknown = sorted(set(columns) - set(available_columns))
    if unknown:
        raise ValueError(f"unknown factor columns: {', '.join(unknown)}")

    bounded_codes = (
        _bounded_validation_codes(cfg.codes, limit=cfg.max_sample_codes)
        if cfg.codes
        else None
    )
    sample_dates = _select_validation_dates(
        storage,
        begin=cfg.begin,
        end=cfg.end,
        limit=cfg.max_sample_dates,
    )
    effective_codes = (
        bounded_codes
        if cfg.codes
        else _select_validation_codes(
            storage,
            begin=cfg.begin,
            end=cfg.end,
            limit=cfg.max_sample_codes,
            dates=sample_dates,
        )
    )
    audit_begin = sample_dates[0] if sample_dates else cfg.begin
    audit_end = sample_dates[-1] if sample_dates else cfg.end
    effective_cfg = replace(
        cfg,
        begin=audit_begin,
        end=audit_end,
        codes=effective_codes,
    )
    source = _read_source(storage, validation_input_columns, effective_cfg)
    begin_date = pd.Timestamp(effective_cfg.begin).date()
    end_date = pd.Timestamp(effective_cfg.end).date()

    audits: list[pd.DataFrame] = []
    # A multi-column factor can expose a single independent recomputation for
    # the shared bounded sample.  This avoids reloading and recomputing the
    # exact same raw inputs once per output column while preserving the normal
    # per-column audit and artifact contract below.
    shared_audit = (
        compute_validation_audits(source, effective_cfg)
        if compute_validation_audits is not None and not source.empty
        else None
    )
    if shared_audit is None:
        shared_audit = pd.DataFrame()
    for factor_column in columns:
        column_cfg = replace(effective_cfg, factor_column=factor_column)
        audit = (
            shared_audit.copy()
            if not shared_audit.empty
            else (
                compute_validation_audit(source, column_cfg)
                if not source.empty
                else pd.DataFrame()
            )
        )
        if audit is None:
            audit = pd.DataFrame()
        audit = audit.copy()
        if audit.empty:
            audits.append(
                pd.DataFrame(columns=["date", "code", f"expected_{factor_column}"])
            )
            continue
        for key in ("date", "code"):
            if key not in audit.columns:
                raise ValueError(f"validation audit must contain {key}")
        audit["date"] = pd.to_datetime(audit["date"], errors="coerce").dt.date
        audit["code"] = audit["code"].astype(str)
        audit = audit[
            audit["date"].notna()
            & (audit["date"] >= begin_date)
            & (audit["date"] <= end_date)
        ].copy()
        expected_col = _expected_column(
            audit,
            cfg.factor_name,
            factor_column,
            allow_generic=len(columns) == 1,
        )
        selected = audit[["date", "code", expected_col]].rename(
            columns={expected_col: f"expected_{factor_column}"}
        )
        audits.append(selected)

    if audits:
        combined = audits[0]
        for audit in audits[1:]:
            combined = combined.merge(audit, on=["date", "code"], how="outer")
    else:
        combined = pd.DataFrame(columns=["date", "code"])

    output_path = package.dist_dir / "audit_sample.csv"
    combined.to_csv(output_path, index=False)
    return output_path


def native_validator(
    compute_validation_audit: Callable[[pd.DataFrame, Any], pd.DataFrame],
    *,
    factor_name: str,
    factor_columns: Sequence[str],
    validation_input_columns: Sequence[str],
    compute_validation_audits: (
        Callable[[pd.DataFrame, Any], pd.DataFrame] | None
    ) = None,
) -> Callable[..., Path]:
    """Build the native validate_factor interface for a factor package."""

    def validate_factor(
        *,
        storage: FactorStorage,
        factor_name: str = factor_name,
        storage_factor_name: str | None = None,
        source_table: str | None = None,
        begin: str,
        end: str,
        output_dir: str | Path | None = None,
        codes: Sequence[str] | None = None,
        lookback_days: int = DEFAULT_VALIDATION_LOOKBACK_DAYS,
        tolerance: float = DEFAULT_VALIDATION_TOLERANCE,
        relative_tolerance: float = DEFAULT_VALIDATION_RELATIVE_TOLERANCE,
        data_root: str | Path | None = None,
        factor_zoo_root: str | Path | None = None,
        threads: int = DEFAULT_VALIDATION_THREADS,
        factor_column: str | None = None,
        max_sample_codes: int = DEFAULT_VALIDATION_SAMPLE_CODES,
        max_sample_dates: int = DEFAULT_VALIDATION_SAMPLE_DATES,
        memory_limit: str | None = None,
        temp_directory: str | Path | None = None,
        project_root: str | Path | None = None,
        **_: Any,
    ) -> Path:
        module = inspect.getmodule(compute_validation_audit)
        cfg = ValidationConfig(
            factor_name=factor_name,
            begin=begin,
            end=end,
            lookback_days=lookback_days,
            codes=tuple(codes) if codes else None,
            tolerance=tolerance,
            relative_tolerance=relative_tolerance,
            data_root=data_root,
            output_dir=output_dir,
            factor_zoo_root=factor_zoo_root,
            threads=threads,
            factor_column=factor_column,
            storage_factor_name=storage_factor_name,
            source_table=source_table
            or getattr(module, "VALIDATION_SOURCE_TABLE", "quotes_day"),
            max_sample_codes=max_sample_codes,
            max_sample_dates=max_sample_dates,
            memory_limit=memory_limit,
            temp_directory=temp_directory,
            project_root=project_root,
        )
        return _compute_standard_audit(
            storage=storage,
            module=module,
            cfg=cfg,
            compute_validation_audit=compute_validation_audit,
            compute_validation_audits=compute_validation_audits,
            validation_input_columns=validation_input_columns,
            factor_columns=factor_columns,
        )

    validate_factor.__name__ = "validate_factor"
    return validate_factor


def _find_artifact(root: Path, names: Sequence[str]) -> Path | None:
    for name in names:
        direct = root / name
        if direct.is_file():
            return direct
    for name in names:
        matches = sorted(root.rglob(name))
        if matches:
            return matches[0]
    return None


def _value_column(
    frame: pd.DataFrame,
    *,
    factor_name: str,
    factor_column: str,
    kind: str,
    allow_generic: bool,
) -> str | None:
    # Native validators historically used both ``expected_<column>`` and
    # ``<column>_expected`` spellings.  Accept the latter while normalizing
    # into the canonical audit schema; otherwise a valid custom audit becomes
    # an all-null generic column and is reported as a false coverage failure.
    inferred = [
        column for column in frame.columns if str(column).startswith(f"{kind}_")
    ]
    candidates = [f"{kind}_{factor_column}", f"{factor_column}_{kind}"]
    if allow_generic or len(inferred) == 1:
        candidates.extend((f"{kind}_{factor_name}", kind))
    if kind == "expected":
        candidates.append(factor_column)
    return next((column for column in candidates if column in frame.columns), None)


def _json_scalar(value: Any) -> Any:
    if pd.isna(value):
        return None
    if isinstance(value, np.generic):
        return value.item()
    return value


def _summary_row_for_column(
    raw_summary: pd.DataFrame,
    factor_column: str,
    *,
    single_column: bool,
) -> dict[str, Any]:
    if raw_summary.empty:
        return {}
    selected = raw_summary
    if "factor_column" in selected.columns:
        matched = selected.loc[selected["factor_column"].astype(str) == factor_column]
        if not matched.empty:
            selected = matched
        elif not single_column:
            return {}
    if selected.empty:
        return {}
    return {
        str(key): _json_scalar(value)
        for key, value in selected.iloc[0].to_dict().items()
    }


def _evaluation_record(value: Any) -> dict[str, Any]:
    """Keep the evaluator's canonical sections while refreshing validation."""
    if not isinstance(value, Mapping):
        return {}
    return {
        key: value[key]
        for key in (
            "status",
            "begin",
            "requested_end",
            "completed_at",
            "stale_reason",
            "parameters",
            "resources",
            "files",
            "source_manifests",
        )
        if key in value
    }


def _write_canonical_validation(
    *,
    storage: FactorStorage,
    factor_name: str,
    factor_columns: Sequence[str],
    fingerprint: str,
    raw_audit_path: Path | None,
    raw_summary_path: Path | None,
    begin: str,
    end: str,
    codes: Sequence[str],
    tolerance: float,
    relative_tolerance: float,
    output_root: Path,
    data_root: Path,
    project_root: Path,
    requested_begin: str | None = None,
    requested_end: str | None = None,
    sample_dates: Sequence[str] | None = None,
    validator_error: str | None = None,
    persist: bool = True,
) -> tuple[list[Path], list[str], list[dict[str, Any]]]:
    report_columns: list[dict[str, Any]] = []
    try:
        raw_audit = (
            pd.read_csv(raw_audit_path)
            if raw_audit_path is not None and raw_audit_path.is_file()
            else pd.DataFrame()
        )
    except (OSError, pd.errors.EmptyDataError):
        raw_audit = pd.DataFrame()
    try:
        raw_summary = (
            pd.read_csv(raw_summary_path)
            if raw_summary_path is not None and raw_summary_path.is_file()
            else pd.DataFrame()
        )
    except (OSError, pd.errors.EmptyDataError):
        raw_summary = pd.DataFrame()

    if "date" not in raw_audit:
        raw_audit["date"] = pd.Series(dtype="object")
    if "code" not in raw_audit:
        raw_audit["code"] = pd.Series(dtype="object")
    raw_audit["date"] = pd.to_datetime(raw_audit["date"], errors="coerce").dt.date
    raw_audit["code"] = raw_audit["code"].astype(str)
    audit_begin = pd.Timestamp(begin).date()
    audit_end = pd.Timestamp(end).date()
    invalid_dates = raw_audit["date"].isna()
    invalid_date_rows = int(invalid_dates.sum())
    raw_audit = raw_audit[
        ~invalid_dates
        & (raw_audit["date"] >= audit_begin)
        & (raw_audit["date"] <= audit_end)
    ].copy()
    if sample_dates is not None:
        sampled = {pd.Timestamp(day).date() for day in sample_dates}
        raw_audit = raw_audit[raw_audit["date"].isin(sampled)].copy()
    if codes:
        raw_audit = raw_audit[raw_audit["code"].isin(codes)].copy()

    if _partition_scan_source(storage.factor_root, sample_dates) is None and not any(
        storage.factor_root.glob("date=*")
    ):
        storage.register_factor_view()
    stored = storage.df(
        _validation_factor_query(
            storage,
            factor_columns,
            begin=begin,
            end=end,
            codes=codes,
            dates=sample_dates,
        )
    )
    if "date" not in stored:
        stored["date"] = pd.Series(dtype="object")
    if "code" not in stored:
        stored["code"] = pd.Series(dtype="object")
    stored["date"] = pd.to_datetime(stored["date"], errors="coerce").dt.date
    stored["code"] = stored["code"].astype(str)

    factor_range = storage.factor_date_range()
    # The validator narrows the factor scope to the bounded audit sample, but
    # evaluation needs a fingerprint for the complete requested materialized
    # interval.  Read the physical partition names directly so a five-day
    # independent audit does not make a full-history validation look stale.
    manifest_dates = _partition_dates(
        storage.factor_root,
        "1900-01-01",
        requested_end or end,
    )
    manifest_through = (
        manifest_dates[-1]
        if manifest_dates
        else (factor_range[1] if factor_range else (requested_end or end))
    )
    source_manifest = dataset_manifest(
        storage.factor_data_root,
        storage.factor_name,
        through=manifest_through,
    )
    outputs: list[Path] = []
    failures: list[str] = []
    evaluation_manifest_cache: dict[str, dict[str, Any]] = {}
    single_column = len(factor_columns) == 1
    metadata_columns = {
        column
        for column in raw_audit.columns
        if column not in {"date", "code", "sample_key"}
        and not str(column).startswith(("expected_", "stored_", "abs_diff"))
        and not str(column).endswith(("_expected", "_stored", "_abs_diff"))
        and column not in factor_columns
    }

    for factor_column in factor_columns:
        expected_col = _value_column(
            raw_audit,
            factor_name=factor_name,
            factor_column=factor_column,
            kind="expected",
            allow_generic=single_column,
        )
        selected_columns = ["date", "code", *sorted(metadata_columns)]
        if expected_col is not None and expected_col not in selected_columns:
            selected_columns.append(expected_col)
        audit = raw_audit[selected_columns].copy()
        if expected_col is None:
            audit["expected_value"] = np.nan
        else:
            audit["expected_value"] = pd.to_numeric(
                audit[expected_col],
                errors="coerce",
            )
        if factor_column in stored.columns:
            stored_values = stored[["date", "code", factor_column]].rename(
                columns={factor_column: "_stored_value"}
            )
        else:
            stored_values = stored[["date", "code"]].copy()
            stored_values["_stored_value"] = np.nan
        audit = audit.merge(
            stored_values, on=["date", "code"], how="outer", indicator="sample_key"
        )
        audit["stored_value"] = pd.to_numeric(audit["_stored_value"], errors="coerce")
        audit["abs_diff"] = (audit["expected_value"] - audit["stored_value"]).abs()
        canonical_columns = [
            "date",
            "code",
            "expected_value",
            "stored_value",
            "abs_diff",
            "sample_key",
            *sorted(metadata_columns),
        ]
        audit = audit[canonical_columns].sort_values(["date", "code"])

        central_summary = (
            _summary(
                audit,
                stored_col="stored_value",
                expected_col="expected_value",
                tolerance=tolerance,
                relative_tolerance=relative_tolerance,
            )
            .iloc[0]
            .to_dict()
        )
        reasons = [
            value
            for value in str(central_summary.get("failure_reasons") or "").split(",")
            if value
        ]
        for condition, reason in (
            (expected_col is None, "column_mapping"),
            (invalid_date_rows > 0, "invalid_date"),
            (factor_column not in stored.columns, "stored_column_missing"),
        ):
            if condition:
                reasons.append(reason)
        status = "fail" if reasons else str(central_summary["status"])
        validator_summary = _summary_row_for_column(
            raw_summary, factor_column, single_column=single_column
        )
        # Native reasons supplement a failed canonical audit.
        if (
            status != "pass"
            and validator_summary
            and str(validator_summary.get("status", "pass")).lower() != "pass"
        ):
            reasons.extend(
                value
                for value in str(
                    validator_summary.get("failure_reasons") or "native_validator"
                ).split(",")
                if value
            )
        if validator_error is not None:
            status = "fail"
            reasons.append("validator_error")
        central_summary["status"] = status
        central_summary["failure_reasons"] = ",".join(dict.fromkeys(reasons))
        central_summary = {
            str(key): _json_scalar(value) for key, value in central_summary.items()
        }

        destination = factor_column_output(
            output_root,
            factor_name,
            factor_column,
            project_root=project_root,
        )
        payload = read_config(destination / "config.json")
        evaluation = _evaluation_record(payload.get("evaluation"))
        previous_factor = payload.get("factor")
        evaluation_definition_changed = bool(evaluation) and (
            not isinstance(previous_factor, Mapping)
            or previous_factor.get("fingerprint") != fingerprint
            or evaluation.get("stale_reason") == "calculation_fingerprint_changed"
        )
        evaluation_source_changed = False
        if evaluation:
            source_manifests = evaluation.get("source_manifests")
            stored_evaluation_manifest = (
                source_manifests.get("factor")
                if isinstance(source_manifests, Mapping)
                else None
            )
            through = (
                stored_evaluation_manifest.get("through")
                if isinstance(stored_evaluation_manifest, Mapping)
                else None
            )
            if isinstance(through, str):
                if through not in evaluation_manifest_cache:
                    evaluation_manifest_cache[through] = dataset_manifest(
                        storage.factor_data_root,
                        storage.factor_name,
                        through=through,
                    )
                evaluation_source_changed = not manifests_match(
                    stored_evaluation_manifest,
                    evaluation_manifest_cache[through],
                )
            else:
                evaluation_source_changed = True
        if evaluation and (
            evaluation_definition_changed or evaluation_source_changed or status != "pass"
        ):
            stale_evaluation = dict(evaluation)
            stale_evaluation["status"] = "stale"
            stale_evaluation["stale_reason"] = (
                "calculation_fingerprint_changed"
                if evaluation_definition_changed
                else "validation_failed"
                if status != "pass"
                else "historical_factor_source_changed"
            )
            evaluation = stale_evaluation
        payload = {
            "factor": {
                "name": factor_name,
                "column": factor_column,
                "fingerprint": fingerprint,
                **(
                    {"dataset": storage.factor_name}
                    if storage.factor_name != factor_name
                    else {}
                ),
            },
            "validation": {
                "status": status,
                "begin": requested_begin or begin,
                "end": requested_end or end,
                "tolerance": tolerance,
                "relative_tolerance": relative_tolerance,
                "data": manifest_reference(source_manifest),
                "completed_at": pd.Timestamp.now(tz="UTC").isoformat(),
            },
            **({"evaluation": evaluation} if evaluation else {}),
        }
        if reasons or validator_error:
            payload["validation"]["error"] = validator_error or ",".join(
                dict.fromkeys(reasons)
            )
        report_columns.append(
            {
                "factor_column": factor_column,
                "watermark": source_manifest["maximum_date"],
                "validation": {
                    **payload["validation"],
                    "sample_begin": begin,
                    "sample_end": end,
                    "metrics": central_summary,
                },
            }
        )
        if persist:
            staging = stage_column_directory(destination)
            try:
                for filename in ("ic.csv", "returns.csv", "correlation.csv"):
                    copy_if_present(destination / filename, staging / filename)
                if evaluation.get("status") == "complete":
                    copy_if_present(destination / "overview.png", staging / "overview.png")
                audit.to_csv(staging / "audit.csv", index=False)
                write_config(staging / "config.json", payload)
                replace_column_directory(staging, destination)
            except Exception:
                if staging.exists():
                    shutil.rmtree(staging)
                raise
            outputs.append(destination / "audit.csv")
        if status != "pass":
            failures.append(
                f"{factor_column}={central_summary.get('failure_reasons') or 'unknown'}"
            )
    return outputs, failures, report_columns


def run_validation(
    *,
    factor_name: str,
    begin: str,
    end: str,
    data_root: str | Path | None = None,
    artifacts_root: str | Path | None = None,
    factor_zoo_root: str | Path | None = None,
    output_dir: str | Path | None = None,
    codes: Sequence[str] | None = None,
    lookback_days: int | None = None,
    tolerance: float | None = None,
    relative_tolerance: float | None = None,
    factor_column: str | None = None,
    storage_factor_name: str | None = None,
    threads: int | None = None,
    memory_limit: str | None = None,
    temp_directory: str | Path | None = None,
    max_sample_codes: int | None = None,
    max_sample_dates: int | None = None,
    fail_on_error: bool = True,
    project_root: str | Path | None = None,
    readme_path: str | Path | None = None,
    update_readme: bool = True,
    persist: bool = True,
) -> Path:
    """Run a native validator and normalize its output into canonical artifacts."""

    root = Path(project_root or Path.cwd()).resolve()
    FactorStorage.identifier(factor_name)
    settings = _validation_settings(root)
    lookback_days = (
        settings["lookback_days"] if lookback_days is None else lookback_days
    )
    tolerance = settings["tolerance"] if tolerance is None else tolerance
    relative_tolerance = (
        settings["relative_tolerance"]
        if relative_tolerance is None
        else relative_tolerance
    )
    validate_tolerances(tolerance, relative_tolerance)
    threads = settings["threads"] if threads is None else max(int(threads), 1)
    max_sample_codes = (
        settings["max_sample_codes"] if max_sample_codes is None else max_sample_codes
    )
    max_sample_dates = (
        settings["max_sample_dates"] if max_sample_dates is None else max_sample_dates
    )
    memory_limit = settings["memory_limit"] if memory_limit is None else memory_limit
    temp_directory = (
        settings["temp_directory"] if temp_directory is None else temp_directory
    )
    if temp_directory is not None:
        temp_directory = _resolve_project_path(temp_directory, root)
    print(f"Validate | {factor_name} | {begin} -> {end}", flush=True)
    print(f"Sample limit: {max_sample_dates} dates x {max_sample_codes} codes | "
          f"Tolerance: abs={tolerance:g}, rel={relative_tolerance:g}", flush=True)
    print(f"Resources: threads={threads}, memory={memory_limit or 'default'}, "
          f"lookback_days={lookback_days}", flush=True)
    resolved_data_root = (
        resolve_data_root(project_root=root)
        if data_root is None
        else _resolve_project_path(data_root, root)
    )
    resolved_factor_zoo_root = (
        resolve_factor_zoo_root(project_root=root)
        if factor_zoo_root is None
        else _resolve_project_path(factor_zoo_root, root)
    )
    target_readme = (
        _resolve_project_path(readme_path, root)
        if readme_path is not None
        else resolved_factor_zoo_root / factor_name / "README.md"
    )
    if update_readme:
        validate_readme_target(target_readme)
    default_output_root = resolve_artifacts_root(project_root=root, artifacts_root=artifacts_root) / "factor_zoo"
    canonical_root = (
        _resolve_project_path(output_dir, root)
        if output_dir is not None
        else default_output_root
    )
    if (
        storage_factor_name
        and storage_factor_name != factor_name
        and canonical_root.resolve() == (resolve_artifacts_root(project_root=root) / "factor_zoo").resolve()
    ):
        raise ValueError("isolated validation requires a separate --output-dir")
    staging_root = root / ".tmp" / "factor_validation" / factor_name / "scratch"
    if staging_root.exists():
        shutil.rmtree(staging_root)
    package = resolve_factor_package(
        factor_name,
        project_root=root,
        output_dir=staging_root,
        factor_zoo_root=resolved_factor_zoo_root,
    )
    module = load_module_from_path(
        package.module_path, project_root=root, factor_name=factor_name
    )
    definition = getattr(module, "validate_factor", None)
    if isinstance(definition, FactorValidationDefinition):
        validator = native_validator(
            definition.compute_validation_audit,
            factor_name=definition.factor_name,
            factor_columns=definition.factor_columns,
            validation_input_columns=definition.validation_input_columns,
            compute_validation_audits=definition.compute_validation_audits,
        )
    elif callable(definition):
        # Specialized factor packages may own a complete validator when their
        # audit shape cannot use the standard independent-recompute adapter.
        validator = definition
    else:
        raise ValueError(  # noqa: TRY004 - an invalid factor declaration violates its value contract.
            "factor package must declare validate_factor with "
            "define_factor_validator or a callable custom validator: "
            f"{package.module_path.as_posix()}"
        )
    identity = require_identity(
        resolve_factor_data_root(project_root=root, artifacts_root=artifacts_root) / (storage_factor_name or factor_name),
        fingerprint=factor_definition_fingerprint(package),
        columns=(factor_column,)
        if factor_column is not None
        else resolve_factor_columns(module),
    )
    if identity.get("factor_name") != factor_name:
        raise ValueError("factor identity does not match the requested package")
    storage = FactorStorage(
        data_root=resolved_data_root,
        project_root=root,
        artifacts_root=artifacts_root,
        factor_name=storage_factor_name or factor_name,
        threads=threads,
        memory_limit=memory_limit,
        temp_directory=temp_directory,
    )
    previous_memory_limit = os.environ.get("QUANTHUB_FACTOR_MEMORY_LIMIT")
    previous_temp_directory = os.environ.get("QUANTHUB_FACTOR_TEMP_DIRECTORY")
    previous_threads = os.environ.get("QUANTHUB_FACTOR_THREADS")
    validation_error: Exception | None = None
    try:
        if memory_limit:
            os.environ["QUANTHUB_FACTOR_MEMORY_LIMIT"] = str(memory_limit)
        if temp_directory:
            os.environ["QUANTHUB_FACTOR_TEMP_DIRECTORY"] = str(temp_directory)
        os.environ["QUANTHUB_FACTOR_THREADS"] = str(max(int(threads), 1))
        bounded_codes = (
            _bounded_validation_codes(codes, limit=max_sample_codes) if codes else None
        )
        sample_dates = _select_validation_dates(
            storage,
            begin=begin,
            end=end,
            limit=max_sample_dates,
        )
        effective_codes = (
            bounded_codes
            if codes
            else _select_validation_codes(
                storage,
                begin=begin,
                end=end,
                limit=max_sample_codes,
                dates=sample_dates,
            )
        )
        audit_begin = sample_dates[0] if sample_dates else begin
        audit_end = sample_dates[-1] if sample_dates else end
        storage.set_validation_factor_scope(audit_begin, audit_end)
        available_columns = resolve_factor_columns(module)
        selected_columns = (
            (factor_column,) if factor_column is not None else available_columns
        )
        unknown = sorted(set(selected_columns) - set(available_columns))
        if unknown:
            raise ValueError(f"unknown factor columns: {', '.join(unknown)}")
        result: Any = None
        validation_kwargs = {
            "storage": storage,
            "source_table": getattr(module, "VALIDATION_SOURCE_TABLE", "quotes_day"),
            "factor_name": factor_name,
            "storage_factor_name": storage_factor_name,
            "begin": audit_begin,
            "end": audit_end,
            "output_dir": package.dist_dir,
            "factor_columns": selected_columns,
            "codes": effective_codes or None,
            "lookback_days": lookback_days,
            "tolerance": tolerance,
            "relative_tolerance": relative_tolerance,
            # Native failure details are normalized into config.json
            # before the public command raises.
            "fail_on_error": False,
            "data_root": resolved_data_root.as_posix(),
            "factor_zoo_root": resolved_factor_zoo_root.as_posix(),
            "threads": threads,
            "factor_column": factor_column,
            "max_sample_codes": max_sample_codes,
            "max_sample_dates": max_sample_dates,
            "memory_limit": memory_limit,
            "temp_directory": temp_directory,
            "project_root": root,
        }
        try:
            result = call_validator(validator, validation_kwargs)
            if isinstance(result, FactorValidationDefinition):
                validator = native_validator(
                    result.compute_validation_audit,
                    factor_name=result.factor_name,
                    factor_columns=result.factor_columns,
                    validation_input_columns=result.validation_input_columns,
                    compute_validation_audits=result.compute_validation_audits,
                )
                result = call_validator(validator, validation_kwargs)
        except Exception as exc:  # noqa: BLE001 - preserve any audit emitted before failure.
            validation_error = exc
        result_path = Path(result) if result is not None else None
        raw_audit_path = (
            result_path
            if result_path is not None
            and result_path.is_file()
            and result_path.name in {"audit.csv", "audit_sample.csv"}
            else _find_artifact(package.dist_dir, ("audit_sample.csv", "audit.csv"))
        )
        raw_summary_path = _find_artifact(
            package.dist_dir,
            ("validation_summary.csv",),
        )
        outputs, failures, report_columns = _write_canonical_validation(
            storage=storage,
            factor_name=factor_name,
            factor_columns=selected_columns,
            fingerprint=identity["fingerprint"],
            raw_audit_path=raw_audit_path,
            raw_summary_path=raw_summary_path,
            begin=audit_begin,
            end=audit_end,
            codes=effective_codes,
            tolerance=tolerance,
            relative_tolerance=relative_tolerance,
            output_root=canonical_root,
            data_root=resolved_data_root,
            project_root=root,
            requested_begin=begin,
            requested_end=end,
            sample_dates=sample_dates,
            persist=persist,
            validator_error=(
                f"{type(validation_error).__name__}: {validation_error}"
                if validation_error is not None
                else None
            ),
        )
        if validation_error is not None:
            failures.append(
                f"validator={type(validation_error).__name__}: {validation_error}"
            )
        report = {
            "factor_name": factor_name,
            "test_date": datetime.now().astimezone().date().isoformat(),
            "scope": "isolated_sample"
            if storage_factor_name and storage_factor_name != factor_name
            else "materialized_sample",
            "storage_factor_name": storage.factor_name,
            "declared_column_count": len(available_columns),
            "status": "fail" if failures else "pass",
            "configuration": {
                "begin": begin,
                "end": end,
                "tolerance": tolerance,
                "relative_tolerance": relative_tolerance,
                "lookback_days": lookback_days,
                "threads": threads,
                "memory_limit": memory_limit,
            },
            "columns": report_columns,
        }
        if update_readme:
            update_factor_readme(target_readme, report)
        passed = sum(column["validation"]["status"] == "pass" for column in report_columns)
        print(f"Validate: pass={passed}, fail={len(report_columns) - passed}", flush=True)
        reasons = Counter(reason for column in report_columns
                          if column["validation"]["status"] != "pass"
                          for reason in column["validation"]["metrics"]["failure_reasons"].split(","))
        for reason, count in reasons.items():
            print(f"  FAIL {reason}: {count} columns", flush=True)
        if validation_error is not None:
            print(f"  {type(validation_error).__name__}: {validation_error}", flush=True)
        if update_readme:
            print(f"README: {target_readme.as_posix()}", flush=True)
        if fail_on_error and failures:
            raise AssertionError("validation failed: " + "; ".join(failures))
        return outputs[0] if len(outputs) == 1 else canonical_root / factor_name
    finally:
        storage.clear_validation_factor_scope()
        if previous_memory_limit is None:
            os.environ.pop("QUANTHUB_FACTOR_MEMORY_LIMIT", None)
        else:
            os.environ["QUANTHUB_FACTOR_MEMORY_LIMIT"] = previous_memory_limit
        if previous_temp_directory is None:
            os.environ.pop("QUANTHUB_FACTOR_TEMP_DIRECTORY", None)
        else:
            os.environ["QUANTHUB_FACTOR_TEMP_DIRECTORY"] = previous_temp_directory
        if previous_threads is None:
            os.environ.pop("QUANTHUB_FACTOR_THREADS", None)
        else:
            os.environ["QUANTHUB_FACTOR_THREADS"] = previous_threads
        storage.close()
        if staging_root.exists():
            shutil.rmtree(staging_root)


def calculate_sample(args: argparse.Namespace, root: Path) -> None:
    """Replace one isolated SQL sample; no run history is retained."""
    workspace = root / ".tmp" / "factor_validate" / args.factor_name
    data = workspace / "data"
    source = resolve_data_root(project_root=root, data_root=args.data_root)
    if data.resolve().is_relative_to(source.resolve()) or source.resolve().is_relative_to(workspace):
        raise ValueError("sample workspace must be separate from DATA")
    data.mkdir(parents=True, exist_ok=True)
    alias = f"sample_{args.factor_name}"
    for table in source.iterdir():
        if (not table.is_dir() or table.name in (args.factor_name, alias)
                or table.name.startswith((".", "validation_", "benchmark_", "sample_", "_materialize_"))):
            continue
        link = data / table.name
        if link.exists():
            if link.resolve() != table.resolve():
                raise ValueError(f"unexpected sample input: {link}")
        elif os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(table.resolve())],
                           check=True, capture_output=True)
        else:
            link.symlink_to(table.resolve(), target_is_directory=True)
    artifacts = workspace / "artifacts"
    sample = artifacts / "factor_data" / alias
    if sample.resolve().parent != (artifacts / "factor_data").resolve():
        raise ValueError("sample output must not be an external link")
    if sample.exists():
        shutil.rmtree(sample)
    args.data_root = str(data)
    args.artifacts_root = str(artifacts)
    args.storage_factor_name = alias
    args.output_dir = str(workspace / "state")
    args.temp_directory = str(workspace / "spill")
    settings = _validation_settings(root)
    command = [sys.executable, str(Path(__file__).resolve().parents[2]
               / "factor-calculate/scripts/run_calculate.py"),
               "--project-root", str(root), "--factor-name", args.factor_name,
               "--begin", args.begin, "--end", args.end, "--data-root", str(data),
               "--artifacts-root", str(artifacts),
               "--storage-factor-name", alias, "--threads", str(args.threads or settings["threads"]),
               "--compute-batch-days", "5", "--temp-directory", args.temp_directory]
    if args.factor_zoo_root:
        command.extend(("--factor-zoo-root", args.factor_zoo_root))
    memory = args.memory_limit or settings["memory_limit"]
    if memory:
        command.extend(("--memory-limit", memory))
    print(f"SQL sample | {args.factor_name} | {args.begin} -> {args.end} | batch_days=5", flush=True)
    result = subprocess.run(command, cwd=root, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode:
        reason = (result.stderr or result.stdout).strip().splitlines()
        raise ValueError("SQL sample calculation failed: " + (reason[-1] if reason else f"exit {result.returncode}"))


def write_failed_readme(args: argparse.Namespace, error: Exception) -> None:
    """Replace stale successful results when validation cannot start."""
    root = Path(args.project_root).resolve()
    zoo = resolve_factor_zoo_root(project_root=root, factor_zoo_root=args.factor_zoo_root)
    package = resolve_factor_package(args.factor_name, project_root=root, factor_zoo_root=zoo)
    module = load_module_from_path(package.module_path, project_root=root, factor_name=args.factor_name)
    columns = (args.factor_column,) if args.factor_column else resolve_factor_columns(module)
    settings = _validation_settings(root)
    report = {
        "scope": "isolated_sample" if args.sample else "materialized_sample",
        "declared_column_count": len(resolve_factor_columns(module)),
        "status": "fail", "test_date": datetime.now().astimezone().date().isoformat(),
        "configuration": {"begin": args.begin, "end": args.end,
                          **{key: getattr(args, key) if getattr(args, key) is not None else settings[key]
                             for key in ("threads", "memory_limit", "lookback_days", "tolerance", "relative_tolerance")}},
        "columns": [{"factor_column": column, "watermark": None, "validation": {
            "sample_begin": args.begin, "sample_end": args.end, "status": "fail", "error": str(error),
            "metrics": dict(audit_date_count=0, audit_code_count=0, audit_rows=0, coverage=0,
                            null_mismatches=0, tolerance_violations=0, duplicate_keys=0, max_abs_diff=None)
        }} for column in columns],
    }
    target = _resolve_project_path(args.readme_path, root) if args.readme_path else zoo / args.factor_name / "README.md"
    update_factor_readme(target, report)
    print(f"Validate: pass=0, fail={len(columns)} | {error}", flush=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Run deterministic independent factor validation."
    )
    p.add_argument("--factor-name", required=True)
    p.add_argument("--sample", action="store_true", help="Calculate a fresh isolated SQL sample before validation.")
    p.add_argument("--begin", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--data-root")
    p.add_argument("--artifacts-root")
    p.add_argument("--factor-zoo-root")
    p.add_argument("--output-dir")
    p.add_argument("--codes")
    p.add_argument("--lookback-days", type=int)
    p.add_argument("--tolerance", type=float)
    p.add_argument("--relative-tolerance", type=float)
    p.add_argument("--factor-column")
    p.add_argument(
        "--storage-factor-name", help="Validate an isolated materialized factor table."
    )
    p.add_argument("--threads", type=int)
    p.add_argument("--memory-limit")
    p.add_argument("--temp-directory")
    p.add_argument("--max-sample-codes", type=int)
    p.add_argument("--max-sample-dates", type=int)
    p.add_argument("--project-root", default=".")
    p.add_argument(
        "--readme-path",
        help="Override the factor README path, relative to project root.",
    )
    p.add_argument(
        "--no-readme-update",
        action="store_true",
        help="Update machine validation state without modifying the README.",
    )
    a = p.parse_args(argv)
    a.factor_name = FactorStorage.identifier(a.factor_name)
    codes = (
        tuple(x.strip() for x in a.codes.split(",") if x.strip()) if a.codes else None
    )
    if not a.no_readme_update:
        from factor_protocol.readme import update_acceptance
        root = Path(a.project_root).resolve()
        zoo = resolve_factor_zoo_root(project_root=root, factor_zoo_root=a.factor_zoo_root)
        target = _resolve_project_path(a.readme_path, root) if a.readme_path else zoo / a.factor_name / "README.md"
        update_acceptance(target, 4, False)
    try:
        if a.sample:
            calculate_sample(a, Path(a.project_root).resolve())
        run_validation(
            factor_name=a.factor_name,
            begin=a.begin,
            end=a.end,
            data_root=a.data_root,
            artifacts_root=a.artifacts_root,
            factor_zoo_root=a.factor_zoo_root,
            output_dir=a.output_dir,
            codes=codes,
            lookback_days=a.lookback_days,
            tolerance=a.tolerance,
            relative_tolerance=a.relative_tolerance,
            factor_column=a.factor_column,
            storage_factor_name=a.storage_factor_name,
            threads=a.threads,
            memory_limit=a.memory_limit,
            temp_directory=a.temp_directory,
            max_sample_codes=a.max_sample_codes,
            max_sample_dates=a.max_sample_dates,
            project_root=Path(a.project_root).resolve(),
            readme_path=a.readme_path,
            update_readme=not a.no_readme_update,
            persist=not a.sample,
        )
    except Exception as exc:
        if not a.no_readme_update and not isinstance(exc, AssertionError):
            write_failed_readme(a, exc)
        if not isinstance(exc, AssertionError):
            print(f"ERROR: {exc}")
        return 1
    finally:
        if a.sample:
            workspace = Path(a.project_root).resolve() / ".tmp" / "factor_validate" / a.factor_name
            data = workspace / "data"
            for directory in (data, workspace / "artifacts" / "factor_data"):
                if directory.exists():
                    for table in directory.iterdir():
                        if table.is_symlink():
                            table.unlink()
                        elif table.is_junction():
                            table.rmdir()
            if workspace.exists():
                shutil.rmtree(workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
