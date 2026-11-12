"""Inspect factor history, source calendars and validation artifacts to derive an update plan."""
from __future__ import annotations

import json
import re
import uuid
from datetime import date
from factor_protocol import FactorStorage, resolve_factor_columns, resolve_factor_data_root
from factor_protocol.artifacts import dataset_manifest, manifests_match, resolve_output_root
from factor_protocol.fingerprint import factor_definition_fingerprint
from factor_protocol.identity import read_identity, stored_identity_matches
from factor_protocol.loader import _project_path, load_module_from_path, resolve_factor_package
from pathlib import Path
from typing import Any, Sequence

from .config import UpdateConfig, UpdatePlan, UpdateResult


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


def _ensure_factor_output_writable(storage: FactorStorage) -> None:
    """Fail before expensive SQL when the formal factor root is read-only.

    Windows ACL failures otherwise appear only after a complete staging
    calculation, when the workflow tries to replace ``date=...``.  A private
    empty-directory probe exercises the same factor-root create/delete
    capability without touching a materialized partition.  The probe is used
    only on real update paths; dry-runs remain read-only.
    """

    factor_root = storage.factor_root
    if not factor_root.is_dir():
        return
    probe = factor_root / f".factor_update_write_probe-{uuid.uuid4().hex}"
    created = False
    try:
        probe.mkdir()
        created = True
        probe.rmdir()
        created = False
    except Exception as exc:  # noqa: BLE001 - preserve the ACL/error detail.
        if created:
            try:
                probe.rmdir()
            except OSError:
                pass
        raise PermissionError(
            f"factor output directory is not writable: {factor_root.as_posix()}"
        ) from exc


def _changed_source_dates(
    storage: FactorStorage,
    *,
    source_dates: Sequence[str],
    source_tables: Sequence[str],
    source_factors: Sequence[str] = (),
    lookback: int = 400,
) -> list[str]:
    """Compare recent source partitions with their existing factor outputs."""
    changed: set[str] = set()
    datasets = tuple(dict.fromkeys((*source_tables, *source_factors)))
    candidate_dates = sorted(set(source_dates))[-max(int(lookback), 1) :]
    for value in candidate_dates:
        output_mtime = _partition_mtime_ns(storage.factor_root / f"date={value}")
        if output_mtime and any(
            _partition_mtime_ns(storage.table_root(dataset) / f"date={value}") > output_mtime
            for dataset in datasets
        ):
            changed.add(value)
    return sorted(changed)


