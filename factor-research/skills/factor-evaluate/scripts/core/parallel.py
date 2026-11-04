"""Memory-bounded process parallelism for one full factor-column rebuild.

The parent process writes shared DuckDB-produced factor and return panels.  One
worker computes IC and portfolio metrics while the remaining workers compute
disjoint reference-correlation groups.  Workers only exchange Parquet shards;
Python never receives metric rows.
"""

from __future__ import annotations

import multiprocessing
import queue
import shutil
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Sequence

from .engine import _EvaluationEngine
from factor_protocol.artifacts import (
    replace_column_directory,
)
from factor_protocol.storage import FactorStorage
from .shared import EvaluationConfig, _chunks, _ColumnState, _is_oom, _parse_memory_bytes

_MINIMUM_WORKER_MEMORY_BYTES = 256_000_000
_WORKER_MEMORY_FRACTION = 0.64
_MAX_WORKER_THREADS = 4
_REFERENCE_PREPARE_CONCURRENCY = 3


@dataclass(frozen=True)
class _ParallelJob:
    index: int
    kind: str
    reference_factors: tuple[str, ...] = ()


@dataclass(frozen=True)
class _WorkerLimit:
    memory_limit: str
    threads: int


@dataclass
class _PrelaunchedJobs:
    processes: tuple[multiprocessing.Process, ...]
    task_queues: dict[int, multiprocessing.queues.Queue]
    result_queue: multiprocessing.queues.Queue
    started: list[multiprocessing.Process]
    start_errors: list[BaseException]
    thread: threading.Thread | None = None
    initialized: bool = False


