"""Check existing state and derive a package update plan without running tasks."""
from __future__ import annotations

from pathlib import Path

from factor_protocol import FactorStorage, resolve_factor_columns
from factor_protocol.fingerprint import factor_definition_fingerprint
from factor_protocol.identity import read_identity, stored_identity_matches
from factor_protocol.loader import _project_path, load_module_from_path, resolve_factor_package

from .models import UpdateConfig, UpdatePlan, UpdateResult
from .preflight import _changed_source_dates, _stored_factor_columns
from .sources import (_partition_dates, _update_source_dates, _update_source_factors,
                      _update_source_tables, resolve_update_calendar)
from .state import _refresh_failure_marker, _validation_artifact_state


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
