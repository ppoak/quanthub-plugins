"""Resolve package targets through the shared registry protocol."""
from __future__ import annotations

from pathlib import Path
from collections.abc import Mapping

import yaml

from factor_protocol import FactorPath, resolve_factor_columns, resolve_factor_schema_path
from factor_protocol.fingerprint import factor_definition_fingerprint
from factor_protocol.loader import load_module_from_path, resolve_factor_package
from factor_protocol.registry import load_factor_registry_entries


def select_packages(*, project_root: Path, factor_zoo_root: str | Path,
                    target: str) -> tuple[str, ...]:
    """Match a schema block first; otherwise resolve a factor package."""
    schema_path = resolve_factor_schema_path(project_root=project_root, factor_zoo_root=factor_zoo_root)
    payload = {}
    if schema_path.is_file():
        try:
            payload = yaml.safe_load(schema_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid factor registry YAML: {schema_path.as_posix()}") from exc
        if not isinstance(payload, Mapping):
            raise ValueError("factor registry root must be a mapping")
    schema_block = target if target in payload else None
    pins: dict[str, set[str]] = {}
    references: dict[str, set[str]] = {}
    if schema_block is None:
        if not (Path(factor_zoo_root) / target / "scripts" / "main.py").is_file():
            raise ValueError(f"target is neither a schema block nor a factor package: {target}")
        names = [target]
    else:
        entries = load_factor_registry_entries(field=schema_block, project_root=project_root,
                                               factor_zoo_root=factor_zoo_root)
        if not entries:
            raise ValueError(f"schema block is missing or empty: {schema_block}")
        for entry in entries:
            path = FactorPath.parse(entry.path)
            references.setdefault(path.table, set()).add(path.column)
            if entry.fingerprint:
                pins.setdefault(path.table, set()).add(entry.fingerprint)
        names = list(references)

    for name in names:
        package = resolve_factor_package(name, project_root=project_root, factor_zoo_root=factor_zoo_root)
        fingerprint = factor_definition_fingerprint(package)
        if pins.get(name) and pins[name] != {fingerprint}:
            raise ValueError(f"schema block {schema_block!r} package {name!r} fingerprint mismatch")
        module = load_module_from_path(package.module_path, project_root=project_root, factor_name=name)
        declared = set(resolve_factor_columns(module))
        if not callable(getattr(module, "calculate_factor", None)):
            raise ValueError(f"factor package {name!r} must define callable calculate_factor")
        if not references.get(name, set()).issubset(declared):
            raise ValueError(f"schema block {schema_block!r} references unknown columns in {name!r}")
        if schema_block == "active" and references[name] != declared:
            raise ValueError(f"active factor package {name!r} must include every declared output column")
    return tuple(names)
