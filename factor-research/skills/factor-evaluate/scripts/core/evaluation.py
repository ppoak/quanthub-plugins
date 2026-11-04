from __future__ import annotations

import multiprocessing
import queue
from time import monotonic
from argparse import Namespace
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

from .shared import print_progress

from .engine import (
    EvaluationConfig,
)
from .package import refresh_evaluations, run_package_evaluation as run_evaluation
from factor_protocol import resolve_data_root, resolve_factor_zoo_root
from factor_protocol.artifacts import resolve_output_root
from factor_protocol.readme import update_acceptance
from .validation import check_package_evaluation as check_evaluation_config
from factor_protocol.loader import (
    load_module_from_path,
    resolve_factor_package,
    select_factor_columns,
)
from factor_protocol.registry import list_active_factor_names
from .readme import IncompleteEvaluationArtifacts, update_factor_readme


from .engine import run_evaluation as run_data_evaluation
from .check import check_evaluation_config as check_data_evaluation

@dataclass(frozen=True)
class _EvaluationTask:
    factor_name: str | None
    factor_columns: tuple[str, ...]

    @property
    def label(self) -> str:
        return ", ".join(
            f"{self.factor_name or 'data'}.{column}" for column in self.factor_columns
        )


def _execute(args, cfg):
    if args.factor_data:
        return run_data_evaluation(cfg)
    return run_evaluation(cfg)


def _config_from_args(
    args: Namespace,
    *,
    factor_name: str | None,
    temp_directory: str | Path,
    project_root: Path,
) -> EvaluationConfig:
    return EvaluationConfig(
        factor_name=factor_name,
        tasks=tuple(dict.fromkeys(args.task or ("ic", "portfolio", "correlation"))),
        storage_factor_name=args.storage_factor_name,
        factor_data=args.factor_data,
        begin=args.begin,
        train_end=args.train_end,
        validation_end=args.validation_end,
        end=args.end,
        horizon_days=args.horizon_days,
        execution_lag_days=args.execution_lag_days,
        price_col=args.price_col,
        quantiles=args.quantiles,
        top_percentile=args.top_percentile,
        rolling_mean_days=args.rolling_mean_days,
        neutralization_industry_level=args.neutralization_industry_level,
        reference_factors=(
            tuple(args.reference_factor) if args.reference_factor is not None else None
        ),
        factor_columns=tuple(args.factor_column) if args.factor_column else None,
        data_root=args.data_root,
        output_dir=args.output_dir,
        factor_zoo_root=args.factor_zoo_root,
        threads=args.threads,
        memory_limit=args.memory_limit,
        temp_directory=str(temp_directory),
        column_batch_size=args.column_batch_size,
        date_batch_days=args.date_batch_days,
        intra_column_workers=args.intra_column_workers,
        rebuild=args.rebuild,
        project_root=project_root,
        settings=args.settings,
    )


def _factor_names(args: Namespace, *, project_root: Path) -> list[str | None]:
    if args.factor_data:
        return [None]
    if args.all_alpha:
        if args.storage_factor_name:
            raise ValueError("--storage-factor-name requires one factor package")
        if args.factor_name or args.factor_column:
            raise ValueError(
                "--all-alpha cannot be combined with individual inputs or --factor-column"
            )
        return [
            name
            for name in list_active_factor_names(
                project_root=project_root,
                factor_zoo_root=args.factor_zoo_root,
            )
            if not name.startswith("barra_cne5_")
        ]
    if len(args.factor_name) != 1:
        raise ValueError("select exactly one factor package or data input")
    return [args.factor_name[0]]


def _factor_columns(
    args: Namespace,
    *,
    factor_name: str | None,
    project_root: Path,
) -> tuple[str, ...]:
    if args.factor_data:
        return tuple(args.factor_column)
    package = resolve_factor_package(
        factor_name,
        project_root=project_root,
        factor_zoo_root=args.factor_zoo_root,
    )
    module = load_module_from_path(
        package.module_path,
        project_root=project_root,
        factor_name=factor_name,
    )
    requested = tuple(args.factor_column) if args.factor_column else None
    return select_factor_columns(module, requested)


