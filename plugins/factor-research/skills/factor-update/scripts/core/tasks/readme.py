"""Write existing evaluation and update reports without rerunning update tasks."""
from __future__ import annotations

from pathlib import Path

from ..models import UpdateConfig
from ..process import run_skill_script
from ..report import load_saved_report, preview_readme, update_readme_from_saved_result


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
