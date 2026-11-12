"""Own package scheduling, update execution, downstream refresh and failure handling."""
from __future__ import annotations

import gc
import json
import os
import time
from argparse import Namespace
from dataclasses import replace
from datetime import datetime
from factor_protocol import (
    FactorStorage,
    resolve_data_root,
    resolve_factor_data_root,
    resolve_factor_zoo_root,
)
from factor_protocol.artifacts import dataset_manifest, manifests_match, resolve_output_root
from factor_protocol.identity import read_identity
from pathlib import Path
from typing import Any, Sequence

from .config import BenchmarkRequired, UpdateConfig, UpdatePlan, UpdateResult, configure_resources, select_packages
from .monitor import ProcessTreeRssSampler
from .planning import _ensure_factor_output_writable, _partition_dates, _refresh_failure_marker, plan_update
from .report import preview_readme, save_update_report
from .tasks import calculate, recover_calculation, refresh_evaluation, refresh_validation, write_readme


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


def refresh_outputs(cfg: UpdateConfig, plan: UpdatePlan) -> tuple[list[str], bool]:
    root = Path(cfg.project_root)
    dates = _partition_dates(resolve_factor_data_root(project_root=root) / (cfg.storage_factor_name or cfg.factor_name))
    if not dates:
        _clear_refresh_failure_marker(root, cfg.factor_name)
        return ["calculation completed with no eligible rows; validation/evaluation await factor data"], False
    messages: list[str] = []
    failed = False
    validation_dates = [day for day in dates if plan.begin <= day <= plan.end] if plan.calculate else dates
    if plan.refresh_validation and validation_dates:
        try:
            path = refresh_validation(factor_name=cfg.factor_name, begin=validation_dates[-5] if len(validation_dates) >= 5 else validation_dates[0],
                                      end=validation_dates[-1], cfg=cfg, project_root=root)
            messages.append(f"validation={path}")
        except Exception as exc:
            failed = True
            messages.append(f"validation_failed={type(exc).__name__}: {exc}")
    elif plan.refresh_validation:
        messages.append("validation=skipped_no_updated_output")
    if cfg.refresh_evaluation and not failed:
        try:
            refresh_evaluation(cfg, end=plan.end)
            messages.append("evaluation_refresh=completed")
        except Exception as exc:
            failed = True
            messages.append(f"evaluation_failed={type(exc).__name__}: {exc}")
    if failed:
        _write_refresh_failure_marker(root, cfg.factor_name, end=plan.end, messages=messages)
    else:
        _clear_refresh_failure_marker(root, cfg.factor_name)
    return messages, failed


def run_update(cfg: UpdateConfig) -> UpdateResult:
    root = Path(cfg.project_root or Path.cwd()).resolve()
    cfg = replace(cfg, project_root=root,
                  data_root=resolve_data_root(project_root=root, data_root=cfg.data_root).as_posix(),
                  factor_zoo_root=resolve_factor_zoo_root(project_root=root, factor_zoo_root=cfg.factor_zoo_root).as_posix())
    plan: UpdatePlan | None = None
    source_begin = None
    try:
        cfg = configure_resources(cfg)
        recover_calculation(cfg)
        planned = plan_update(cfg)
        if isinstance(planned, UpdateResult):
            return planned
        plan = planned
        if plan.calculate:
            storage = FactorStorage(project_root=root, data_root=cfg.data_root,
                                    factor_name=cfg.storage_factor_name or cfg.factor_name,
                                    threads=cfg.threads, memory_limit=cfg.memory_limit)
            try:
                _ensure_factor_output_writable(storage)
            finally:
                storage.close()
            source_begin = calculate(cfg, plan)
        messages, failed = refresh_outputs(cfg, plan)
        dates = _partition_dates(resolve_factor_data_root(project_root=root) / (cfg.storage_factor_name or cfg.factor_name))
        return UpdateResult(cfg.factor_name, "failed" if failed else "updated" if plan.calculate else "up_to_date",
                            begin=plan.begin, end=plan.end, before_end=plan.before_end,
                            after_end=dates[-1] if dates else None, source_begin=source_begin,
                            compute_batch_days=cfg.compute_batch_days if plan.calculate else None,
                            message="; ".join(messages))
    except BenchmarkRequired as exc:
        return UpdateResult(cfg.factor_name, "benchmark_required", message=str(exc))
    except Exception as exc:
        return UpdateResult(cfg.factor_name, "failed", begin=plan.begin if plan else None,
                            end=plan.end if plan else None, before_end=plan.before_end if plan else None,
                            message=f"{type(exc).__name__}: {exc}")


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