def _stored_factor_columns(storage: FactorStorage) -> tuple[str, ...]:
    """Check the first, middle and latest output schemas before updating."""

    partitions = _partition_dates(storage.factor_root)
    if not partitions:
        return ()
    sample_indices = sorted({0, len(partitions) // 2, len(partitions) - 1})
    columns: set[str] = set()
    for index in sample_indices:
        source = storage.partition_scan_sql(
            storage.factor_name,
            begin=partitions[index],
            end=partitions[index],
        )
        rows = storage.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()
        columns.update(
            str(row[0]) for row in rows if str(row[0]) not in {"code", "date", "time"}
        )
    return tuple(sorted(columns))


def _refresh_failure_marker(project_root: Path, factor_name: str) -> Path:
    return project_root / ".tmp" / "factor_update" / factor_name / "refresh_failed.json"


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


def plan_update(cfg: UpdateConfig) -> UpdatePlan | UpdateResult:
    root = Path(cfg.project_root)
    package = resolve_factor_package(cfg.factor_name, project_root=root,
                                     factor_module=cfg.factor_module, factor_zoo_root=cfg.factor_zoo_root)
    module = load_module_from_path(package.module_path, project_root=root, factor_name=cfg.factor_name)
    columns = tuple(resolve_factor_columns(module))
    if not callable(getattr(module, "calculate_factor", None)):
        raise ValueError("factor module must define callable calculate_factor")
    factor_name = cfg.storage_factor_name or cfg.factor_name
    temp = (_project_path(cfg.temp_directory, project_root=root) / "preflight"
            if cfg.temp_directory is not None
            else root / ".tmp" / "factor_update" / factor_name / "preflight")
    if cfg.dry_run:
        temp = root
    storage = FactorStorage(project_root=root, data_root=cfg.data_root, factor_name=factor_name,
                            threads=cfg.threads, memory_limit=cfg.memory_limit, temp_directory=temp,
                            duckdb_settings={"temp_directory": ""} if cfg.dry_run else None)
    try:
        calendar = resolve_update_calendar(module)
        source_tables = _update_source_tables(module, calendar=calendar)
        source_factors = _update_source_factors(module)
        dates = _update_source_dates(storage, calendar=calendar, module=module, source_factors=source_factors)
        if not dates:
            return UpdateResult(cfg.factor_name, "failed", message=f"{calendar} has no date partitions")
        end = dates[-1]
        identity = read_identity(storage.factor_root)
        manifest = identity.get("manifest", {})
        output = set(_partition_dates(storage.factor_root))
        output.update(day for day in (manifest.get("begin"), manifest.get("end")) if day)
        output = sorted(output)
        before_end = output[-1] if output else None
        if output:
            if set(_stored_factor_columns(storage)) != set(columns):
                return UpdateResult(cfg.factor_name, "schema_mismatch", end=end, before_end=before_end,
                                    after_end=before_end, message="stored factor schema differs; full rebuild required")
            matches = stored_identity_matches(storage, fingerprint=factor_definition_fingerprint(package))
            if (not matches or identity.get("factor_name") != cfg.factor_name
                    or set(identity.get("columns", ())) != set(columns)):
                return UpdateResult(cfg.factor_name, "rebuild_required", begin=output[0], end=end,
                                    before_end=before_end, after_end=before_end,
                                    message="factor data identity is missing or changed; run factor-calculate full-history workflow")
            detect_rewrites = (hasattr(module, "UPDATE_SOURCE_TABLES")
                               or calendar == "quotes_common" or bool(source_factors))
            changed = (_changed_source_dates(storage, source_dates=dates, source_tables=source_tables,
                                             source_factors=source_factors) if detect_rewrites else [])
            if changed:
                return UpdateResult(cfg.factor_name, "rebuild_required", begin=changed[0], end=end,
                                    before_end=before_end, after_end=before_end,
                                    message="materialized source partitions were rewritten at " + ",".join(changed)
                                    + "; run factor-calculate full-history workflow")
            pending = [day for day in dates if day > before_end]
            if not pending:
                return _plan_refresh(cfg, end=end, before_end=before_end, columns=columns)
            begin = pending[0]
        else:
            if cfg.bootstrap_begin is None:
                return UpdateResult(cfg.factor_name, "uninitialized", end=end,
                                    message="factor data is absent; run factor-calculate for initial materialization")
            begin = cfg.bootstrap_begin
        if begin > end:
            return UpdateResult(cfg.factor_name, "up_to_date", begin=begin, end=end,
                                before_end=before_end, after_end=before_end)
        if cfg.dry_run:
            return UpdateResult(cfg.factor_name, "planned", begin=begin, end=end,
                                before_end=before_end, after_end=before_end,
                                compute_batch_days=cfg.compute_batch_days)
        return UpdatePlan(cfg.factor_name, begin, end, before_end, True, cfg.refresh_validation)
    finally:
        storage.close()


def _plan_refresh(cfg: UpdateConfig, *, end: str, before_end: str,
                  columns: tuple[str, ...]) -> UpdatePlan | UpdateResult:
    state = _validation_artifact_state(project_root=Path(cfg.project_root), output_root=cfg.factor_output_root,
                                      factor_name=cfg.factor_name, factor_columns=columns,
                                      source_end=before_end, storage_factor_name=cfg.storage_factor_name)
    status = state[0] if state else "up_to_date"
    details = state[1] if state else ()
    if state is None and _refresh_failure_marker(Path(cfg.project_root), cfg.factor_name).exists():
        status = "evaluation_stale"
    blocked = state is not None and not cfg.refresh_validation
    if cfg.dry_run or blocked or not (state or cfg.refresh_evaluation):
        return UpdateResult(cfg.factor_name, status, begin=before_end, end=end, before_end=before_end,
                            after_end=before_end, message="; ".join(details))
    return UpdatePlan(cfg.factor_name, before_end, end, before_end, False, state is not None)
