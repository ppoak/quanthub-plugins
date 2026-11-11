"""Orchestrate a single package without implementing individual update stages."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from factor_protocol import FactorStorage, resolve_data_root, resolve_factor_zoo_root, resolve_factor_data_root

from .models import UpdateConfig, UpdatePlan, UpdateResult
from .planning import plan_update
from .preflight import _ensure_factor_output_writable
from .refresh import refresh_outputs
from .resources import BenchmarkRequired, configure_resources
from .sources import _partition_dates
from .tasks.calculate import calculate, recover_calculation


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
