"""Refresh the independent audit for a bounded materialized interval."""
from __future__ import annotations
from pathlib import Path
from factor_protocol import resolve_data_root
from factor_protocol.artifacts import resolve_output_root
from factor_protocol.loader import _project_path
from ..models import UpdateConfig
from ..process import run_skill_script

def refresh_validation(
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
    run_skill_script(
        project_root,
        "factor-validate",
        "run_validate.py",
        validation_args,
    )
    return (output_root / factor_name).as_posix()
