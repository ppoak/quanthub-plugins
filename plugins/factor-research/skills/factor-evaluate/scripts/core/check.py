"""Verify canonical evaluation metadata, input snapshots and metric files."""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from factor_protocol import resolve_factor_data_root
from factor_protocol.artifacts import dataset_manifest, manifests_match, read_config
from factor_protocol.identity import read_identity

EVALUATION_FILES = {
    "ic": "ic.csv",
    "returns": "returns.csv",
    "correlation": "correlation.csv",
}
PARAMETERS = {
    "begin", "train_end", "validation_end", "horizon_days", "execution_lag_days",
    "price_col", "quantiles", "top_percentile", "rolling_mean_days", "industry_level",
    "direction", "reference_factors",
}


def factor_snapshot(root: Path, *, through: str) -> dict[str, str]:
    """Use a committed snapshot when available; support bounded research runs."""
    manifest = read_identity(root).get("manifest", {})
    if manifest.get("end") == through and manifest.get("sha256"):
        return {"through": through, "sha256": manifest["sha256"]}
    snapshot = dataset_manifest(root.parent, root.name, through=through)
    if snapshot["maximum_date"] is None:
        raise ValueError(f"evaluation input is empty: {root}")
    return {"through": through, "sha256": snapshot["sha256"]}


def evaluation_config(config: dict[str, Any]) -> dict[str, Any]:
    """Read complete legacy metadata using the snapshot it actually evaluated."""
    old = config.get("evaluation")
    if (not isinstance(old, Mapping) or old.get("status") != "complete"
            or not all(key in old for key in ("files", "parameters", "begin", "completed_at"))):
        return config
    snapshot = old.get("source_manifests", {}).get("factor")
    if not isinstance(snapshot, Mapping) or not all(snapshot.get(key) for key in ("through", "sha256")):
        return config
    factor = {**config["factor"], "through": snapshot["through"], "sha256": snapshot["sha256"]}
    parameters = {**old["parameters"], "begin": old["begin"]}
    parameters.setdefault("direction", "auto")
    compact = {
        "factor": factor,
        "parameters": {key: value for key, value in parameters.items() if key in PARAMETERS},
        "files": old["files"],
        "completed_at": old["completed_at"],
    }
    validation = config.get("validation")
    if isinstance(validation, Mapping) and "status" not in validation:
        compact["validation"] = validation
    elif isinstance(validation, Mapping) and validation.get("status") == "pass":
        validated = validation.get("data")
        if isinstance(validated, Mapping) and factor.get("fingerprint") and not manifests_match(factor, validated):
            compact["validation"] = {
                "fingerprint": factor["fingerprint"],
                "through": validated.get("through"),
                "sha256": validated.get("sha256"),
            }
    elif validation is not None:
        compact["validation"] = "fail"
    return compact


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
    verify_sources: bool = True,
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
    dataset = str(factor.get("dataset", factor_name or "_factor_data"))
    if factor_source is not None and dataset != factor_source:
        raise ValueError(f"evaluation config identifies another dataset: {column_dir}")
    stored_path = factor.get("path")
    if factor_data is not None:
        path = Path(factor_data)
        expected = (Path(project_root) / path).resolve().as_posix()
        if stored_path != expected:
            raise ValueError(f"evaluation config identifies another input directory: {column_dir}")
    if not config.get("files"):
        raise ValueError(f"complete evaluation is required: {column_dir}")
    if not isinstance(factor.get("sha256"), str) or len(factor["sha256"]) != 64:
        raise ValueError(f"evaluation factor snapshot is missing: {column_dir}")
    parameters = config.get("parameters")
    if not isinstance(parameters, Mapping) or set(parameters) != PARAMETERS:
        raise ValueError(f"evaluation parameters are missing: {column_dir}")
    if parameters["direction"] not in {"positive", "negative", "auto"}:
        raise ValueError(f"invalid evaluation direction: {column_dir}")
    try:
        begin = date.fromisoformat(parameters["begin"])
        through = date.fromisoformat(factor["through"])
        train_end = date.fromisoformat(parameters["train_end"])
        validation_end = date.fromisoformat(parameters["validation_end"])
        datetime.fromisoformat(config["completed_at"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"evaluation dates are incomplete: {column_dir}") from exc
    if not begin <= train_end < validation_end < through:
        raise ValueError(f"evaluation periods are inconsistent: {column_dir}")
    if end is not None and factor["through"] != end:
        raise ValueError(f"evaluation does not cover the requested end: {column_dir}")
    if not evaluation_files_match(column_dir, config):
        raise ValueError(f"evaluation metric changed or is missing: {column_dir}")

    if not verify_sources:
        return config

    root = (Path(stored_path) if stored_path
            else resolve_factor_data_root(project_root=project_root) / dataset)
    cache = manifest_cache if manifest_cache is not None else {}
    key = (root.as_posix(), factor["through"])
    if key not in cache:
        cache[key] = factor_snapshot(root, through=factor["through"])
    if not manifests_match(factor, cache[key]):
        raise ValueError(f"evaluated factor data changed: {column_dir}")
    return config
