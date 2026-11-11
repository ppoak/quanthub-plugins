"""Refresh evaluations independently of update report writeback."""
from __future__ import annotations

from pathlib import Path

from factor_protocol.loader import _project_path

from ..models import UpdateConfig
from ..process import run_skill_script


def refresh_evaluation(cfg: UpdateConfig, *, end: str) -> None:
    root = Path(cfg.project_root)
    temp = (_project_path(cfg.temp_directory, project_root=root) / "evaluation"
            if cfg.temp_directory is not None
            else root / ".tmp" / "factor_update" / (cfg.storage_factor_name or cfg.factor_name) / "evaluation")
    arguments = ["--refresh", "--factor-package", cfg.factor_name, "--project-root", root.as_posix(),
                 "--data-root", str(cfg.data_root), "--factor-zoo-root", str(cfg.factor_zoo_root),
                 "--output-dir", cfg.factor_output_root, "--threads", str(cfg.threads),
                 "--memory-limit", cfg.memory_limit, "--temp-directory", temp.as_posix(), "--end", end]
    if cfg.storage_factor_name:
        arguments.extend(("--storage-factor-name", cfg.storage_factor_name))
    for operation in ("evaluate", "check", "plot"):
        arguments.extend(("--operation", operation))
    run_skill_script(root, "factor-evaluate", "run_evaluate.py", arguments)