def _declared_factor_columns(
    factor_name: str,
    *,
    factor_zoo_root: str | Path,
    project_root: Path,
) -> tuple[str, ...]:
    package = resolve_factor_package(
        factor_name,
        project_root=project_root,
        factor_zoo_root=factor_zoo_root,
    )
    module = load_module_from_path(
        package.module_path,
        project_root=project_root,
        factor_name=factor_name,
    )
    return select_factor_columns(module, None)


def _write_factor_readmes(
    args: Namespace,
    *,
    factor_names: Sequence[str],
    project_root: Path,
) -> int:
    output_root = resolve_output_root(args.output_dir, project_root=project_root)
    factor_zoo_root = resolve_factor_zoo_root(
        project_root=project_root,
        factor_zoo_root=args.factor_zoo_root,
    )
    for factor_name in factor_names:
        columns = _declared_factor_columns(
            factor_name,
            factor_zoo_root=args.factor_zoo_root,
            project_root=project_root,
        )
        readme_path = factor_zoo_root / factor_name / "README.md"
        update_acceptance(readme_path, 7, False)
        failures = {label.removeprefix(f"{factor_name}."): error
                    for labels, error in getattr(args, "evaluation_failures", {}).items()
                    for label in labels.split(", ") if label.startswith(f"{factor_name}.")}
        print(f"Evaluate {factor_name} | phase=updating README", flush=True)
        try:
            changed = update_factor_readme(
                readme_path=readme_path,
                output_root=output_root,
                factor_name=factor_name,
                columns=columns,
                project_root=project_root,
                data_root=args.data_root,
                failures=failures,
            )
        except IncompleteEvaluationArtifacts as exc:
            update_factor_readme(
                readme_path=readme_path, output_root=output_root, factor_name=factor_name,
                columns=columns, project_root=project_root, data_root=args.data_root,
                failures={column: str(exc) for column in columns},
            )
            print(f"README writeback failed for {factor_name}: {exc}", flush=True)
            return 1
        except Exception as exc:  # noqa: BLE001 - report writeback failures to the CLI.
            print(
                f"README writeback failed for {factor_name}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            return 1
        if failures:
            return 1
        action = "updated" if changed else "already current"
        print(f"Factor README {action}: {readme_path.as_posix()}", flush=True)
    return 0


def _evaluation_tasks(
    args: Namespace,
    *,
    factor_names: Sequence[str | None],
    project_root: Path,
) -> list[_EvaluationTask]:
    group_size = int(args.worker_column_group_size)
    if group_size <= 0:
        raise ValueError("--worker-column-group-size must be positive")
    tasks: list[_EvaluationTask] = []
    for factor_name in factor_names:
        columns = _factor_columns(
            args,
            factor_name=factor_name,
            project_root=project_root,
        )
        for index in range(0, len(columns), group_size):
            tasks.append(
                _EvaluationTask(
                    factor_name=factor_name,
                    factor_columns=tuple(columns[index : index + group_size]),
                )
            )
    return tasks


def _assign_tasks(
    tasks: Sequence[_EvaluationTask],
    workers: int,
) -> list[list[_EvaluationTask]]:
    assignments: list[list[_EvaluationTask]] = [[] for _ in range(workers)]
    for index, task in enumerate(tasks):
        assignments[index % workers].append(task)
    return assignments


def _worker_temp_directory(
    args: Namespace,
    *,
    project_root: Path,
    worker_index: int,
) -> Path:
    temp_directory = Path(args.temp_directory)
    if not temp_directory.is_absolute():
        temp_directory = project_root / temp_directory
    return temp_directory / f"worker_{worker_index}"


def _worker_config(
    args: Namespace,
    *,
    task: _EvaluationTask,
    project_root: Path,
    worker_index: int,
) -> EvaluationConfig:
    config = _config_from_args(
        args,
        factor_name=task.factor_name,
        temp_directory=_worker_temp_directory(
            args,
            project_root=project_root,
            worker_index=worker_index,
        ),
        project_root=project_root,
    )
    return replace(
        config,
        factor_columns=task.factor_columns,
        # A worker task has independently materialized outputs per column.
        # Keep its internal batches at one column to avoid returning to the
        # high-RSS multi-column execution that this scheduler replaces.
        column_batch_size=args.column_batch_size or 1,
        # The full-history single-column benchmark selected 126 dates.  A
        # caller can still select another value explicitly.
        date_batch_days=(
            args.date_batch_days
            if args.date_batch_days is not None
            else args.settings.single_column_date_batch_days
        ),
    )


def _run_worker(
    worker_index: int,
    tasks: Sequence[_EvaluationTask],
    args: Namespace,
    project_root: str,
    result_queue: multiprocessing.queues.Queue,
) -> None:
    root = Path(project_root)
    for task in tasks:
        result_queue.put({"kind": "started", "worker": worker_index, "label": task.label})
        try:
            output = _execute(args,
                _worker_config(
                    args,
                    task=task,
                    project_root=root,
                    worker_index=worker_index,
                )
            )
        except Exception as exc:  # noqa: BLE001 - report every worker task.
            result_queue.put(
                {
                    "kind": "failed",
                    "worker": worker_index,
                    "label": task.label,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        else:
            result_queue.put(
                {
                    "kind": "completed",
                    "worker": worker_index,
                    "label": task.label,
                    "output": output.as_posix(),
                }
            )
    result_queue.put({"kind": "worker_finished", "worker": worker_index})


def _terminate(processes: Sequence[multiprocessing.Process]) -> None:
    for process in processes:
        if process.is_alive():
            process.terminate()
    for process in processes:
        process.join()


def _run_parallel(
    args: Namespace,
    *,
    project_root: Path,
    tasks: Sequence[_EvaluationTask],
) -> int:
    worker_count = min(int(args.workers), len(tasks))
    assignments = _assign_tasks(tasks, worker_count)
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_run_worker,
            args=(index + 1, assignment, args, project_root.as_posix(), result_queue),
            name=f"factor-evaluation-worker-{index + 1}",
        )
        for index, assignment in enumerate(assignments)
    ]
    started = monotonic()
    print(f"Evaluate tasks | total={len(tasks)} | workers={worker_count}", flush=True)
    expected = {task.label for task in tasks}
    completed: set[str] = set()
    failures: dict[str, str] = {}
    try:
        for process, assignment in zip(processes, assignments, strict=True):
            process.start()
        while len(completed) + len(failures) < len(expected):
            try:
                event = result_queue.get(timeout=0.5)
            except queue.Empty:
                if all(not process.is_alive() for process in processes):
                    break
                continue
            kind = str(event.get("kind"))
            if kind == "started":
                print(f"Evaluate task | worker={event['worker']} | {event['label']}", flush=True)
            elif kind == "completed":
                label = str(event["label"])
                completed.add(label)
                print_progress("Evaluate tasks", len(completed), len(expected), started, failed=len(failures))
            elif kind == "failed":
                label = str(event["label"])
                failures[label] = str(event["error"])
                print(
                    f"failed worker={event['worker']} {label}: {event['error']}",
                    flush=True,
                )
                print_progress("Evaluate tasks", len(completed), len(expected), started, failed=len(failures))
    except KeyboardInterrupt:
        _terminate(processes)
        raise
    finally:
        for process in processes:
            process.join()
    missing = expected - completed - failures.keys()
    for label in sorted(missing):
        failures[label] = "worker exited before reporting a result"
        print(f"failed {label}: {failures[label]}", flush=True)
    for process in processes:
        if process.exitcode not in (0, None):
            print(
                f"worker {process.name} exited with code {process.exitcode}",
                flush=True,
            )
    result_queue.close()
    result_queue.join_thread()
    print(
        f"evaluation complete: columns={len(completed)}/{len(expected)}; "
        f"failed={len(failures)}; workers={worker_count}",
        flush=True,
    )
    if failures:
        print("failed columns: " + ", ".join(sorted(failures)), flush=True)
    args.evaluation_failures = failures
    return 1 if failures else 0


def _run_sequential(
    args: Namespace,
    *,
    factor_names: Sequence[str | None],
    project_root: Path,
) -> int:
    failures: dict[str, str] = {}
    # Keep the single-process path consistent with the worker path.  In
    # particular, ``--worker-column-group-size 1`` must not silently fall back
    # to one wide UNPIVOT of every dense factor column under a small memory
    # limit; that was the source of multi-tens-of-GB temporary spills during
    # factor_update refreshes.
    tasks = _evaluation_tasks(
        args,
        factor_names=factor_names,
        project_root=project_root,
    )
    started = monotonic()
    print(f"Evaluate tasks | total={len(tasks)} | workers=1", flush=True)
    for index, task in enumerate(tasks, 1):
        print(f"Evaluate task {index}/{len(tasks)} | {task.label}", flush=True)
        try:
            output = _execute(args,
                _worker_config(
                    args,
                    task=task,
                    worker_index=1,
                    project_root=project_root,
                )
            )
        except Exception as exc:  # noqa: BLE001
            failures[task.label] = f"{type(exc).__name__}: {exc}"
            print(f"failed {task.label}: {type(exc).__name__}: {exc}", flush=True)
        print_progress("Evaluate tasks", index - len(failures), len(tasks), started, failed=len(failures))
    print(
        f"evaluation complete: columns={len(tasks) - len(failures)}/{len(tasks)}; "
        f"failed={len(failures)}",
        flush=True,
    )
    if failures:
        print("failed columns: " + ", ".join(failures), flush=True)
    args.evaluation_failures = failures
    return 1 if failures else 0


def run(args: Namespace, *, project_root: str | Path) -> int:
    """Orchestrate optional package prerequisites, metric tasks and postprocessing."""
    root = Path(project_root)
    if args.factor_data:
        args.factor_data = (root / args.factor_data).resolve().as_posix()
        if not Path(args.factor_data).is_dir():
            raise ValueError(f"factor data directory is unavailable: {args.factor_data}")
        if args.storage_factor_name:
            raise ValueError("--storage-factor-name requires package input")
    args.factor_name = args.factor_package
    args.output_dir = args.output_dir or (
        "artifacts/factor_evaluation" if args.factor_data else "artifacts/factor_zoo"
    )
    operations = set(args.operation or ("evaluate",))
    if args.factor_data and (args.all_alpha or args.refresh or "readme" in operations):
        raise ValueError("data input cannot use package refresh, registry selection or README updates")
    if args.factor_data and not args.factor_column:
        raise ValueError("--factor-data requires explicit --factor-column")
    if args.workers <= 0:
        raise ValueError("workers must be positive")
    if args.refresh and (args.task or args.all_alpha or len(args.factor_name) != 1 or args.factor_column):
        raise ValueError("--refresh requires exactly one complete factor package")
    names = _factor_names(args, project_root=root)
    if "readme" in operations:
        zoo = resolve_factor_zoo_root(project_root=root, factor_zoo_root=args.factor_zoo_root)
        for name in names:
            update_acceptance(zoo / name / "README.md", 7, False)
    if "evaluate" in operations:
        if args.refresh:
            refresh_evaluations(factor_name=names[0], project_root=root,
                storage_factor_name=args.storage_factor_name, data_root=args.data_root,
                output_root=args.output_dir, factor_zoo_root=args.factor_zoo_root,
                threads=args.threads, memory_limit=args.memory_limit,
                temp_directory=args.temp_directory, end=args.end, settings=args.settings)
            result = 0
        elif args.workers == 1:
            result = _run_sequential(args, factor_names=names, project_root=root)
        else:
            result = _run_parallel(args, project_root=root,
                tasks=_evaluation_tasks(args, factor_names=names, project_root=root))
        if result:
            return result
    output = resolve_output_root(args.output_dir, project_root=root)
    for name in names:
        columns = _factor_columns(args, factor_name=name, project_root=root)
        directory = output / name if name is not None else output
        if operations & {"check", "plot", "readme"}:
            # Selecting package input always selects its prerequisite contract.
            checker = check_data_evaluation if args.factor_data else check_evaluation_config
            for column in columns:
                options = {"factor_data": args.factor_data} if args.factor_data else {}
                checker(directory / column, factor_name=name,
                    factor_source=args.storage_factor_name or name, project_root=root,
                    data_root=resolve_data_root(project_root=root, data_root=args.data_root), **options)
                if "plot" in operations:
                    from .visualize import render
                    render(project_root=root, factor_name=name, factor_column=column, output_dir=output)
    if "readme" in operations:
        return _write_factor_readmes(args, factor_names=names, project_root=root)
    return 0
