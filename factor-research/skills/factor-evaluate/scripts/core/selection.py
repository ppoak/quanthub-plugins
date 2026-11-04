"""Derive training direction and validation selection from canonical IC rows."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from factor_protocol import FactorStorage

VARIANTS = ("raw", "neutralized", "rolling_mean")


def selection_query(
    *,
    begin: str,
    train_end: str,
    validation_end: str,
    ic_source: str,
    requested_source: str,
) -> str:
    literal = FactorStorage.sql_literal
    variants = ", ".join(
        f"({literal(variant)}, {index})" for index, variant in enumerate(VARIANTS)
    )
    return f"""
            WITH requested AS ({requested_source}),
            variant_grid(variant, variant_order) AS (
                VALUES {variants}
            ),
            train_raw AS (
                SELECT
                    ic.factor_column,
                    AVG(ic.ic) AS train_mean_ic,
                    COUNT(*) AS train_ic_days
                FROM ({ic_source}) ic
                JOIN requested USING (factor_column)
                WHERE ic.variant = 'raw'
                  AND ic.date >= CAST(
                      {literal(begin)} AS DATE
                  )
                  AND ic.date <= CAST(
                      {literal(train_end)} AS DATE
                  )
                GROUP BY ic.factor_column
            ),
            validation_stats AS (
                SELECT
                    ic.factor_column,
                    ic.variant,
                    AVG(ic.ic) AS validation_mean_ic,
                    STDDEV_SAMP(ic.ic) AS validation_stddev_ic,
                    COUNT(*) AS validation_ic_days
                FROM ({ic_source}) ic
                JOIN requested USING (factor_column)
                WHERE ic.date > CAST(
                      {literal(train_end)} AS DATE
                  )
                  AND ic.date <= CAST(
                      {literal(validation_end)} AS DATE
                  )
                GROUP BY ic.factor_column, ic.variant
            ),
            candidates AS (
                SELECT
                    requested.factor_column,
                    grid.variant,
                    grid.variant_order,
                    CASE WHEN train.train_mean_ic > 0.0 THEN 1 ELSE -1 END
                        AS direction_sign,
                    train.train_mean_ic,
                    COALESCE(train.train_ic_days, 0) AS train_ic_days,
                    validation.validation_mean_ic,
                    validation.validation_stddev_ic,
                    validation.validation_mean_ic / NULLIF(
                        validation.validation_stddev_ic,
                        0.0
                    ) AS validation_ic_ir,
                    COALESCE(validation.validation_ic_days, 0)
                        AS validation_ic_days
                FROM requested
                LEFT JOIN train_raw train USING (factor_column)
                CROSS JOIN variant_grid grid
                LEFT JOIN validation_stats validation
                  ON validation.factor_column = requested.factor_column
                 AND validation.variant = grid.variant
            ),
            scored AS (
                SELECT
                    candidates.*,
                    CASE
                        WHEN validation_mean_ic IS NULL THEN FALSE
                        WHEN direction_sign > 0 THEN validation_mean_ic > 0.0
                        ELSE validation_mean_ic < 0.0
                    END AS direction_consistent
                FROM candidates
            ),
            eligible AS (
                SELECT
                    scored.*,
                    MAX(
                        CASE
                            WHEN direction_consistent
                             AND isfinite(validation_ic_ir)
                            THEN 1 ELSE 0
                        END
                    ) OVER (PARTITION BY factor_column) AS has_eligible
                FROM scored
            ),
            ranked AS (
                SELECT
                    eligible.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY factor_column
                        ORDER BY
                            CASE
                                WHEN has_eligible = 1 THEN
                                    CASE
                                        WHEN direction_consistent
                                         AND isfinite(validation_ic_ir)
                                        THEN 0 ELSE 1
                                    END
                                ELSE
                                    CASE
                                        WHEN isfinite(validation_ic_ir)
                                        THEN 0 ELSE 1
                                    END
                            END,
                            ABS(validation_ic_ir) DESC NULLS LAST,
                            variant_order
                    ) AS selection_rank
                FROM eligible
            )
            SELECT
                factor_column,
                variant,
                direction_sign,
                train_mean_ic,
                train_ic_days,
                validation_mean_ic,
                validation_stddev_ic,
                validation_ic_ir,
                validation_ic_days,
                direction_consistent,
                selection_rank = 1 AS selected
            FROM ranked
    """


def read_selection_rows(
    connection: Any,
    path: Path,
    column: str,
    parameters: Mapping[str, Any],
    begin: str,
) -> list[tuple[Any, ...]]:
    literal = FactorStorage.sql_literal
    source = f"""
        SELECT {literal(column)} AS factor_column, CAST(date AS DATE) AS date,
               CAST(variant AS VARCHAR) AS variant, TRY_CAST(ic AS DOUBLE) AS ic
        FROM read_csv_auto({literal(path.as_posix())}, header=TRUE)
        WHERE TRY_CAST(ic AS DOUBLE) IS NOT NULL AND isfinite(TRY_CAST(ic AS DOUBLE))
    """
    query = selection_query(
        begin=begin,
        train_end=str(parameters["train_end"]),
        validation_end=str(parameters["validation_end"]),
        ic_source=source,
        requested_source=f"SELECT {literal(column)} AS factor_column",
    )
    return connection.execute(query).fetchall()


def selection_details(rows: list[tuple[Any, ...]], column: str) -> dict[str, Any]:
    if len(rows) != len(VARIANTS):
        raise ValueError(f"evaluation selection is incomplete: {column}")
    selected = next((row[1] for row in rows if row[10]), None)
    if selected is None:
        raise ValueError(f"evaluation variant is not selected: {column}")
    return {
        "direction": {
            "mean_ic": rows[0][3],
            "top_side": "high" if rows[0][2] > 0 else "low",
        },
        "variant": {
            "selected": selected,
            "candidates": {row[1]: {"ic_ir": row[7]} for row in rows},
        },
    }
