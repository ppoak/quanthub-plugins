"""Minimal SQL-production and Python-validation factor package template."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
from factor_protocol import FactorStorage, define_factor_validator

FACTOR_NAME = "example_factor"
FACTOR_COLUMNS = ("example_factor",)
# Set these after inspecting the active project's data.
SOURCE_TABLE = "source_table"
SOURCE_VALUE_COLUMN = "input_value"
VALIDATION_SOURCE_TABLE = SOURCE_TABLE
UPDATE_CALENDAR = SOURCE_TABLE
UPDATE_SOURCE_TABLES = (SOURCE_TABLE,)
VALIDATION_INPUT_COLUMNS = ("code", "date", SOURCE_VALUE_COLUMN)


def build_factor_sql(storage: FactorStorage, *, begin: str, end: str) -> str:
    """Return the DuckDB SQL for one bounded production interval."""
    source_table = storage.identifier(SOURCE_TABLE)
    value_column = storage.identifier(SOURCE_VALUE_COLUMN)
    source = storage.partition_scan_sql(source_table, begin=begin, end=end)
    return f"""
        SELECT
            CAST(code AS VARCHAR) AS code,
            CAST(date AS DATE) AS date,
            CAST({value_column} AS DOUBLE) AS example_factor
        FROM {source}
        WHERE date >= CAST('{begin}' AS DATE)
          AND date <= CAST('{end}' AS DATE)
    """


def calculate_factor(
    *,
    storage: FactorStorage | None = None,
    factor_name: str = FACTOR_NAME,
    begin: str,
    end: str | None = None,
    data_root: str | Path | None = None,
    threads: int = 1,
    compute_batch_days: int | None = None,
    memory_limit: str | None = None,
    temp_directory: str | Path | None = None,
    **_: Any,
) -> Path:
    """Calculate and write a production interval entirely through DuckDB SQL."""
    own_storage = storage is None
    st = storage or FactorStorage(
        factor_name=factor_name,
        data_root=data_root,
        threads=threads,
        memory_limit=memory_limit,
        temp_directory=temp_directory,
    )
    try:
        for batch_begin, batch_end in st.partition_date_batches(
            SOURCE_TABLE,
            begin=begin,
            end=end,
            batch_days=compute_batch_days or 20,
        ):
            st.write_factor_query(
                build_factor_sql(st, begin=batch_begin, end=batch_end)
            )
        return st.factor_root
    finally:
        if own_storage:
            st.close()


def compute_validation_audit(source: pd.DataFrame, cfg: object) -> pd.DataFrame:
    """Independently recompute the example factor with Python on bounded raw rows."""
    factor_column = str(getattr(cfg, "factor_column", None) or FACTOR_COLUMNS[0])
    if factor_column not in FACTOR_COLUMNS:
        raise ValueError(f"unsupported factor column: {factor_column}")
    audit = source.loc[:, ["code", "date", SOURCE_VALUE_COLUMN]].copy()
    audit[f"expected_{factor_column}"] = pd.to_numeric(
        audit[SOURCE_VALUE_COLUMN],
        errors="coerce",
    )
    return audit.loc[:, ["code", "date", f"expected_{factor_column}"]]


validate_factor = define_factor_validator(
    compute_validation_audit,
    factor_name=FACTOR_NAME,
    factor_columns=FACTOR_COLUMNS,
    validation_input_columns=VALIDATION_INPUT_COLUMNS,
)
