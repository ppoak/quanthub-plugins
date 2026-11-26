"""Verify evaluation metadata and metric files."""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from factor_protocol.artifacts import read_config

EVALUATION_FILES = {
    "ic": "ic.csv",
    "returns": "returns.csv",
    "correlation": "correlation.csv",
}
PORTFOLIO_ACCOUNTING = "continuous_v2"
MISSING_SIGNAL_POLICIES = ("hold", "liquidate")
PARAMETERS = {
    "begin", "train_end", "validation_end", "horizon_days", "execution_lag_days",
    "price_col", "quantiles", "top_percentile", "rolling_mean_days", "industry_level",
    "direction", "reference_factors",
}


def evaluation_config(config: dict[str, Any]) -> dict[str, Any]:
    """Read current and legacy metric bundles without copying factor identity."""
    old = config.get("evaluation")
    if isinstance(old, Mapping) and old.get("status") == "complete" and all(
        key in old for key in ("files", "parameters", "begin", "completed_at")
    ):
        parameters = {**old["parameters"], "begin": old["begin"]}
        parameters.setdefault("direction", "auto")
        config = {
            **{key: config[key] for key in ("factor", "validation") if key in config},
            "parameters": {key: value for key, value in parameters.items() if key in PARAMETERS},
            "files": old["files"],
            "completed_at": old["completed_at"],
        }
    factor = config.get("factor")
    if not isinstance(factor, Mapping):
        return config
    result = {**config, "factor": {key: factor[key] for key in ("name", "column") if key in factor}}
    alternate_input = factor.get("path") or factor.get("dataset")
    if alternate_input and alternate_input != factor.get("name"):
        result.setdefault("input", alternate_input)
    if config.get("files") and set(config["files"]) != set(EVALUATION_FILES.values()) and factor.get("through"):
        result.setdefault("partial_end", factor["through"])
    return result


def evaluation_files(output_dir: Path) -> dict[str, str]:
    """Identify canonical metric files by their contents."""
    result = {}
    for filename in EVALUATION_FILES.values():
        if not (output_dir / filename).is_file():
            continue
        with (output_dir / filename).open("rb") as stream:
            result[filename] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def evaluation_files_match(output_dir: Path, evaluation: Mapping[str, Any]) -> bool:
    expected = evaluation.get("files")
    if not isinstance(expected, Mapping) or set(expected) != set(
        EVALUATION_FILES.values()
    ):
        return False
    try:
        return evaluation_files(output_dir) == expected
    except OSError:
        return False


def check_evaluation_config(
    column_dir: Path,
    *,
    factor_name: str | None = None,
    project_root: str | Path,
    factor_source: str | None = None,
    factor_data: str | Path | None = None,
) -> dict[str, Any]:
    """Require a consistent, complete metric bundle."""
    config = evaluation_config(read_config(column_dir / "config.json"))
    factor = config.get("factor")
    if not isinstance(factor, Mapping) or (
        factor.get("name") != factor_name or factor.get("column") != column_dir.name
    ):
        raise ValueError(
            f"evaluation config identifies another factor output: {column_dir}"
        )
    if factor_data is not None:
        expected_input = (Path(project_root) / factor_data).resolve().as_posix()
    else:
        expected_input = factor_source if factor_source != factor_name else None
    if (factor_data is not None or factor_source is not None) and config.get("input") != expected_input:
        raise ValueError(f"evaluation config identifies another input: {column_dir}")
    if not config.get("files"):
        raise ValueError(f"complete evaluation is required: {column_dir}")
    parameters = config.get("parameters")
    if not isinstance(parameters, Mapping) or set(parameters) - {"portfolio_accounting", "missing_signal"} != PARAMETERS:
        raise ValueError(f"evaluation parameters are missing: {column_dir}")
    if parameters.get("portfolio_accounting") not in {None, "continuous", PORTFOLIO_ACCOUNTING}:
        raise ValueError(f"unknown portfolio accounting: {column_dir}")
    if parameters.get("missing_signal", "hold") not in MISSING_SIGNAL_POLICIES:
        raise ValueError(f"invalid missing signal policy: {column_dir}")
    if parameters["direction"] not in {"positive", "negative", "auto"}:
        raise ValueError(f"invalid evaluation direction: {column_dir}")
    try:
        begin = date.fromisoformat(parameters["begin"])
        train_end = date.fromisoformat(parameters["train_end"])
        validation_end = date.fromisoformat(parameters["validation_end"])
        datetime.fromisoformat(config["completed_at"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"evaluation dates are incomplete: {column_dir}") from exc
    if not begin <= train_end < validation_end:
        raise ValueError(f"evaluation periods are inconsistent: {column_dir}")
    if not evaluation_files_match(column_dir, config):
        raise ValueError(f"evaluation metric changed or is missing: {column_dir}")

    return config
