"""Verify canonical evaluation metadata, input snapshots and metric files."""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from factor_protocol import FactorPath, resolve_data_root, resolve_factor_data_root
from factor_protocol.artifacts import dataset_manifest, manifests_match, read_config

EVALUATION_FILES = {
    "ic": "ic.csv",
    "returns": "returns.csv",
    "correlation": "correlation.csv",
}


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
    data_root: str | Path,
    project_root: str | Path,
    factor_source: str | None = None,
    factor_data: str | Path | None = None,
    end: str | None = None,
    manifest_cache: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Require complete metrics from unchanged input snapshots."""
    config = read_config(column_dir / "config.json")
    factor = config.get("factor")
    if not isinstance(factor, Mapping) or (
        factor.get("name") != factor_name or factor.get("column") != column_dir.name
    ):
        raise ValueError(
            f"evaluation config identifies another factor output: {column_dir}"
        )
    dataset = str(factor.get("dataset", factor_name or "_factor_data"))
    if factor_source is not None and dataset != factor_source:
        raise ValueError(f"evaluation config identifies another dataset: {column_dir}")
    stored_path = factor.get("path")
    if factor_data is not None:
        path = Path(factor_data)
        expected = (Path(project_root) / path).resolve().as_posix()
        if stored_path != expected:
            raise ValueError(f"evaluation config identifies another input directory: {column_dir}")
    factor_root = resolve_factor_data_root(project_root=project_root)
    evaluation = config.get("evaluation")
    if not isinstance(evaluation, Mapping) or evaluation.get("status") != "complete":
        raise ValueError(f"complete evaluation is required: {column_dir}")
    parameters = evaluation.get("parameters")
    required_parameters = {
        "horizon_days",
        "execution_lag_days",
        "price_col",
        "quantiles",
        "top_percentile",
        "rolling_mean_days",
        "industry_level",
        "train_end",
        "validation_end",
        "reference_factors",
    }
    if not isinstance(parameters, Mapping) or not required_parameters.issubset(
        parameters
    ):
        raise ValueError(f"evaluation parameters are missing: {column_dir}")
    if parameters.get("direction", "auto") not in {"positive", "negative", "auto"}:
        raise ValueError(f"invalid evaluation direction: {column_dir}")
    try:
        begin = date.fromisoformat(evaluation["begin"])
        requested_end = date.fromisoformat(evaluation["requested_end"])
        train_end = date.fromisoformat(parameters["train_end"])
        validation_end = date.fromisoformat(parameters["validation_end"])
        datetime.fromisoformat(evaluation["completed_at"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"evaluation dates are incomplete: {column_dir}") from exc
    if not begin <= train_end < validation_end < requested_end:
        raise ValueError(f"evaluation periods are inconsistent: {column_dir}")
    if end is not None and evaluation["requested_end"] != end:
        raise ValueError(f"evaluation does not cover the requested end: {column_dir}")
    if not evaluation_files_match(column_dir, evaluation):
        raise ValueError(f"evaluation metric changed or is missing: {column_dir}")

    cache = manifest_cache if manifest_cache is not None else {}
    factor_sources = {dataset, *(FactorPath.parse(value).table for value in parameters["reference_factors"])}

    def current_manifest(source: str, through: str) -> dict[str, Any]:
        key = (source, through)
        if key not in cache:
            directory = (
                Path(stored_path) if source == dataset and stored_path
                else Path(factor_root if source in factor_sources else data_root) / source
            )
            cache[key] = dataset_manifest(directory.parent, directory.name, through=through)
        return cache[key]

    def require_snapshot(snapshot: Any, source: str) -> dict[str, Any]:
        if not isinstance(snapshot, Mapping) or not isinstance(
            snapshot.get("through"), str
        ):
            raise ValueError(
                f"evaluation input snapshot is missing: {column_dir}: {source}"
            )
        manifest = current_manifest(source, snapshot["through"])
        if not manifests_match(snapshot, manifest):
            raise ValueError(f"evaluation input changed: {column_dir}: {source}")
        return manifest

    sources = evaluation.get("source_manifests")
    if not isinstance(sources, Mapping):
        raise ValueError(f"evaluation input snapshots are missing: {column_dir}")
    factor_manifest = require_snapshot(sources.get("factor"), dataset)
    last_trading_date = current_manifest("quotes_day", evaluation["requested_end"])[
        "maximum_date"
    ]
    if factor_manifest["through"] != last_trading_date:
        raise ValueError(
            f"evaluation does not cover the last available trading date: {column_dir}"
        )
    factor_end = factor_manifest["maximum_date"]
    if not factor_end:
        raise ValueError(f"evaluation input is empty: {column_dir}")
    for source in ("quotes_day", "industry_mapping"):
        require_snapshot(sources.get(source), source)
    references = sources.get("references")
    expected = {
        FactorPath.parse(value).table
        for value in parameters.get("reference_factors", ())
    }
    if not isinstance(references, Mapping) or set(references) != expected:
        raise ValueError(f"evaluation reference snapshots are incomplete: {column_dir}")
    for source, snapshot in references.items():
        require_snapshot(snapshot, source)
    return config
