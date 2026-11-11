"""Inspect validation handoffs and persist refresh failure markers."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Sequence
from factor_protocol import resolve_factor_data_root
from factor_protocol.artifacts import dataset_manifest, manifests_match, resolve_output_root
from factor_protocol.identity import read_identity

def _refresh_failure_marker(project_root: Path, factor_name: str) -> Path:
    return project_root / ".tmp" / "factor_update" / factor_name / "refresh_failed.json"


def _write_refresh_failure_marker(
    project_root: Path,
    factor_name: str,
    *,
    end: str,
    messages: Sequence[str],
) -> None:
    marker = _refresh_failure_marker(project_root, factor_name)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {"factor_name": factor_name, "end": end, "messages": list(messages)},
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )


def _clear_refresh_failure_marker(project_root: Path, factor_name: str) -> None:
    marker = _refresh_failure_marker(project_root, factor_name)
    if marker.exists():
        marker.unlink()


def _validation_artifact_state(
    *,
    project_root: Path,
    output_root: str | Path,
    factor_name: str,
    factor_columns: Sequence[str],
    source_end: str,
    storage_factor_name: str | None = None,
) -> tuple[str, tuple[str, ...]] | None:
    """Check the package's validation handoff artifacts."""

    root = resolve_output_root(output_root, project_root=project_root) / factor_name
    factor_source = storage_factor_name or factor_name
    factor_data = resolve_factor_data_root(project_root=project_root)
    fingerprint = read_identity(factor_data / factor_source).get("fingerprint")
    cache: dict[tuple[str, str], dict[str, Any]] = {}

    def snapshot_current(stored: Any, table: str) -> bool:
        if not isinstance(stored, dict) or not isinstance(stored.get("through"), str):
            return False
        key = (table, stored["through"])
        if key not in cache:
            cache[key] = dataset_manifest(factor_data, table, through=stored["through"])
        return manifests_match(stored, cache[key])

    states: list[tuple[str, str]] = []
    for column in factor_columns:
        path = root / column / "config.json"
        if not path.is_file():
            states.append((column, "missing_config"))
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            states.append((column, "invalid_config"))
            continue
        if not isinstance(payload, dict):
            states.append((column, "invalid_config"))
            continue
        factor = payload.get("factor", {})
        if (
            not isinstance(factor, dict)
            or factor.get("name") != factor_name
            or factor.get("column") != column
            or factor.get("dataset", factor_name) != factor_source
            or not factor.get("fingerprint")
            or factor.get("fingerprint") != fingerprint
        ):
            states.append((column, "factor_identity_mismatch"))
            continue
        validation = payload.get("validation")
        if not isinstance(validation, dict):
            states.append((column, "missing_validation"))
            continue
        status = str(validation.get("status") or "unknown")
        end = str(validation.get("end") or "")
        audit_path = path.parent / "audit.csv"
        if not audit_path.is_file() or audit_path.stat().st_size == 0:
            states.append((column, "missing_audit"))
        if status != "pass":
            states.append((column, f"status={status}"))
        if not end:
            states.append((column, "missing_validation_end"))
        elif end < source_end:
            states.append((column, f"stale_end={end}"))
        if not snapshot_current(validation.get("data"), factor_source):
            states.append((column, "stale_data"))
    if not states:
        return None
    if any(
        not (detail.startswith("stale_end=") or detail == "stale_data")
        for _, detail in states
    ):
        state = "validation_failed"
    else:
        state = "validation_stale"
    details = tuple(f"{column}:{detail}" for column, detail in states)
    return state, details
