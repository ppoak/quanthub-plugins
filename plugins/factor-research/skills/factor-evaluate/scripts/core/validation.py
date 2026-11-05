"""Independent validation prerequisite checks for the factor-package workflow."""
from pathlib import Path
from types import SimpleNamespace
import importlib.util

from factor_protocol import resolve_factor_data_root, resolve_factor_package
from factor_protocol.artifacts import read_config, resolve_output_root, dataset_manifest, manifests_match
from factor_protocol.fingerprint import factor_definition_fingerprint
from factor_protocol.identity import require_identity


def require_validation(cfg, columns, fingerprint):
    root = Path(cfg.project_root or Path.cwd())
    source = cfg.storage_factor_name or cfg.factor_name
    data = resolve_factor_data_root(project_root=root)
    current = dataset_manifest(data, source, through=cfg.end)
    output = resolve_output_root(cfg.output_dir, project_root=root) / cfg.factor_name
    for column in columns:
        directory = output / column
        config = read_config(directory / "config.json")
        factor, validation = config.get("factor", {}), config.get("validation", {})
        if not (directory / "audit.csv").is_file() or validation.get("status") != "pass":
            raise ValueError(f"passed validation audit is required: {directory}")
        if (factor.get("name") != cfg.factor_name or factor.get("column") != column
                or factor.get("dataset", cfg.factor_name) != source
                or factor.get("fingerprint") != fingerprint):
            raise ValueError(f"validation input or calculation fingerprint differs: {directory}")
        snapshot = validation.get("data", {})
        through = snapshot.get("through")
        if (not through or not current["maximum_date"] or through < current["maximum_date"]
                or not manifests_match(snapshot, dataset_manifest(data, source, through=through))):
            raise ValueError(f"validation is stale: {directory}")


def check_package_evaluation(column_dir, *, factor_name, project_root, factor_source=None, **kwargs):
    """Check generic metrics and package prerequisites as separate steps."""
    # Loading by path also supports the publication script's standalone module loader.
    spec = importlib.util.spec_from_file_location("factor_metric_check", Path(__file__).with_name("check.py"))
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    config = checker.check_evaluation_config(column_dir, factor_name=factor_name,
        project_root=project_root, factor_source=factor_source, **kwargs)
    source = factor_source or config["factor"].get("dataset", factor_name)
    package = resolve_factor_package(factor_name, project_root=project_root)
    fingerprint = factor_definition_fingerprint(package)
    identity = require_identity(resolve_factor_data_root(project_root=project_root) / source,
        fingerprint=fingerprint, columns=(column_dir.name,))
    if identity.get("factor_name") != factor_name:
        raise ValueError("factor identity differs from package")
    cfg = SimpleNamespace(project_root=project_root, storage_factor_name=source,
        factor_name=factor_name, end=config["evaluation"]["requested_end"], output_dir=column_dir.parent.parent)
    require_validation(cfg, (column_dir.name,), fingerprint)
    return config
