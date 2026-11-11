"""Run transactional Calculate with the caller's batch and resource settings."""
from __future__ import annotations

import re
from pathlib import Path

from factor_protocol import resolve_factor_data_root
from factor_protocol.loader import _project_path

from ..models import UpdateConfig, UpdatePlan
from ..process import run_skill_script


def recover_calculation(cfg: UpdateConfig) -> None:
    root = Path(cfg.project_root)
    name = cfg.storage_factor_name or cfg.factor_name
    factor_data = resolve_factor_data_root(project_root=root)
    if cfg.dry_run or not any((factor_data / path).exists()
                             for path in ("_materialize_" + name, "." + name + ".previous")):
        return
    run_skill_script(root, "factor-calculate", "run_calculate.py",
                     ["--factor-name", cfg.factor_name, "--storage-factor-name", name,
                      "--project-root", root.as_posix(), "--data-root", str(cfg.data_root), "--recover-only"])


def calculate(cfg: UpdateConfig, plan: UpdatePlan) -> str | None:
    root = Path(cfg.project_root)
    temp = (_project_path(cfg.temp_directory, project_root=root) if cfg.temp_directory is not None
            else root / ".tmp" / "factor_update" / cfg.factor_name)
    arguments = ["--factor-name", cfg.factor_name, "--begin", plan.begin, "--end", plan.end,
                 "--data-root", str(cfg.data_root), "--factor-zoo-root", str(cfg.factor_zoo_root),
                 "--threads", str(cfg.threads), "--compute-batch-days", str(cfg.compute_batch_days),
                 "--temp-directory", temp.as_posix(), "--project-root", root.as_posix(),
                 "--require-values", "--no-readme-update"]
    for flag, value in (("--memory-limit", cfg.memory_limit),
                        ("--storage-factor-name", cfg.storage_factor_name), ("--factor-module", cfg.factor_module)):
        if value is not None:
            arguments.extend((flag, str(value)))
    result = run_skill_script(root, "factor-calculate", "run_calculate.py", arguments, capture_output=True)
    begins = re.findall(r"\bsource_begin\s*=\s*(\d{4}-\d{2}-\d{2})", result.stdout or "")
    return min(begins) if begins else None
