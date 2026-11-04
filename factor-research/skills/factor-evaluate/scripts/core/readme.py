"""Render canonical evaluation artifacts into the factor README section 8."""

from __future__ import annotations

import math
import os
import re
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

import duckdb
from .validation import check_package_evaluation as check_evaluation_config
from factor_protocol import resolve_data_root
from factor_protocol.readme import set_acceptance
from core.selection import read_selection_rows, selection_details

RESULTS_START_MARKER = "<!-- factor-evaluate:results:start -->"
RESULTS_END_MARKER = "<!-- factor-evaluate:results:end -->"
TABLE_HEADERS = (
    "测试日期",
    "评估区间",
    "因子列",
    "选定变体",
    "方向",
    "训练期平均 IC",
    "验证期 IC IR",
    "测试期 IC 天数",
    "测试期 IC 均值",
    "测试期 IC 标准差",
    "测试期 IC IR",
    "Top 净收益均值",
    "Top 期末净 NAV 均值",
    "Top 换手均值",
    "参考因子平均绝对相关",
    "评估水位",
    "状态",
    "备注",
)
TABLE_HEADER_LINE = "| " + " | ".join(TABLE_HEADERS) + " |"
TABLE_SEPARATOR = "|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---|"


class IncompleteEvaluationArtifacts(ValueError):
    """Raised when canonical artifacts cannot support a complete README table."""


def _section_bounds(lines: list[str]) -> tuple[int, int]:
    starts: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        match = re.match(r"^(#{2,3})\s+8[.．、]\s*因子评估\s*$", line.strip())
        if match:
            starts.append((index, len(match.group(1))))
    if len(starts) != 1:
        raise ValueError("factor README must contain exactly one section 8 因子评估")
    start, heading_level = starts[0]
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if (match := re.match(r"^(#+)\s", lines[index]))
            and len(match.group(1)) <= heading_level
        ),
        len(lines),
    )
    return start, end


def _table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _format_number(value: Any) -> str:
    parsed = _number(value)
    return "—" if parsed is None else f"{parsed:.6f}"


def _format_cell(value: Any) -> str:
    text = "—" if value is None else str(value)
    return " ".join(text.replace("|", "/").split()) or "—"


