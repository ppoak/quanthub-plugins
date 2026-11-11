"""Coordinate validation and evaluation tasks after planning or materialization."""
from __future__ import annotations

from pathlib import Path

from factor_protocol import resolve_factor_data_root

from .models import UpdateConfig, UpdatePlan
from .sources import _partition_dates
from .state import _clear_refresh_failure_marker, _write_refresh_failure_marker
from .tasks.evaluate import refresh_evaluation
from .tasks.validate import refresh_validation


def refresh_outputs(cfg: UpdateConfig, plan: UpdatePlan) -> tuple[list[str], bool]:
    root = Path(cfg.project_root)
    dates = _partition_dates(resolve_factor_data_root(project_root=root) / (cfg.storage_factor_name or cfg.factor_name))
    if not dates:
        _clear_refresh_failure_marker(root, cfg.factor_name)
        return ["calculation completed with no eligible rows; validation/evaluation await factor data"], False
    messages: list[str] = []
    failed = False
    if plan.refresh_validation:
        try:
            path = refresh_validation(factor_name=cfg.factor_name, begin=dates[-5] if len(dates) >= 5 else dates[0],
                                      end=dates[-1], cfg=cfg, project_root=root)
            messages.append(f"validation={path}")
        except Exception as exc:
            failed = True
            messages.append(f"validation_failed={type(exc).__name__}: {exc}")
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
