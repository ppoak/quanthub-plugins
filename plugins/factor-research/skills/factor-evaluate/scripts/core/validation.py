"""Read Factor Validate's audit and handoff for package evaluations."""
import csv
import importlib.util
from pathlib import Path

from factor_protocol.artifacts import read_config


def require_validation(column_dir: Path, *, end: str | None = None) -> None:
    config = read_config(column_dir / "config.json")
    validation = config.get("validation")
    audit = column_dir / "audit.csv"
    if not audit.is_file() or audit.stat().st_size == 0:
        raise ValueError(f"passed validation audit is required: {column_dir}")
    if isinstance(validation, dict):
        if "status" in validation:
            through = validation.get("data", {}).get("through") if validation["status"] == "pass" else None
        else:
            through = validation.get("through")
    elif validation is None and config.get("files"):
        with audit.open(newline="", encoding="utf-8") as stream:
            through = max((row.get("date") or "" for row in csv.DictReader(stream)), default="")
    else:
        through = None
    if not through:
        raise ValueError(f"passed validation audit is required: {column_dir}")
    if end is not None and through < end:
        raise ValueError(f"validation ends at {through}, before {end}: {column_dir}")


def check_package_evaluation(column_dir, *, factor_name, project_root, factor_source=None):
    """Check the complete metric bundle and its validation prerequisite."""
    # Factor Publish loads this module by path.
    spec = importlib.util.spec_from_file_location("factor_metric_check", Path(__file__).with_name("check.py"))
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    config = checker.check_evaluation_config(
        column_dir, factor_name=factor_name, project_root=project_root, factor_source=factor_source,
    )
    require_validation(column_dir)
    return config