def _local_date(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone().date().isoformat()


def _watermark(payload: Mapping[str, Any], column_dir: Path) -> str:
    filenames = ["ic.csv", "returns.csv"]
    if payload["evaluation"]["parameters"].get("reference_factors"):
        filenames.append("correlation.csv")
    dates: list[str] = []
    with duckdb.connect(":memory:", config={"threads": 1}) as connection:
        for filename in filenames:
            row = connection.execute(
                "SELECT MAX(TRY_CAST(date AS DATE)) FROM read_csv_auto(?, header=TRUE)",
                [(column_dir / filename).as_posix()],
            ).fetchone()
            if not row or row[0] is None:
                raise IncompleteEvaluationArtifacts(
                    f"evaluation artifact has no dates: {filename}"
                )
            dates.append(row[0].isoformat())
    return min(dates)


def _csv_path(output_root: Path, factor_name: str, column: str, filename: str) -> Path:
    path = output_root / factor_name / column / filename
    if not path.is_file():
        raise IncompleteEvaluationArtifacts(
            f"missing evaluation artifact: {path.as_posix()}"
        )
    return path


def _test_metrics(
    connection: duckdb.DuckDBPyConnection,
    *,
    output_root: Path,
    factor_name: str,
    column: str,
    variant: str | None,
    validation_end: str,
    watermark: str,
    has_references: bool,
) -> dict[str, Any]:
    if not variant:
        return {
            "ic_days": None,
            "ic_mean": None,
            "ic_stddev": None,
            "ic_ir": None,
            "top_net_return_mean": None,
            "top_net_nav_mean": None,
            "top_turnover_mean": None,
            "mean_abs_correlation": None,
        }

    ic_path = _csv_path(output_root, factor_name, column, "ic.csv")
    ic_row = connection.execute(
        """
        SELECT COUNT(DISTINCT CAST(date AS DATE)), AVG(value), STDDEV_SAMP(value)
        FROM (
            SELECT CAST(date AS DATE) AS date, TRY_CAST(ic AS DOUBLE) AS value
            FROM read_csv_auto(?, header=TRUE)
            WHERE CAST(variant AS VARCHAR) = ?
              AND CAST(date AS DATE) > CAST(? AS DATE)
              AND CAST(date AS DATE) <= CAST(? AS DATE)
        )
        WHERE value IS NOT NULL AND isfinite(value)
        """,
        [ic_path.as_posix(), variant, validation_end, watermark],
    ).fetchone()

    returns_path = _csv_path(output_root, factor_name, column, "returns.csv")
    return_row = connection.execute(
        """
        WITH daily_top AS (
            SELECT
                CAST(date AS DATE) AS date,
                CAST(portfolio_id AS INTEGER) AS portfolio_id,
                MAX(TRY_CAST(top_net_return_5bps AS DOUBLE)) AS net_return,
                MAX(TRY_CAST(top_net_return_5bps_nav AS DOUBLE)) AS net_nav,
                MAX(TRY_CAST(top_turnover AS DOUBLE)) AS turnover
            FROM read_csv_auto(?, header=TRUE)
            WHERE CAST(variant AS VARCHAR) = ?
              AND CAST(date AS DATE) > CAST(? AS DATE)
              AND CAST(date AS DATE) <= CAST(? AS DATE)
            GROUP BY CAST(date AS DATE), CAST(portfolio_id AS INTEGER)
        ), latest_nav AS (
            SELECT portfolio_id, net_nav
            FROM daily_top
            WHERE net_nav IS NOT NULL AND isfinite(net_nav)
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY portfolio_id ORDER BY date DESC
            ) = 1
        )
        SELECT
            AVG(net_return) FILTER (WHERE net_return IS NOT NULL AND isfinite(net_return)),
            (SELECT AVG(net_nav) FROM latest_nav),
            AVG(turnover) FILTER (WHERE turnover IS NOT NULL AND isfinite(turnover))
        FROM daily_top
        """,
        [returns_path.as_posix(), variant, validation_end, watermark],
    ).fetchone()

    mean_abs_correlation = None
    if has_references:
        correlation_path = _csv_path(
            output_root, factor_name, column, "correlation.csv"
        )
        correlation_row = connection.execute(
            """
            SELECT AVG(ABS(value))
            FROM (
                SELECT TRY_CAST(correlation AS DOUBLE) AS value
                FROM read_csv_auto(?, header=TRUE)
                WHERE CAST(variant AS VARCHAR) = ?
                  AND CAST(date AS DATE) > CAST(? AS DATE)
                  AND CAST(date AS DATE) <= CAST(? AS DATE)
            )
            WHERE value IS NOT NULL AND isfinite(value)
            """,
            [correlation_path.as_posix(), variant, validation_end, watermark],
        ).fetchone()
        mean_abs_correlation = correlation_row[0] if correlation_row else None

    ic_count = int(ic_row[0] or 0) if ic_row else 0
    ic_mean = ic_row[1] if ic_row else None
    ic_stddev = ic_row[2] if ic_row else None
    ic_ir = (
        float(ic_mean) / float(ic_stddev)
        if _number(ic_mean) is not None and _number(ic_stddev) not in {None, 0.0}
        else None
    )
    return {
        "ic_days": ic_count,
        "ic_mean": ic_mean,
        "ic_stddev": ic_stddev,
        "ic_ir": ic_ir,
        "top_net_return_mean": return_row[0] if return_row else None,
        "top_net_nav_mean": return_row[1] if return_row else None,
        "top_turnover_mean": return_row[2] if return_row else None,
        "mean_abs_correlation": mean_abs_correlation,
    }


def _evaluation_scope(payload: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    evaluation = payload["evaluation"]
    parameters = evaluation.get("parameters")
    if not isinstance(parameters, dict) or not all(
        isinstance(parameters.get(key), str) for key in ("train_end", "validation_end")
    ):
        raise IncompleteEvaluationArtifacts(
            "evaluation config has incomplete period parameters"
        )
    sources = evaluation.get("source_manifests")
    if not isinstance(sources, dict) or not sources:
        raise IncompleteEvaluationArtifacts("evaluation config has no source snapshots")
    signature = {
        "begin": evaluation.get("begin"),
        "requested_end": evaluation.get("requested_end"),
        "parameters": parameters,
        "source_manifests": sources,
    }
    return signature, parameters["validation_end"]


def _render_period_line(signature: Mapping[str, Any], watermark: str) -> str:
    parameters = signature["parameters"]
    train_end = parameters["train_end"]
    validation_end = parameters["validation_end"]
    validation_begin = (date.fromisoformat(train_end) + timedelta(days=1)).isoformat()
    test_begin = (date.fromisoformat(validation_end) + timedelta(days=1)).isoformat()
    return (
        f"评估时间划分：训练期 `{signature['begin']}..{train_end}`；"
        f"验证期 `{validation_begin}..{validation_end}`；"
        f"测试期 `{test_begin}..{watermark}`。"
    )


def _make_row(
    *,
    column: str,
    payload: Mapping[str, Any],
    output_root: Path,
    factor_name: str,
    watermark: str,
    validation_end: str,
    connection: duckdb.DuckDBPyConnection,
) -> list[str]:
    evaluation = payload["evaluation"]
    selection = selection_details(
        read_selection_rows(
            connection,
            _csv_path(output_root, factor_name, column, "ic.csv"),
            column,
            evaluation["parameters"],
            str(evaluation["begin"]),
        ),
        column,
    )
    direction = selection.get("direction") or {}
    variant_selection = selection.get("variant") or {}
    selected_variant = variant_selection.get("selected")
    if selected_variant not in {"raw", "neutralized", "rolling_mean"}:
        selected_variant = None
    candidates = variant_selection.get("candidates") or {}
    selected_metrics = candidates.get(selected_variant, {}) if selected_variant else {}
    metrics = _test_metrics(
        connection,
        output_root=output_root,
        factor_name=factor_name,
        column=column,
        variant=selected_variant,
        validation_end=validation_end,
        watermark=watermark,
        has_references=bool(evaluation.get("parameters", {}).get("reference_factors")),
    )
    interval_begin = str(evaluation.get("begin") or "—")
    completed_at = str(evaluation["completed_at"])
    test_date = _local_date(completed_at)
    cells: list[Any] = [
        test_date,
        f"{interval_begin}..{watermark}",
        f"`{column}`",
        f"`{selected_variant}`" if selected_variant else "—",
        str(direction.get("top_side") or "—"),
        _format_number(direction.get("mean_ic")),
        _format_number(selected_metrics.get("ic_ir")),
        metrics["ic_days"] if metrics["ic_days"] is not None else "—",
        _format_number(metrics["ic_mean"]),
        _format_number(metrics["ic_stddev"]),
        _format_number(metrics["ic_ir"]),
        _format_number(metrics["top_net_return_mean"]),
        _format_number(metrics["top_net_nav_mean"]),
        _format_number(metrics["top_turnover_mean"]),
        _format_number(metrics["mean_abs_correlation"]),
        watermark,
        "complete",
        "规范评估产物",
    ]
    return [_format_cell(value) for value in cells]


def _render_table(rows: Sequence[Sequence[str]]) -> list[str]:
    rendered_rows = [
        "| " + " | ".join(_format_cell(value) for value in row) + " |" for row in rows
    ]
    return [TABLE_HEADER_LINE, TABLE_SEPARATOR, *rendered_rows]


def _replace_managed_block(
    section: list[str], *, period_line: str, table_lines: list[str]
) -> list[str]:
    starts = [
        i for i, line in enumerate(section) if line.strip() == RESULTS_START_MARKER
    ]
    ends = [i for i, line in enumerate(section) if line.strip() == RESULTS_END_MARKER]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise ValueError("factor README requires one evaluation results block")
    body = _managed_body(period_line, _table_rows_from_lines(table_lines))
    return [*section[:starts[0]], *body, *section[ends[0] + 1:]]


def _managed_body(period_line: str, rows: Sequence[Sequence[str]]) -> list[str]:
    return [
        RESULTS_START_MARKER,
        period_line,
        "",
        *_render_table(rows),
        RESULTS_END_MARKER,
    ]


def _table_rows_from_lines(lines: Sequence[str]) -> list[list[str]]:
    return [_table_cells(line) for line in lines[2:] if line.lstrip().startswith("|")]


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def update_factor_readme(
    *,
    readme_path: Path,
    output_root: Path,
    factor_name: str,
    columns: Sequence[str],
    project_root: str | Path = ".",
    data_root: str | Path | None = None,
    failures: Mapping[str, str] | None = None,
) -> bool:
    if not readme_path.is_file():
        raise FileNotFoundError(f"factor README not found: {readme_path.as_posix()}")
    if not columns:
        raise IncompleteEvaluationArtifacts(
            f"no declared factor columns for {factor_name}"
        )

    resolved_data_root = resolve_data_root(
        project_root=project_root, data_root=data_root
    )
    manifest_cache: dict[tuple[str, str], dict[str, Any]] = {}
    payloads: dict[str, dict[str, Any]] = {}
    signatures: list[dict[str, Any]] = []
    watermarks: list[str] = []
    validation_ends: list[str] = []
    for column in columns:
        if failures and column in failures:
            continue
        config_path = output_root / factor_name / column / "config.json"
        try:
            payload = check_evaluation_config(
                config_path.parent,
                factor_name=factor_name,
                data_root=resolved_data_root,
                project_root=project_root,
                manifest_cache=manifest_cache,
            )
        except (OSError, ValueError) as exc:
            raise IncompleteEvaluationArtifacts(str(exc)) from exc
        for filename in ("ic.csv", "returns.csv", "correlation.csv"):
            _csv_path(output_root, factor_name, column, filename)
        signature, validation_end = _evaluation_scope(payload)
        signatures.append(signature)
        validation_ends.append(validation_end)
        watermarks.append(_watermark(payload, config_path.parent))
        payloads[column] = payload

    if signatures and any(signature != signatures[0] for signature in signatures[1:]):
        raise IncompleteEvaluationArtifacts(
            f"factor columns have inconsistent evaluation periods or parameters: {factor_name}"
        )
    watermark = min(watermarks) if watermarks else "—"
    validation_end = validation_ends[0] if validation_ends else "—"

    connection = duckdb.connect(":memory:", config={"threads": 1})
    try:
        rows = [
            _make_row(
                column=column,
                payload=payloads[column],
                output_root=output_root,
                factor_name=factor_name,
                watermark=watermark,
                validation_end=validation_end,
                connection=connection,
            )
            for column in columns if column in payloads
        ]
    finally:
        connection.close()

    original = readme_path.read_text(encoding="utf-8")
    lines = original.splitlines()
    start, end = _section_bounds(lines)
    section = lines[start:end]
    for column, error in (failures or {}).items():
        row = ["—"] * len(TABLE_HEADERS)
        row[0], row[2], row[-2], row[-1] = date.today().isoformat(), f"`{column}`", "fail", error
        rows.append(row)
    period_line = (_render_period_line(signatures[0], watermark) if signatures
                   else "评估时间划分：本次评估失败，原因见表格。")
    rendered_table = _render_table(rows)
    updated_section = _replace_managed_block(
        section,
        period_line=period_line,
        table_lines=rendered_table,
    )
    updated = "\n".join([*lines[:start], *updated_section, *lines[end:]]) + "\n"
    complete = not failures and len(payloads) == len(columns) and all(
        (output_root / factor_name / column / "overview.png").is_file()
        and (output_root / factor_name / column / "overview.png").stat().st_size > 0
        for column in columns)
    updated = set_acceptance(updated, 7, complete)
    if updated == original:
        return False
    _write_atomic(readme_path, updated)
    return True
