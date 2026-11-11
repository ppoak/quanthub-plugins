"""Coordinate selected tasks, measured updates and independent README writeback."""
from __future__ import annotations

import gc
import os
import time
from argparse import Namespace
from datetime import datetime
from pathlib import Path

from factor_protocol import resolve_data_root, resolve_factor_zoo_root
from factor_protocol.artifacts import resolve_output_root

from .models import UpdateConfig
from .monitor import ProcessTreeRssSampler
from .report import preview_readme, save_update_report
from .resources import configure_resources
from .selection import select_packages
from .tasks.readme import write_readme
from .workflow import run_update


def _measure_update(cfg: UpdateConfig):
    started = time.perf_counter()
    sampler = None
    sampler_error = None
    peak = None
    if not cfg.dry_run:
        try:
            sampler = ProcessTreeRssSampler(os.getpid())
            sampler.start()
        except OSError as exc:
            sampler_error = str(exc)
    try:
        result = run_update(cfg)
    finally:
        seconds = time.perf_counter() - started
        if sampler is not None:
            try:
                peak = sampler.close()
                sampler_error = sampler.error
            except OSError as exc:
                sampler_error = str(exc)
    return result, seconds, peak, sampler_error


def run(args: Namespace, *, project_root: str | Path) -> int:
    root = Path(project_root).resolve()
    data_root = resolve_data_root(project_root=root).as_posix()
    zoo = resolve_factor_zoo_root(project_root=root).as_posix()
    names = select_packages(project_root=root, factor_zoo_root=zoo, target=args.target)
    tasks = set(args.task or ("update",))
    failed = False
    for name in names:
        cfg = UpdateConfig(factor_name=name, project_root=root, data_root=data_root, factor_zoo_root=zoo,
                           factor_output_root=resolve_output_root("artifacts/factor_zoo", project_root=root).as_posix(),
                           threads=args.threads, memory_limit=args.memory_limit, compute_batch_days=args.compute_batch_days,
                           temp_directory=args.temp_directory, dry_run=args.dry_run)
        try:
            if "update" in tasks:
                cfg = configure_resources(cfg)
                print(f"{name}: Benchmark config | threads={cfg.threads} | memory={cfg.memory_limit} | "
                      f"batch_days={cfg.compute_batch_days}", flush=True)
                started_at = datetime.now().astimezone()
                result, seconds, peak, sampler_error = _measure_update(cfg)
                print(f"{result.factor_name}: {result.status}"
                      + (f" | {result.begin} -> {result.end}" if result.begin and result.end else "")
                      + (f" ({result.message})" if result.message else ""), flush=True)
                if result.status not in {"updated", "up_to_date", "planned"}:
                    failed = True
                    continue
                if not args.dry_run:
                    save_update_report(project_root=root, data_root=data_root, factor_zoo_root=zoo,
                                       factor_name=name, status=result.status, run_started_at=started_at,
                                       threads=cfg.threads, memory_limit=cfg.memory_limit,
                                       compute_batch_days=result.compute_batch_days, wall_clock_seconds=seconds,
                                       peak_rss_bytes=peak, sampler_error=sampler_error, begin=result.begin,
                                       after_end=result.end, source_begin=result.source_begin, message=result.message)
            if "readme" in tasks:
                if args.dry_run:
                    if "update" not in tasks:
                        preview_readme(project_root=root, factor_zoo_root=zoo, factor_name=name)
                    print(f"{name}: would write README from successful update and evaluation reports", flush=True)
                else:
                    write_readme(cfg)
        except Exception as exc:
            print(f"{name}: failed ({type(exc).__name__}: {exc})", flush=True)
            failed = True
        finally:
            gc.collect()
    return int(failed)
