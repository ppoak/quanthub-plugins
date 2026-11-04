from __future__ import annotations

import gc
import os
import time
from argparse import ArgumentParser, Namespace
from datetime import datetime
from pathlib import Path

from factor_protocol import (
    resolve_data_root,
    resolve_factor_columns,
    resolve_factor_schema_path,
    resolve_factor_zoo_root,
)
from factor_protocol.fingerprint import factor_definition_fingerprint
from factor_protocol.loader import (
    load_module_from_path,
    resolve_factor_package,
)
from factor_protocol.registry import load_active_factor_packages
from process_monitor import ProcessTreeRssSampler
from readme import update_readme_after_run
from update_workflow import UpdateConfig, run_update


def add_arguments(parser: ArgumentParser) -> None:
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--factor-name", default=None)
    parser.add_argument(
        "--all",
        action="store_true",
        help="Update complete factor packages listed in the configured registry active section.",
    )
    parser.add_argument("--data-root")
    parser.add_argument("--factor-zoo-root")
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--bootstrap-begin", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-validation-refresh", action="store_true")
    parser.add_argument("--skip-evaluation-refresh", action="store_true")
    parser.add_argument("--no-readme-update", action="store_true")
    parser.add_argument("--validation-memory-limit", default="1GB")
    parser.add_argument("--memory-limit", default="1GB")
    parser.add_argument("--temp-directory", default=None)


def run(args: Namespace, *, project_root: str | Path) -> int:
    if args.all == (args.factor_name is not None):
        raise ValueError("update requires exactly one of --factor-name or --all")
    root = Path(project_root).resolve()
    args.data_root = resolve_data_root(
        project_root=root,
        data_root=args.data_root,
    ).as_posix()
    args.factor_zoo_root = resolve_factor_zoo_root(
        project_root=root,
        factor_zoo_root=args.factor_zoo_root,
    ).as_posix()
    active_packages = {
        package.package: package
        for package in load_active_factor_packages(
            project_root=root,
            factor_zoo_root=args.factor_zoo_root,
        )
    }
    names = list(active_packages) if args.all else [args.factor_name]
    for name in names:
        active_package = active_packages.get(name)
        if active_package is None:
            schema_path = resolve_factor_schema_path(
                project_root=root,
                factor_zoo_root=args.factor_zoo_root,
            )
            raise ValueError(f"factor package is not active in {schema_path}: {name}")
        package = resolve_factor_package(
            name,
            project_root=root,
            factor_zoo_root=args.factor_zoo_root,
        )
        current_fingerprint = factor_definition_fingerprint(package)
        if active_package.fingerprint != current_fingerprint:
            raise ValueError(
                f"active factor package {name!r} fingerprint mismatch: "
                f"schema={active_package.fingerprint}, current={current_fingerprint}"
            )
        module = load_module_from_path(
            package.module_path,
            project_root=root,
            factor_name=name,
        )
        if set(active_package.columns) != set(resolve_factor_columns(module)):
            raise ValueError(
                f"active factor package {name!r} must include every declared output column"
            )

    command_failed = False
    writeback_failed = False
    for name in names:
        config = UpdateConfig(
            factor_name=name,
            data_root=args.data_root,
            factor_zoo_root=args.factor_zoo_root,
            threads=args.threads,
            bootstrap_begin=args.bootstrap_begin,
            dry_run=args.dry_run,
            refresh_validation=not args.skip_validation_refresh,
            refresh_evaluation=not args.skip_evaluation_refresh,
            readme_update=not args.no_readme_update,
            validation_memory_limit=args.validation_memory_limit,
            memory_limit=args.memory_limit,
            temp_directory=args.temp_directory,
            project_root=root,
        )
        run_started_at = datetime.now().astimezone()
        started = time.perf_counter()
        sampler: ProcessTreeRssSampler | None = None
        sampler_error: str | None = None
        if not args.dry_run:
            try:
                sampler = ProcessTreeRssSampler(os.getpid())
                sampler.start()
            except OSError as exc:
                sampler_error = str(exc)
        try:
            result = run_update(config)
        finally:
            wall_clock_seconds = time.perf_counter() - started
            peak_rss_bytes = None
            if sampler is not None:
                try:
                    peak_rss_bytes = sampler.close()
                    sampler_error = sampler.error
                except OSError as exc:
                    sampler_error = str(exc)

        print(
            f"{result.factor_name}: {result.status}"
            + (f" ({result.message})" if result.message else "")
        )
        if result.status in {
            "failed",
            "error",
            "schema_mismatch",
            "validation_failed",
            "validation_stale",
            "evaluation_stale",
            "rebuild_required",
            "uninitialized",
        }:
            command_failed = True

        if not args.dry_run and not args.no_readme_update:
            try:
                readme_result = update_readme_after_run(
                    project_root=root,
                    data_root=args.data_root,
                    factor_zoo_root=args.factor_zoo_root,
                    factor_name=result.factor_name,
                    status=result.status,
                    run_started_at=run_started_at,
                    threads=args.threads,
                    memory_limit=args.memory_limit,
                    compute_batch_days=result.compute_batch_days,
                    wall_clock_seconds=wall_clock_seconds,
                    peak_rss_bytes=peak_rss_bytes,
                    sampler_error=sampler_error,
                    begin=result.begin,
                    after_end=result.after_end,
                    source_begin=result.source_begin,
                    message=result.message,
                )
                if readme_result is not None:
                    readme_path, changed = readme_result
                    action = "updated" if changed else "already current"
                    print(
                        f"Factor README {action}: {readme_path.as_posix()}",
                        flush=True,
                    )
            except Exception as exc:  # noqa: BLE001 - do not hide failed writeback.
                print(
                    f"README writeback failed for {result.factor_name}: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
                writeback_failed = True
        gc.collect()

    return 1 if command_failed or writeback_failed else 0


def main() -> int:
    parser = ArgumentParser(description="Incrementally update active factor packages.")
    add_arguments(parser)
    args = parser.parse_args()
    return run(args, project_root=Path(args.project_root).resolve())


if __name__ == "__main__":
    raise SystemExit(main())
