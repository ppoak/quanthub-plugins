"""Independent validation prerequisite checks for the factor-package workflow."""
from pathlib import Path
from types import SimpleNamespace
import importlib.util

from factor_protocol import resolve_factor_data_root, resolve_factor_package
from factor_protocol.artifacts import read_config, resolve_output_root, dataset_manifest, manifests_match
from factor_protocol.fingerprint import factor_definition_fingerprint
from factor_protocol.identity import read_identity


def require_validation(cfg, columns, fingerprint, *, committed_manifest=None):
    root = Path(cfg.project_root or Path.cwd())
    source = cfg.storage_factor_name or cfg.factor_name
    factor_root = resolve_factor_data_root(project_root=root) / source
    committed = committed_manifest or read_identity(factor_root)["manifest"]
    end = min(cfg.end or committed["end"], committed["end"])
    output = resolve_output_root(cfg.output_dir, project_root=root) / cfg.factor_name
    checked = {}
    for column in columns:
        directory = output / column
        config = read_config(directory / "config.json")
        factor = config.get("factor", {})
        pending = config.get("validation")
        if isinstance(pending, dict) and "status" in pending:
            proof = ({"fingerprint": factor.get("fingerprint"), **pending.get("data", {})}
                     if pending["status"] == "pass" else {})
        elif pending is None:
            proof = factor if config.get("files") else {}
        else:
            proof = pending if isinstance(pending, dict) else {}
        audit = directory / "audit.csv"
        if not audit.is_file() or audit.stat().st_size == 0 or not proof:
            raise ValueError(f"passed validation audit is required: {directory}")
        if (factor.get("name") != cfg.factor_name or factor.get("column") != column
                or factor.get("dataset", cfg.factor_name) != source
                or proof.get("fingerprint") != fingerprint):
            raise ValueError(f"validation input or calculation fingerprint differs: {directory}")
        through = proof.get("through")
        if not through or through < end or (committed_manifest is not None and through != committed["end"]):
            raise ValueError(f"validation is stale: {directory}")
        if through not in checked:
            checked[through] = ({"through": through, "sha256": committed["sha256"]}
                                if through == committed["end"] else
                                dataset_manifest(factor_root.parent, source, through=through))
        if not manifests_match(proof, checked[through]):
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
    identity = read_identity(resolve_factor_data_root(project_root=project_root) / source)
    if (identity.get("factor_name") != factor_name or identity.get("fingerprint") != fingerprint
            or config["factor"].get("fingerprint") != fingerprint
            or column_dir.name not in identity.get("columns", ())):
        raise ValueError(f"factor identity differs from evaluated package: {column_dir}")
    cfg = SimpleNamespace(project_root=project_root, storage_factor_name=source,
        factor_name=factor_name, end=config["factor"]["through"], output_dir=column_dir.parent.parent)
    require_validation(cfg, (column_dir.name,), fingerprint)
    return config