def _resolve_path(value: str | Path, *, project_root: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else project_root / path


def _new_storage(
    *,
    cfg: EvaluationConfig,
    project_root: Path,
    factor_source: str,
    temp_directory: Path,
) -> FactorStorage:
    return FactorStorage(
        project_root=project_root,
        data_root=_resolve_path(cfg.data_root, project_root=project_root),
        factor_name=factor_source,
        factor_root=cfg.factor_data,
        threads=cfg.threads,
        memory_limit=cfg.memory_limit,
        temp_directory=temp_directory,
        duckdb_settings={
            "preserve_insertion_order": False,
            "enable_progress_bar": False,
        },
    )


def _new_engine(
    *,
    storage: FactorStorage,
    cfg: EvaluationConfig,
    project_root: Path,
    factor_source: str,
    factor_columns: Sequence[str],
    reference_factors: Sequence[str],
) -> _EvaluationEngine:
    return _EvaluationEngine(
        storage=storage,
        cfg=cfg,
        project_root=project_root,
        factor_source=factor_source,
        factor_columns=factor_columns,
        reference_factors=reference_factors,
    )


def _prepare_engine(engine: _EvaluationEngine) -> tuple[dict[str, Any], list[str]]:
    engine._validate_quote_schema()
    engine._validate_industry_schema()
    manifest = engine._manifest(engine.factor_source, engine.end)
    engine._states = engine._column_states(manifest)
    engine._create_calendar()
    process_begin = max(
        min(state.cutoff for state in engine._states),
        engine.factor_range[0],
    )
    process_dates = [date for date in engine.signal_dates if date >= process_begin]
    return manifest, process_dates


def _prepare_worker_engine(
    engine: _EvaluationEngine,
    *,
    workspace: Path,
) -> None:
    """Prepare SQL-only worker state after the parent validation gate."""

    cutoff = max(engine.begin, engine.factor_range[0])
    engine._states = (
        _ColumnState(
            factor_column=engine.factor_columns[0],
            output_dir=workspace,
            config={},
            cutoff=cutoff,
            incremental=False,
        ),
    )
    engine._create_calendar()


def _prepare_candidate_panel(
    *,
    cfg: EvaluationConfig,
    project_root: Path,
    factor_source: str,
    factor_columns: tuple[str, ...],
    workspace: Path,
    on_factor_batch_ready: Callable[[], None] | None = None,
    on_candidate_panel_ready: Callable[[], None] | None = None,
) -> tuple[str, str, tuple[str, ...]]:
    started = time.perf_counter()
    preparation_cfg = _preparation_config(cfg)
    storage = _new_storage(
        cfg=preparation_cfg,
        project_root=project_root,
        factor_source=factor_source,
        temp_directory=workspace / "candidate_spill",
    )
    try:
        engine = _new_engine(
            storage=storage,
            cfg=preparation_cfg,
            project_root=project_root,
            factor_source=factor_source,
            factor_columns=factor_columns,
            reference_factors=(),
        )
        _manifest, process_dates = _prepare_engine(engine)
        if not process_dates:
            raise ValueError(
                "parallel evaluation range contains no materialized factor dates"
            )
        engine._prepare_shared_forward_returns(process_dates[0], process_dates[-1])
        engine._create_factor_batch(
            factor_columns,
            process_dates[0],
            process_dates[-1],
        )
        if on_factor_batch_ready is not None:
            on_factor_batch_ready()
        engine._prepare_factor_variants(
            factor_columns,
            process_begin=process_dates[0],
            process_end=process_dates[-1],
            materialize=False,
        )
        factor_column = storage.sql_literal(factor_columns[0])
        storage.execute(
            f"""
            CREATE TEMP VIEW _parallel_eligible AS
            SELECT
                variants.date,
                variants.code,
                {factor_column} AS factor_column,
                variants.variant AS variant,
                variants.factor_value,
                returns.portfolio_id,
                returns.gross_security_return
            FROM {engine._factor_variants_relation} variants
            JOIN {engine._forward_returns_relation} returns USING (date, code)
            WHERE isfinite(returns.gross_security_return)
              AND isfinite(variants.factor_value)
            """
        )
        ranked_query = engine._ranked_positions_query("_parallel_eligible")
        candidate_directory = workspace / "factor_candidates"
        destination = storage.sql_literal(candidate_directory.as_posix())
        storage.execute(
            f"""
            COPY (
                WITH ranked AS (
                    {ranked_query}
                )
                SELECT
                    YEAR(date) AS candidate_year,
                    date,
                    code,
                    variant,
                    factor_value,
                    factor_rank,
                    return_rank,
                    layer,
                    sample_count,
                    top_high_selected,
                    top_low_selected
                FROM ranked
            ) TO {destination} (
                FORMAT PARQUET,
                COMPRESSION 'snappy',
                ROW_GROUP_SIZE 122880,
                PARTITION_BY (candidate_year)
            )
            """
        )
        if on_candidate_panel_ready is not None:
            on_candidate_panel_ready()
        candidate_glob = candidate_directory.as_posix() + "/candidate_year=*/*.parquet"
        candidate_files = tuple(candidate_directory.rglob("*.parquet"))
        if not candidate_files:
            raise ValueError("parallel evaluation candidate panel is empty")
        returns_directory = workspace / "security_returns"
        storage.execute(
            f"""
            COPY (
                SELECT
                    YEAR(date) AS return_year,
                    date,
                    code,
                    portfolio_id,
                    gross_security_return
                FROM {engine._forward_returns_relation}
                WHERE isfinite(gross_security_return)
            ) TO {storage.sql_literal(returns_directory.as_posix())} (
                FORMAT PARQUET,
                COMPRESSION 'snappy',
                ROW_GROUP_SIZE 122880,
                PARTITION_BY (return_year)
            )
            """
        )
        returns_glob = returns_directory.as_posix() + "/return_year=*/*.parquet"
        returns_files = tuple(returns_directory.rglob("*.parquet"))
        if not returns_files:
            raise ValueError("parallel evaluation security-return panel is empty")
        return candidate_glob, returns_glob, tuple(process_dates)
    finally:
        storage.close()


def _reference_groups(
    references: Sequence[str],
    workers: int,
) -> tuple[tuple[str, ...], ...]:
    if not references:
        return ()
    group_count = min(max(workers, 1), len(references))
    quotient, remainder = divmod(len(references), group_count)
    groups: list[tuple[str, ...]] = []
    offset = 0
    for index in range(group_count):
        size = quotient + (1 if index < remainder else 0)
        groups.append(tuple(references[offset : offset + size]))
        offset += size
    return tuple(groups)


def _parallel_layout(
    cfg: EvaluationConfig,
    references: Sequence[str],
) -> tuple[tuple[_ParallelJob, ...], dict[int, _WorkerLimit]]:
    if not references:
        raise ValueError("intra-column parallel evaluation requires reference factors")
    requested = max(int(cfg.intra_column_workers), 2)
    total_memory = _parse_memory_bytes(cfg.memory_limit)
    if total_memory is None:
        raise ValueError(
            "intra-column parallel evaluation requires an explicit memory limit"
        )
    usable_memory = int(total_memory * _WORKER_MEMORY_FRACTION)
    if usable_memory < 2 * _MINIMUM_WORKER_MEMORY_BYTES:
        raise ValueError("intra-column parallel evaluation requires at least 512MB")
    memory_workers = max(
        2,
        usable_memory // _MINIMUM_WORKER_MEMORY_BYTES,
    )
    requested = min(requested, memory_workers)
    correlation_workers = min(requested - 1, len(references))
    groups = _reference_groups(references, correlation_workers)
    jobs = [_ParallelJob(index=0, kind="metrics")]
    jobs.extend(
        _ParallelJob(
            index=index + 1,
            kind="correlation",
            reference_factors=group,
        )
        for index, group in enumerate(groups)
    )
    worker_count = len(jobs)
    worker_bytes = max(
        _MINIMUM_WORKER_MEMORY_BYTES,
        usable_memory // worker_count,
    )
    requested_threads = max(int(cfg.threads), 1)
    metric_threads = min(
        _MAX_WORKER_THREADS,
        max(requested_threads - correlation_workers, 1),
    )
    correlation_threads = {job.index: 1 for job in jobs[1:]}
    remaining_threads = max(
        requested_threads - metric_threads - correlation_workers,
        0,
    )
    while remaining_threads:
        changed = False
        for job in jobs[1:]:
            if correlation_threads[job.index] >= _MAX_WORKER_THREADS:
                continue
            correlation_threads[job.index] += 1
            remaining_threads -= 1
            changed = True
            if not remaining_threads:
                break
        if not changed:
            break
    limits = {
        job.index: _WorkerLimit(
            memory_limit=f"{max(worker_bytes // 1_000_000, 1)}MB",
            threads=(
                metric_threads
                if job.kind == "metrics"
                else correlation_threads[job.index]
            ),
        )
        for job in jobs
    }
    return tuple(jobs), limits


def _worker_config(
    *,
    cfg: EvaluationConfig,
    job: _ParallelJob,
    workspace: Path,
    worker_limit: _WorkerLimit,
) -> EvaluationConfig:
    return replace(
        cfg,
        factor_columns=tuple(cfg.factor_columns or ()),
        reference_factors=job.reference_factors,
        threads=worker_limit.threads,
        memory_limit=worker_limit.memory_limit,
        temp_directory=workspace / f"worker_{job.index}_spill",
        date_batch_days=(
            cfg.date_batch_days or cfg.settings.single_column_date_batch_days
        ),
        column_batch_size=1,
        intra_column_workers=1,
    )


def _preparation_config(cfg: EvaluationConfig) -> EvaluationConfig:
    total_memory = _parse_memory_bytes(cfg.memory_limit)
    if total_memory is None:
        return cfg
    preparation_bytes = max(
        _MINIMUM_WORKER_MEMORY_BYTES,
        int(total_memory * 0.80),
    )
    return replace(
        cfg,
        memory_limit=f"{max(preparation_bytes // 1_000_000, 1)}MB",
    )


def _copy_table(storage: FactorStorage, table: str, path: Path) -> None:
    storage.execute(
        f"COPY (SELECT * FROM {table}) TO "
        f"{storage.sql_literal(path.as_posix())} "
        "(FORMAT PARQUET, COMPRESSION 'snappy')"
    )


def _run_metric_job(
    *,
    engine: _EvaluationEngine,
    storage: FactorStorage,
    process_dates: Sequence[str],
    candidate_glob: str,
    returns_glob: str,
    shard_directory: Path,
) -> tuple[Path, Path]:
    engine._create_stage_tables()
    source = storage.sql_literal(candidate_glob)
    returns_source = storage.sql_literal(returns_glob)
    factor_column = storage.sql_literal(engine.factor_columns[0])
    batch_days = (
        engine.cfg.date_batch_days or engine.settings.single_column_date_batch_days
    )

    def process_block(block: tuple[str, ...]) -> None:
        context_begin = engine._context_begin(block[0])
        try:
            storage.execute("BEGIN TRANSACTION")
            engine._drop_temp_relations(
                "_ranked_positions",
                "_portfolio_positions",
            )
            storage.execute(
                f"""
                CREATE TEMP TABLE _ranked_positions AS
                SELECT
                    candidate.date,
                    candidate.code,
                    {factor_column} AS factor_column,
                    CAST(candidate.variant AS evaluation_variant) AS variant,
                    candidate.factor_value,
                    returns.portfolio_id,
                    returns.gross_security_return,
                    candidate.factor_rank,
                    candidate.return_rank,
                    candidate.layer,
                    candidate.sample_count,
                    candidate.top_high_selected,
                    candidate.top_low_selected
                FROM read_parquet({source}) candidate
                JOIN (
                    SELECT date, code, portfolio_id, gross_security_return
                    FROM read_parquet({returns_source})
                    WHERE date
                          BETWEEN CAST({storage.sql_literal(context_begin)} AS DATE)
                              AND CAST({storage.sql_literal(block[-1])} AS DATE)
                ) returns USING (date, code)
                WHERE candidate.date
                      BETWEEN CAST({storage.sql_literal(context_begin)} AS DATE)
                          AND CAST({storage.sql_literal(block[-1])} AS DATE)
                """
            )
            engine._insert_ic(block[0], block[-1])
            engine._create_position_tables()
            engine._insert_returns(block[0], block[-1])
            storage.execute("COMMIT")
        except Exception as exc:
            try:
                storage.execute("ROLLBACK")
            except Exception:  # noqa: BLE001 - preserve the original OOM.
                pass
            if not _is_oom(exc) or len(block) <= 1:
                raise
            midpoint = len(block) // 2
            process_block(block[:midpoint])
            process_block(block[midpoint:])

    for values in _chunks(process_dates, batch_days):
        process_block(tuple(values))
    ic_path = shard_directory / "ic.parquet"
    returns_path = shard_directory / "returns.parquet"
    _copy_table(storage, "_evaluation_ic", ic_path)
    _copy_table(storage, "_evaluation_returns", returns_path)
    return ic_path, returns_path


def _prepare_correlation_job(
    *,
    engine: _EvaluationEngine,
    storage: FactorStorage,
    begin: str,
    end: str,
    returns_glob: str,
    shard_directory: Path,
) -> None:
    engine._prepare_reference_sources(
        begin,
        end,
        materialize=False,
    )
    returns_source = storage.sql_literal(returns_glob)
    storage.execute(
        f"""
        CREATE TEMP VIEW _forward_returns_block AS
        SELECT date, code
        FROM read_parquet({returns_source})
        WHERE date BETWEEN CAST({storage.sql_literal(begin)} AS DATE)
                       AND CAST({storage.sql_literal(end)} AS DATE)
        """
    )
    engine._create_reference_block(begin, end, materialize=False)
    reference_root = shard_directory / "reference_ranked"
    storage.execute(
        f"""
        COPY (
            SELECT
                YEAR(date) AS reference_year,
                date,
                code,
                reference_id,
                reference_rank
            FROM _reference_ranked
        ) TO {storage.sql_literal(reference_root.as_posix())} (
            FORMAT PARQUET,
            COMPRESSION 'snappy',
            ROW_GROUP_SIZE 122880,
            PARTITION_BY (reference_year)
        )
        """
    )
    reference_glob = reference_root.as_posix() + "/reference_year=*/*.parquet"
    storage.execute(
        f"""
        CREATE TEMP VIEW _prepared_reference_ranked AS
        SELECT date, code, reference_id, reference_rank
        FROM read_parquet({storage.sql_literal(reference_glob)})
        """
    )


def _run_correlation_job(
    *,
    engine: _EvaluationEngine,
    storage: FactorStorage,
    process_dates: Sequence[str],
    candidate_glob: str,
    shard_directory: Path,
) -> Path:
    engine._create_stage_tables()
    source = storage.sql_literal(candidate_glob)
    factor_column = storage.sql_literal(engine.factor_columns[0])

    def process_block(block: tuple[str, ...]) -> None:
        try:
            storage.execute("BEGIN TRANSACTION")
            engine._drop_temp_relations(
                "_correlation_candidates",
                "_reference_ranked",
            )
            storage.execute(
                f"""
                CREATE TEMP VIEW _correlation_candidates AS
                SELECT
                    date,
                    code,
                    {factor_column} AS factor_column,
                    CAST(variant AS evaluation_variant) AS variant,
                    factor_value,
                    factor_rank
                FROM read_parquet({source})
                WHERE date
                      BETWEEN CAST({storage.sql_literal(block[0])} AS DATE)
                          AND CAST({storage.sql_literal(block[-1])} AS DATE)
                """
            )
            storage.execute(
                f"""
                CREATE TEMP VIEW _reference_ranked AS
                SELECT date, code, reference_id, reference_rank
                FROM _prepared_reference_ranked
                WHERE date
                      BETWEEN CAST({storage.sql_literal(block[0])} AS DATE)
                          AND CAST({storage.sql_literal(block[-1])} AS DATE)
                """
            )
            engine._insert_correlations_from(
                "_correlation_candidates",
                block_begin=block[0],
                block_end=block[-1],
            )
            storage.execute("COMMIT")
        except Exception as exc:
            try:
                storage.execute("ROLLBACK")
            except Exception:  # noqa: BLE001 - preserve the original OOM.
                pass
            if not _is_oom(exc) or len(block) <= 1:
                raise
            midpoint = len(block) // 2
            process_block(block[:midpoint])
            process_block(block[midpoint:])

    process_block(tuple(process_dates))
    path = shard_directory / "correlation.parquet"
    _copy_table(storage, "_evaluation_correlation", path)
    return path


def _waiting_worker_entry(
    job: _ParallelJob,
    cfg: EvaluationConfig,
    project_root_value: str,
    factor_source: str,
    factor_columns: tuple[str, ...],
    workspace_value: str,
    worker_limit: _WorkerLimit,
    task_queue: multiprocessing.queues.Queue,
    result_queue: multiprocessing.queues.Queue,
) -> None:
    project_root = Path(project_root_value)
    workspace = Path(workspace_value)
    worker_cfg = _worker_config(
        cfg=replace(cfg, factor_columns=factor_columns),
        job=job,
        workspace=workspace,
        worker_limit=worker_limit,
    )
    storage: FactorStorage | None = None
    phase = "initialize"
    try:
        message = task_queue.get()
        if message is None:
            return
        if message != ("initialize",):
            raise RuntimeError("worker expected an initialize message")
        initialized_at = time.perf_counter()
        storage = _new_storage(
            cfg=worker_cfg,
            project_root=project_root,
            factor_source=factor_source,
            temp_directory=Path(worker_cfg.temp_directory),
        )
        engine = _new_engine(
            storage=storage,
            cfg=worker_cfg,
            project_root=project_root,
            factor_source=factor_source,
            factor_columns=factor_columns,
            reference_factors=job.reference_factors,
        )
        shard_directory = workspace / "shards" / f"worker_{job.index}"
        shard_directory.mkdir(parents=True, exist_ok=True)
        _prepare_worker_engine(engine, workspace=shard_directory)
        storage.execute(
            "CREATE TYPE evaluation_variant AS ENUM "
            "('raw', 'neutralized', 'rolling_mean')"
        )
        result_queue.put(
            {
                "kind": "ready",
                "job": job.index,
                "job_kind": job.kind,
                "elapsed_seconds": time.perf_counter() - initialized_at,
            }
        )
        message = task_queue.get()
        if message is None:
            return
        if job.kind == "metrics":
            kind, candidate_glob, returns_glob, process_dates = message
            if kind != "run":
                raise RuntimeError("metric worker expected a run message")
            phase = "run"
            started = time.perf_counter()
            ic_path, returns_path = _run_metric_job(
                engine=engine,
                storage=storage,
                process_dates=process_dates,
                candidate_glob=candidate_glob,
                returns_glob=returns_glob,
                shard_directory=shard_directory,
            )
            result_queue.put(
                {
                    "kind": "completed",
                    "job": job.index,
                    "job_kind": job.kind,
                    "paths": (ic_path.as_posix(), returns_path.as_posix()),
                    "elapsed_seconds": time.perf_counter() - started,
                }
            )
            return

        kind, begin, end, returns_glob = message
        if kind != "prepare":
            raise RuntimeError("correlation worker expected a prepare message")
        phase = "prepare"
        prepared_at = time.perf_counter()
        _prepare_correlation_job(
            engine=engine,
            storage=storage,
            begin=begin,
            end=end,
            returns_glob=returns_glob,
            shard_directory=shard_directory,
        )
        result_queue.put(
            {
                "kind": "prepared",
                "job": job.index,
                "job_kind": job.kind,
                "elapsed_seconds": time.perf_counter() - prepared_at,
            }
        )
        message = task_queue.get()
        if message is None:
            return
        kind, candidate_glob, _returns_glob, process_dates = message
        if kind != "run":
            raise RuntimeError("correlation worker expected a run message")
        phase = "run"
        started = time.perf_counter()
        correlation_path = _run_correlation_job(
            engine=engine,
            storage=storage,
            process_dates=process_dates,
            candidate_glob=candidate_glob,
            shard_directory=shard_directory,
        )
        result_queue.put(
            {
                "kind": "completed",
                "job": job.index,
                "job_kind": job.kind,
                "paths": (correlation_path.as_posix(),),
                "elapsed_seconds": time.perf_counter() - started,
            }
        )
    except Exception as exc:  # noqa: BLE001 - propagate worker diagnostics.
        result_queue.put(
            {
                "kind": "failed",
                "job": job.index,
                "job_kind": job.kind,
                "phase": phase,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }
        )
    finally:
        if storage is not None:
            storage.close()


def _terminate(processes: Sequence[multiprocessing.Process]) -> None:
    for process in processes:
        if process.is_alive():
            process.terminate()
    for process in processes:
        process.join()


def _prelaunch_jobs(
    *,
    jobs: Sequence[_ParallelJob],
    cfg: EvaluationConfig,
    project_root: Path,
    factor_source: str,
    factor_columns: tuple[str, ...],
    workspace: Path,
    worker_limits: dict[int, _WorkerLimit],
) -> _PrelaunchedJobs:
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    task_queues = {job.index: context.Queue(maxsize=1) for job in jobs}
    processes = tuple(
        context.Process(
            target=_waiting_worker_entry,
            args=(
                job,
                cfg,
                project_root.as_posix(),
                factor_source,
                factor_columns,
                workspace.as_posix(),
                worker_limits[job.index],
                task_queues[job.index],
                result_queue,
            ),
            name=f"evaluation-column-worker-{job.index}",
        )
        for job in jobs
    )
    launch = _PrelaunchedJobs(
        processes=processes,
        task_queues=task_queues,
        result_queue=result_queue,
        started=[],
        start_errors=[],
    )

    def start_all() -> None:
        try:
            for process, job in zip(processes, jobs, strict=True):
                process.start()
                launch.started.append(process)
        except BaseException as exc:  # noqa: BLE001 - forward launch failure.
            launch.start_errors.append(exc)

    launch.thread = threading.Thread(
        target=start_all,
        name="evaluation-column-worker-launcher",
        daemon=True,
    )
    launch.thread.start()
    return launch


def _close_prelaunched_queues(launch: _PrelaunchedJobs) -> None:
    for task_queue in launch.task_queues.values():
        task_queue.close()
        task_queue.join_thread()
    launch.result_queue.close()
    launch.result_queue.join_thread()


def _initialize_prelaunched_jobs(launch: _PrelaunchedJobs) -> None:
    if launch.thread is not None:
        launch.thread.join()
    if launch.start_errors:
        raise RuntimeError("failed to prestart intra-column workers") from (
            launch.start_errors[0]
        )
    if len(launch.started) != len(launch.processes):
        raise RuntimeError("not all intra-column workers were prestarted")
    if launch.initialized:
        raise RuntimeError("intra-column workers were initialized twice")
    for task_queue in launch.task_queues.values():
        task_queue.put(("initialize",))
    launch.initialized = True


def _cancel_prelaunched_jobs(launch: _PrelaunchedJobs) -> None:
    if launch.thread is not None:
        launch.thread.join()
    _terminate(tuple(launch.started))
    _close_prelaunched_queues(launch)


def _run_jobs(
    *,
    jobs: Sequence[_ParallelJob],
    candidate_glob: str,
    returns_glob: str,
    process_dates: tuple[str, ...],
    launch: _PrelaunchedJobs,
) -> dict[int, tuple[Path, ...]]:
    if launch.thread is not None:
        launch.thread.join()
    processes = tuple(launch.started)
    if launch.start_errors:
        _terminate(processes)
        _close_prelaunched_queues(launch)
        raise RuntimeError(
            "failed to prestart intra-column workers"
        ) from launch.start_errors[0]
    if len(processes) != len(jobs):
        _terminate(processes)
        _close_prelaunched_queues(launch)
        raise RuntimeError("not all intra-column workers were prestarted")
    if not launch.initialized:
        _terminate(processes)
        _close_prelaunched_queues(launch)
        raise RuntimeError("intra-column workers were not initialized")
    result_queue = launch.result_queue
    process_by_job = {
        job.index: process for job, process in zip(jobs, processes, strict=True)
    }
    completed: dict[int, tuple[Path, ...]] = {}
    failure: str | None = None
    try:
        ready: set[int] = set()
        while len(ready) < len(jobs) and failure is None:
            try:
                event = result_queue.get(timeout=0.5)
            except queue.Empty:
                dead = [
                    job.index
                    for job in jobs
                    if job.index not in ready
                    and not process_by_job[job.index].is_alive()
                ]
                if dead:
                    failure = "workers exited during initialization: " + ", ".join(
                        str(value) for value in dead
                    )
                continue
            if event.get("kind") == "failed":
                failure = (
                    f"worker {event['job']} ({event['job_kind']}) failed "
                    f"during {event.get('phase', 'initialize')}: "
                    f"{event['error']}"
                )
                break
            if event.get("kind") != "ready":
                failure = "worker reported an invalid initialization event"
                break
            job_index = int(event["job"])
            ready.add(job_index)

        metric_job = next(job for job in jobs if job.kind == "metrics")
        metric_payload = ("run", candidate_glob, returns_glob, process_dates)
        if failure is None:
            launch.task_queues[metric_job.index].put(metric_payload)

        correlation_jobs = tuple(job for job in jobs if job.kind == "correlation")
        prepared: set[int] = set()
        pending = list(correlation_jobs)
        active: set[int] = set()

        def fill_preparation_slots() -> None:
            while pending and len(active) < _REFERENCE_PREPARE_CONCURRENCY:
                job = pending.pop(0)
                launch.task_queues[job.index].put(
                    (
                        "prepare",
                        process_dates[0],
                        process_dates[-1],
                        returns_glob,
                    )
                )
                active.add(job.index)

        if failure is None:
            fill_preparation_slots()
        while len(prepared) < len(correlation_jobs) and failure is None:
            try:
                event = result_queue.get(timeout=0.5)
            except queue.Empty:
                dead = [
                    job_index
                    for job_index in active
                    if not process_by_job[job_index].is_alive()
                ]
                if dead:
                    failure = (
                        "correlation workers exited during preparation: "
                        + ", ".join(str(value) for value in dead)
                    )
                continue
            if event.get("kind") == "failed":
                failure = (
                    f"worker {event['job']} ({event['job_kind']}) failed "
                    f"during {event.get('phase', 'unknown')}: "
                    f"{event['error']}"
                )
                break
            if event.get("kind") == "completed":
                job_index = int(event["job"])
                if job_index != metric_job.index:
                    failure = "correlation worker completed before its run barrier"
                    break
                completed[job_index] = tuple(
                    Path(value) for value in event.get("paths", ())
                )
                continue
            if event.get("kind") != "prepared":
                failure = "worker reported completion before preparation barrier"
                break
            job_index = int(event["job"])
            if job_index not in active:
                failure = "worker reported preparation outside the active window"
                break
            active.remove(job_index)
            prepared.add(job_index)
            fill_preparation_slots()

        if failure is None:
            payload = ("run", candidate_glob, returns_glob, process_dates)
            for job in correlation_jobs:
                launch.task_queues[job.index].put(payload)

        while len(completed) < len(jobs) and failure is None:
            try:
                event = result_queue.get(timeout=0.5)
            except queue.Empty:
                dead = [
                    job.index
                    for job in jobs
                    if job.index not in completed
                    and not process_by_job[job.index].is_alive()
                ]
                if dead:
                    failure = "workers exited before completion: " + ", ".join(
                        str(value) for value in dead
                    )
                continue
            if event.get("kind") == "failed":
                failure = (
                    f"worker {event['job']} ({event['job_kind']}) failed "
                    f"during {event.get('phase', 'run')}: "
                    f"{event['error']}"
                )
                break
            job_index = int(event["job"])
            completed[job_index] = tuple(
                Path(value) for value in event.get("paths", ())
            )
    except KeyboardInterrupt:
        _terminate(processes)
        raise
    finally:
        if failure is not None:
            _terminate(processes)
        else:
            for process in processes:
                process.join()
        _close_prelaunched_queues(launch)
    if failure is not None:
        raise RuntimeError(failure)
    bad_exits = [
        f"{process.name}={process.exitcode}"
        for process in processes
        if process.exitcode not in (0, None)
    ]
    if bad_exits:
        raise RuntimeError(
            "intra-column workers exited unsuccessfully: " + ", ".join(bad_exits)
        )
    return completed


def _merge_shards(
    *,
    cfg: EvaluationConfig,
    project_root: Path,
    factor_source: str,
    factor_columns: tuple[str, ...],
    reference_factors: tuple[str, ...],
    jobs: Sequence[_ParallelJob],
    shards: dict[int, tuple[Path, ...]],
    workspace: Path,
    worker_limits: dict[int, _WorkerLimit],
) -> Path:
    merge_cfg = replace(cfg, intra_column_workers=len(jobs))
    storage = _new_storage(
        cfg=merge_cfg,
        project_root=project_root,
        factor_source=factor_source,
        temp_directory=workspace / "merge_spill",
    )
    staged_columns: list[tuple[Path, Path]] = []
    try:
        engine = _new_engine(
            storage=storage,
            cfg=merge_cfg,
            project_root=project_root,
            factor_source=factor_source,
            factor_columns=factor_columns,
            reference_factors=reference_factors,
        )
        _prepare_engine(engine)
        engine.column_batch_size = 1
        engine.date_batch_days = (
            cfg.date_batch_days or engine.settings.single_column_date_batch_days
        )
        engine._create_stage_tables()
        metric_paths = shards[0]
        storage.execute(
            "INSERT INTO _evaluation_ic SELECT * FROM read_parquet("
            f"{storage.sql_literal(metric_paths[0].as_posix())})"
        )
        storage.execute(
            "INSERT INTO _evaluation_returns SELECT * FROM read_parquet("
            f"{storage.sql_literal(metric_paths[1].as_posix())})"
        )
        for job in jobs:
            if job.kind != "correlation":
                continue
            path = shards[job.index][0]
            storage.execute(
                "INSERT INTO _evaluation_correlation SELECT * FROM read_parquet("
                f"{storage.sql_literal(path.as_posix())})"
            )
        engine._create_nav_results()
        staged_columns = [
            (engine._stage_column(state), state.output_dir)
            for state in engine._states
        ]
    except Exception:
        for staging, _destination in staged_columns:
            if staging.exists():
                shutil.rmtree(staging)
        raise
    finally:
        storage.close()
    try:
        for staging, destination in staged_columns:
            replace_column_directory(staging, destination)
    except Exception:
        for staging, _destination in staged_columns:
            if staging.exists():
                shutil.rmtree(staging)
        raise
    return engine._states[0].output_dir


def run_parallel_single_column_evaluation(
    *,
    cfg: EvaluationConfig,
    project_root: Path,
    factor_source: str,
    factor_columns: tuple[str, ...],
    reference_factors: tuple[str, ...],
) -> Path:
    """Run a full single-column rebuild with bounded SQL worker shards."""

    if len(factor_columns) != 1:
        raise ValueError("parallel single-column evaluation requires one column")
    jobs, worker_limits = _parallel_layout(cfg, reference_factors)
    temp_root = _resolve_path(cfg.temp_directory, project_root=project_root)
    temp_root.mkdir(parents=True, exist_ok=True)
    workspace = Path(
        tempfile.mkdtemp(
            prefix=f"{factor_source}_parallel_",
            dir=temp_root,
        )
    )
    launch: _PrelaunchedJobs | None = None

    def launch_workers() -> None:
        nonlocal launch
        if launch is not None:
            raise RuntimeError("intra-column workers were launched twice")
        launch = _prelaunch_jobs(
            jobs=jobs,
            cfg=cfg,
            project_root=project_root,
            factor_source=factor_source,
            factor_columns=factor_columns,
            workspace=workspace,
            worker_limits=worker_limits,
        )

    def initialize_workers() -> None:
        if launch is None:
            raise RuntimeError("intra-column workers were not launched")
        _initialize_prelaunched_jobs(launch)

    try:
        try:
            candidate_glob, returns_glob, process_dates = _prepare_candidate_panel(
                cfg=cfg,
                project_root=project_root,
                factor_source=factor_source,
                factor_columns=factor_columns,
                workspace=workspace,
                on_factor_batch_ready=launch_workers,
                on_candidate_panel_ready=initialize_workers,
            )
        except BaseException:
            if launch is not None:
                _cancel_prelaunched_jobs(launch)
            raise
        if launch is None:
            raise RuntimeError("intra-column workers were not launched")
        shards = _run_jobs(
            jobs=jobs,
            candidate_glob=candidate_glob,
            returns_glob=returns_glob,
            process_dates=process_dates,
            launch=launch,
        )
        return _merge_shards(
            cfg=cfg,
            project_root=project_root,
            factor_source=factor_source,
            factor_columns=factor_columns,
            reference_factors=reference_factors,
            jobs=jobs,
            shards=shards,
            workspace=workspace,
            worker_limits=worker_limits,
        )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


__all__ = ["run_parallel_single_column_evaluation"]
