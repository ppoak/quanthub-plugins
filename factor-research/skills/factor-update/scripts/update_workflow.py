from __future__ import annotations

import gc
import json
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, Sequence

from factor_protocol import (
    FactorStorage,
    resolve_data_root,
    resolve_factor_data_root,
    resolve_factor_columns,
    resolve_factor_zoo_root,
)
from factor_protocol.artifacts import (
    dataset_manifest,
    manifests_match,
    resolve_output_root,
)
from factor_protocol.fingerprint import (
    factor_definition_fingerprint,
)
from factor_protocol.identity import (
    read_identity,
    stored_identity_matches,
)
from factor_protocol.loader import (
    _project_path,
    load_module_from_path,
    resolve_factor_package,
)


def _run_skill_script(
    project_root: Path,
    skill_name: str,
    script_name: str,
    arguments: Sequence[str],
    *,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a terminal skill in its own process and release its working memory."""

    project_root = project_root.resolve()
    skill_root = Path(__file__).resolve().parents[2]
    script_path = skill_root / skill_name / "scripts" / script_name
    if not script_path.is_file():
        raise FileNotFoundError(f"skill script not found: {script_path.as_posix()}")
    command = [sys.executable, script_path.as_posix(), *arguments]
    if not capture_output:
        return subprocess.run(
            command,
            cwd=project_root,
            check=True,
            text=True,
        )

    process = subprocess.Popen(
        command,
        cwd=project_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    output: list[str] = []
    if process.stdout is not None:
        for line in process.stdout:
            output.append(line)
            sys.stdout.write(line)
            sys.stdout.flush()
        process.stdout.close()
    return_code = process.wait()
    completed_output = "".join(output)
    if return_code != 0:
        raise subprocess.CalledProcessError(
            return_code,
            command,
            output=completed_output,
        )
    return subprocess.CompletedProcess(
        command,
        return_code,
        stdout=completed_output,
        stderr="",
    )


def update_compute_batch_days(factor_name: str) -> int:
    """Return the bounded calculation batch used by production updates."""

    return (
        31
        if factor_name in {"technical_alphas", "historical_similar_price_pattern_factors"}
        else 20
    )


@dataclass(frozen=True)
class UpdateConfig:
    factor_name: str
    storage_factor_name: str | None = None
    factor_module: str | None = None
    data_root: str | Path | None = None
    factor_zoo_root: str | Path | None = None
    threads: int = 1
    bootstrap_begin: str | None = None
    dry_run: bool = False
    refresh_validation: bool = True
    refresh_evaluation: bool = True
    readme_update: bool = True
    factor_output_root: str = "artifacts/factor_zoo"
    memory_limit: str | None = "1GB"
    temp_directory: str | Path | None = None
    validation_memory_limit: str | None = "1GB"
    project_root: str | Path | None = None


@dataclass(frozen=True)
class UpdateResult:
    factor_name: str
    status: str
    begin: str | None = None
    end: str | None = None
    before_end: str | None = None
    after_end: str | None = None
    missing_dates: tuple[str, ...] = ()
    message: str = ""
    source_begin: str | None = None
    compute_batch_days: int | None = None


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


def _refresh_factor_validation(
    *,
    factor_name: str,
    begin: str,
    end: str,
    cfg: UpdateConfig,
    project_root: Path,
) -> str:
    """Run a bounded recent validation after a successful factor update."""

    data_root = resolve_data_root(
        project_root=project_root,
        data_root=cfg.data_root,
    )
    output_root = resolve_output_root(
        cfg.factor_output_root,
        project_root=project_root,
    )
    # Keep validation spill files on the caller-selected volume when one is
    # provided.  The default remains the historical per-factor workspace so
    # existing callers keep their layout.  Update runs can therefore move the
    # whole calculate/validate/evaluate refresh off a space-constrained D:\\
    # drive without changing the factor or artifact roots.
    temp_directory = (
        _project_path(cfg.temp_directory, project_root=project_root) / "validation"
        if cfg.temp_directory is not None
        else project_root / ".tmp" / "factor_validation" / factor_name
    )
    validation_args = [
        "--factor-name",
        factor_name,
        "--begin",
        begin,
        "--end",
        end,
        "--data-root",
        data_root.as_posix(),
        "--factor-zoo-root",
        cfg.factor_zoo_root,
        "--output-dir",
        output_root.as_posix(),
        "--threads",
        str(cfg.threads),
        "--temp-directory",
        temp_directory.as_posix(),
        "--project-root",
        project_root.as_posix(),
        "--no-readme-update",
    ]
    if cfg.validation_memory_limit:
        validation_args.extend(("--memory-limit", cfg.validation_memory_limit))
    if cfg.storage_factor_name:
        validation_args.extend(("--storage-factor-name", cfg.storage_factor_name))
    _run_skill_script(
        project_root,
        "factor-validate",
        "run_validate.py",
        validation_args,
    )
    return (output_root / factor_name).as_posix()


def _refresh_update_outputs(
    *,
    factor_name: str,
    evaluation_end: str,
    cfg: UpdateConfig,
    project_root: Path,
) -> tuple[list[str], bool]:
    """Validate materialized dates and refresh evaluation through its target date."""

    data_root = resolve_data_root(project_root=project_root, data_root=cfg.data_root)
    output_dates = _partition_dates(
        resolve_factor_data_root(project_root=project_root) / (cfg.storage_factor_name or factor_name)
    )
    if not output_dates:
        return [
            "calculation completed with no eligible rows; validation/evaluation await factor data"
        ], False
    messages: list[str] = []
    failed = False
    if cfg.refresh_validation:
        try:
            validation_path = _refresh_factor_validation(
                factor_name=factor_name,
                begin=output_dates[-5] if len(output_dates) >= 5 else output_dates[0],
                end=output_dates[-1],
                cfg=cfg,
                project_root=project_root,
            )
            messages.append(f"validation={validation_path}")
        except Exception as exc:  # noqa: BLE001 - refresh is a hard handoff gate.
            failed = True
            messages.append(f"validation_failed={type(exc).__name__}: {exc}")
    if cfg.refresh_evaluation and not failed:
        try:
            evaluation_temp_directory = (
                _project_path(cfg.temp_directory, project_root=project_root)
                / "evaluation"
                if cfg.temp_directory is not None
                else (
                    project_root
                    / ".tmp"
                    / "factor_update"
                    / (cfg.storage_factor_name or factor_name)
                    / "evaluation"
                )
            )
            evaluation_args = [
                "--refresh",
                "--factor-package",
                factor_name,
                "--project-root",
                project_root.as_posix(),
                "--data-root",
                resolve_data_root(
                    project_root=project_root,
                    data_root=cfg.data_root,
                ).as_posix(),
                "--factor-zoo-root",
                cfg.factor_zoo_root,
                "--output-dir",
                cfg.factor_output_root,
                "--threads",
                str(cfg.threads),
                "--memory-limit",
                cfg.memory_limit or "1GB",
                "--temp-directory",
                evaluation_temp_directory.as_posix(),
                "--end",
                evaluation_end,
            ]
            if cfg.storage_factor_name:
                evaluation_args.extend(
                    ("--storage-factor-name", cfg.storage_factor_name)
                )
            evaluation_args.extend(("--operation", "evaluate"))
            if cfg.readme_update:
                evaluation_args.extend(("--operation", "check", "--operation", "plot", "--operation", "readme"))
            _run_skill_script(
                project_root,
                "factor-evaluate",
                "run_evaluate.py",
                evaluation_args,
            )
            messages.append("evaluation_refresh=completed")
        except Exception as exc:  # noqa: BLE001 - refresh is a hard handoff gate.
            failed = True
            messages.append(f"evaluation_failed={type(exc).__name__}: {exc}")
    return messages, failed


def resolve_update_calendar(module: Any) -> str:
    """Return the declared upstream table or the common-table calendar."""
    return FactorStorage.identifier(getattr(module, "UPDATE_CALENDAR", "quotes_day"))


def run_update(cfg: UpdateConfig) -> UpdateResult:
    project_root = (
        Path(cfg.project_root) if cfg.project_root is not None else Path.cwd()
    ).resolve()
    cfg = replace(
        cfg,
        data_root=resolve_data_root(
            project_root=project_root, data_root=cfg.data_root
        ).as_posix(),
        factor_zoo_root=resolve_factor_zoo_root(
            project_root=project_root, factor_zoo_root=cfg.factor_zoo_root
        ).as_posix(),
    )
    update_batch_days: int | None = None
    calculation_source_begin: str | None = None
    try:
        package = resolve_factor_package(
            cfg.factor_name,
            project_root=project_root,
            factor_module=cfg.factor_module,
            factor_zoo_root=cfg.factor_zoo_root,
        )
        module = load_module_from_path(
            package.module_path,
            project_root=project_root,
            factor_name=cfg.factor_name,
        )
        current_columns = tuple(resolve_factor_columns(module))
        calculate = getattr(module, "calculate_factor", None)
        if not callable(calculate):
            raise ValueError("factor module must define callable calculate_factor")
    except Exception as exc:  # noqa: BLE001 - update-all reports each package independently.
        return UpdateResult(
            factor_name=cfg.factor_name,
            status="failed",
            message=f"{type(exc).__name__}: {exc}",
        )

    storage_factor_name = cfg.storage_factor_name or cfg.factor_name
    data_root = resolve_data_root(
        project_root=project_root,
        data_root=cfg.data_root,
    )
    preflight_temp = (
        _project_path(cfg.temp_directory, project_root=project_root) / "preflight"
        if cfg.temp_directory is not None
        else project_root / ".tmp" / "factor_update" / storage_factor_name / "preflight"
    )
    storage = FactorStorage(
        project_root=project_root,
        data_root=data_root,
        factor_name=storage_factor_name,
        threads=cfg.threads,
        memory_limit=cfg.memory_limit,
        temp_directory=preflight_temp,
    )
    try:
        if not cfg.dry_run and any(
            (storage.factor_data_root / name).exists()
            for name in (
                "_materialize_" + storage_factor_name,
                "." + storage_factor_name + ".previous",
            )
        ):
            _run_skill_script(
                project_root,
                "factor-calculate",
                "run_calculate.py",
                [
                    "--factor-name",
                    cfg.factor_name,
                    "--storage-factor-name",
                    storage_factor_name,
                    "--project-root",
                    project_root.as_posix(),
                    "--data-root",
                    data_root.as_posix(),
                    "--recover-only",
                ],
            )
        definition_fingerprint = factor_definition_fingerprint(package)
        identity = read_identity(storage.factor_root)
        stored_identity_match = stored_identity_matches(
            storage, fingerprint=definition_fingerprint
        )
        if identity:
            stored_identity_match = (
                stored_identity_match
                and identity.get("factor_name") == cfg.factor_name
                and set(identity.get("columns", ())) == set(current_columns)
            )
        update_calendar = resolve_update_calendar(module)
        source_tables = _update_source_tables(module, calendar=update_calendar)
        source_factors = _update_source_factors(module)
        source_dates = _update_source_dates(
            storage,
            calendar=update_calendar,
            module=module,
            source_factors=source_factors,
        )
        if not source_dates:
            return UpdateResult(
                factor_name=cfg.factor_name,
                status="failed",
                message=f"{update_calendar} has no date partitions",
            )
        stored_manifest = identity.get("manifest", {})
        output_dates = set(_partition_dates(storage.factor_root))
        output_dates.update(
            day for day in (stored_manifest.get("begin"), stored_manifest.get("end")) if day
        )
        output_dates = sorted(output_dates)
        factor_range = (output_dates[0], output_dates[-1]) if output_dates else None
        source_end = source_dates[-1]
        before_end = factor_range[1] if factor_range else None

        definition_unknown = factor_range is not None and stored_identity_match is None
        definition_changed = factor_range is not None and (
            stored_identity_match is False or definition_unknown
        )
        if factor_range is not None:
            try:
                stored_columns = _stored_factor_columns(storage)
            except Exception as exc:
                return UpdateResult(
                    factor_name=cfg.factor_name,
                    status="failed",
                    before_end=before_end,
                    end=source_end,
                    message=f"factor schema inspection failed: {type(exc).__name__}: {exc}",
                )
            if set(stored_columns) != set(current_columns):
                return UpdateResult(
                    factor_name=cfg.factor_name,
                    status="schema_mismatch",
                    before_end=before_end,
                    after_end=before_end,
                    end=source_end,
                    message="stored factor schema differs from the current definition; full rebuild required",
                )

        if definition_changed:
            return UpdateResult(
                factor_name=cfg.factor_name,
                status="rebuild_required",
                begin=factor_range[0] if factor_range else None,
                end=source_end,
                before_end=before_end,
                after_end=before_end,
                message=(
                    "factor data identity is missing or changed; "
                    "factor_update is incremental-only; run the explicit "
                    "factor-calculate full-history workflow before enabling "
                    "nightly updates"
                ),
            )

        detect_source_rewrites = (
            hasattr(module, "UPDATE_SOURCE_TABLES")
            or update_calendar == "quotes_common"
            or bool(source_factors)
        )
        changed_source_dates = (
            _changed_source_dates(
                storage,
                source_dates=source_dates,
                source_tables=source_tables,
                source_factors=source_factors,
            )
            if factor_range is not None and detect_source_rewrites
            else []
        )
        if changed_source_dates:
            return UpdateResult(
                factor_name=cfg.factor_name,
                status="rebuild_required",
                begin=changed_source_dates[0],
                end=source_end,
                before_end=before_end,
                after_end=before_end,
                message=(
                    "materialized source partitions were rewritten at "
                    + ",".join(changed_source_dates)
                    + "; factor_update is incremental-only; run the explicit "
                    "factor-calculate full-history workflow before enabling "
                    "nightly updates"
                ),
            )

        if factor_range is None:
            if cfg.bootstrap_begin is None:
                return UpdateResult(
                    factor_name=cfg.factor_name,
                    status="uninitialized",
                    end=source_end,
                    message="factor data is absent; pass --bootstrap-begin for initial materialization",
                )
            begin = cfg.bootstrap_begin
        else:
            pending_dates = [value for value in source_dates if value > factor_range[1]]
            refresh_retry = _refresh_failure_marker(
                project_root, cfg.factor_name
            ).exists()
            if not pending_dates:
                artifact_state = _validation_artifact_state(
                    project_root=project_root,
                    output_root=resolve_output_root(
                        cfg.factor_output_root, project_root=project_root
                    ),
                    factor_name=cfg.factor_name,
                    factor_columns=current_columns,
                    source_end=factor_range[1],
                    storage_factor_name=cfg.storage_factor_name,
                )
                state = artifact_state[0] if artifact_state else "up_to_date"
                details = artifact_state[1] if artifact_state else ()
                if refresh_retry and artifact_state is None:
                    state = "evaluation_stale"
                blocked = artifact_state is not None and not cfg.refresh_validation
                if (
                    cfg.dry_run
                    or blocked
                    or not (artifact_state or cfg.refresh_evaluation)
                ):
                    return UpdateResult(
                        factor_name=cfg.factor_name,
                        status=state,
                        begin=factor_range[1],
                        end=source_end,
                        before_end=before_end,
                        after_end=before_end,
                        message="; ".join(details),
                    )
                messages, failed = _refresh_update_outputs(
                    factor_name=cfg.factor_name,
                    evaluation_end=source_end,
                    cfg=replace(cfg, refresh_validation=artifact_state is not None),
                    project_root=project_root,
                )
                if failed:
                    _write_refresh_failure_marker(
                        project_root, cfg.factor_name, end=source_end, messages=messages
                    )
                else:
                    _clear_refresh_failure_marker(project_root, cfg.factor_name)
                return UpdateResult(
                    factor_name=cfg.factor_name,
                    status="failed" if failed else "up_to_date",
                    begin=factor_range[1],
                    end=source_end,
                    before_end=before_end,
                    after_end=before_end,
                    message="; ".join(messages),
                )
            begin = pending_dates[0]

        if begin > source_end:
            return UpdateResult(
                factor_name=cfg.factor_name,
                status="up_to_date",
                begin=begin,
                end=source_end,
                before_end=before_end,
                after_end=before_end,
            )
        if cfg.dry_run:
            return UpdateResult(
                factor_name=cfg.factor_name,
                status="planned",
                begin=begin,
                end=source_end,
                before_end=before_end,
                after_end=before_end,
            )
        # Check formal output permissions before launching a potentially
        # multi-hour staging calculation.  The update writer replaces a date
        # directory only after SQL completes; a read-only factor root would
        # otherwise waste the entire calculation and fail at the final move.
        try:
            _ensure_factor_output_writable(storage)
        except Exception as exc:  # noqa: BLE001 - fail before expensive SQL.
            return UpdateResult(
                factor_name=cfg.factor_name,
                status="failed",
                begin=begin,
                end=source_end,
                before_end=before_end,
                after_end=before_end,
                message=f"factor output preflight failed: {type(exc).__name__}: {exc}",
            )
    finally:
        storage.close()

    try:
        # Technical Alpha recomputes a 19-trading-day minute warm-up for each
        # output batch. HSP also benefits from a 31-trading-day batch after
        # its source staging/code-chunk optimization; both remain bounded and
        # do not change their rolling-window definitions. Keep the generic
        # 20-day default for other factors.
        update_batch_days = update_compute_batch_days(cfg.factor_name)
        calculation_temp_directory = (
            _project_path(cfg.temp_directory, project_root=project_root)
            if cfg.temp_directory is not None
            else project_root / ".tmp" / "factor_update" / cfg.factor_name
        )
        calculation_args = [
            "--factor-name",
            cfg.factor_name,
            "--begin",
            begin,
            "--end",
            source_end,
            "--data-root",
            data_root.as_posix(),
            "--factor-zoo-root",
            cfg.factor_zoo_root,
            "--threads",
            str(cfg.threads),
            "--compute-batch-days",
            str(update_batch_days),
            "--temp-directory",
            calculation_temp_directory.as_posix(),
            "--project-root",
            project_root.as_posix(),
            "--require-values",
            "--no-readme-update",
        ]
        if cfg.memory_limit:
            calculation_args.extend(("--memory-limit", cfg.memory_limit))
        if cfg.storage_factor_name:
            calculation_args.extend(("--storage-factor-name", cfg.storage_factor_name))
        if cfg.factor_module:
            calculation_args.extend(("--factor-module", cfg.factor_module))
        calculation = _run_skill_script(
            project_root,
            "factor-calculate",
            "run_calculate.py",
            calculation_args,
            capture_output=True,
        )
        source_begins = sorted(
            set(
                re.findall(
                    r"\bsource_begin\s*=\s*(\d{4}-\d{2}-\d{2})",
                    calculation.stdout or "",
                )
            )
        )
        if source_begins:
            calculation_source_begin = source_begins[0]
        messages, failed = _refresh_update_outputs(
            factor_name=cfg.factor_name,
            evaluation_end=source_end,
            cfg=cfg,
            project_root=project_root,
        )
        if failed:
            _write_refresh_failure_marker(
                project_root, cfg.factor_name, end=source_end, messages=messages
            )
        else:
            _clear_refresh_failure_marker(project_root, cfg.factor_name)
        return UpdateResult(
            factor_name=cfg.factor_name,
            status="failed" if failed else "updated",
            begin=begin,
            end=source_end,
            before_end=before_end,
            after_end=source_end,
            source_begin=calculation_source_begin,
            compute_batch_days=update_batch_days,
            message="; ".join(messages),
        )
    except Exception as exc:  # noqa: BLE001 - update-all must report each package independently.
        return UpdateResult(
            factor_name=cfg.factor_name,
            status="failed",
            begin=begin,
            end=source_end,
            before_end=before_end,
            message=f"{type(exc).__name__}: {exc}",
        )


def run_updates(
    *,
    factor_names: Sequence[str],
    project_root: str | Path,
    data_root: str | Path | None = None,
    factor_zoo_root: str | Path | None = None,
    threads: int = 1,
    bootstrap_begin: str | None = None,
    dry_run: bool = False,
    refresh_validation: bool = True,
    refresh_evaluation: bool = True,
    validation_memory_limit: str | None = "1GB",
    memory_limit: str | None = "1GB",
    temp_directory: str | Path | None = None,
) -> list[UpdateResult]:
    results: list[UpdateResult] = []
    for name in factor_names:
        results.append(
            run_update(
                UpdateConfig(
                    factor_name=name,
                    data_root=data_root,
                    factor_zoo_root=factor_zoo_root,
                    threads=threads,
                    bootstrap_begin=bootstrap_begin,
                    dry_run=dry_run,
                    refresh_validation=refresh_validation,
                    refresh_evaluation=refresh_evaluation,
                    memory_limit=memory_limit,
                    temp_directory=temp_directory,
                    validation_memory_limit=validation_memory_limit,
                    project_root=project_root,
                )
            )
        )
        gc.collect()
    return results
