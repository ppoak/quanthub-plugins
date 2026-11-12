"""Adapt Calculate, Validate, Evaluate and README stages through isolated skill processes."""
from __future__ import annotations

import re
import subprocess
import sys
from factor_protocol import resolve_data_root, resolve_factor_data_root
from factor_protocol.artifacts import resolve_output_root
from factor_protocol.loader import _project_path
from pathlib import Path
from typing import Sequence

from .config import UpdateConfig, UpdatePlan
from .report import load_saved_report, preview_readme, update_readme_from_saved_result


def run_skill_script(
    project_root: Path,
    skill_name: str,
    script_name: str,
    arguments: Sequence[str],
    *,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a terminal skill in its own process and release its working memory."""

    project_root = project_root.resolve()
    skill_root = Path(__file__).resolve().parents[3]
    script_path = skill_root / skill_name / "scripts" / script_name
    if not script_path.is_file():
        raise FileNotFoundError(f"skill script not found: {script_path.as_posix()}")
    command = [sys.executable, script_path.as_posix(), *arguments]
    if not capture_output:
        return subprocess.run(
            command,
            cwd=project_root,
            check=True,
            text=True,
        )

    process = subprocess.Popen(
        command,
        cwd=project_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    output: list[str] = []
    if process.stdout is not None:
        for line in process.stdout:
            output.append(line)
            sys.stdout.write(line)
            sys.stdout.flush()
        process.stdout.close()
    return_code = process.wait()
    completed_output = "".join(output)
    if return_code != 0:
        raise subprocess.CalledProcessError(
            return_code,
            command,
            output=completed_output,
        )
    return subprocess.CompletedProcess(
        command,
        return_code,
        stdout=completed_output,
        stderr="",
    )


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


def write_readme(cfg: UpdateConfig) -> None:
    root = Path(cfg.project_root)
    preview_readme(project_root=root, factor_zoo_root=cfg.factor_zoo_root, factor_name=cfg.factor_name)
    report = load_saved_report(root, cfg.factor_name)
    arguments = ["--factor-package", cfg.factor_name, "--project-root", root.as_posix(),
                 "--data-root", str(cfg.data_root), "--factor-zoo-root", str(cfg.factor_zoo_root),
                 "--output-dir", cfg.factor_output_root, "--operation", "readme"]
    if isinstance(report.get("threads"), int):
        arguments.extend(("--threads", str(report["threads"])))
    if report.get("memory_limit") and report["memory_limit"] != "—":
        arguments.extend(("--memory-limit", report["memory_limit"]))
    run_skill_script(root, "factor-evaluate", "run_evaluate.py", arguments)
    result = update_readme_from_saved_result(project_root=root, factor_zoo_root=cfg.factor_zoo_root,
                                            factor_name=cfg.factor_name)
    print(f"Factor README: {result[0].as_posix()}", flush=True)
