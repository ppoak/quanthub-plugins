"""Parse factor schema data and validate active factor package declarations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

import yaml

from .config import resolve_factor_schema_path, resolve_factor_zoo_root
from .fingerprint import factor_definition_fingerprint
from .loader import (
    load_module_from_path,
    resolve_factor_package,
)
from .paths import FactorPath, validate_identifier
from .protocol import FactorPackage, resolve_factor_columns


_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class FactorRegistryEntry:
    """One registered factor column and its optional pinned package fingerprint."""

    path: str
    fingerprint: str | None = None


@dataclass(frozen=True)
class ActiveFactorPackage:
    """One active factor package and its explicit production columns."""

    package: str
    fingerprint: str
    columns: tuple[str, ...]


def _load_factor_registry_payload(
    *,
    project_root: str | Path,
    factor_zoo_root: str | Path,
) -> Mapping[str, object] | None:
    schema_path = resolve_factor_schema_path(
        project_root=project_root,
        factor_zoo_root=factor_zoo_root,
    )
    if not schema_path.exists():
        return None

    try:
        payload = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise ValueError(
            f"cannot read factor registry: {schema_path.as_posix()}"
        ) from exc
    except yaml.YAMLError as exc:
        raise ValueError(
            f"invalid factor registry YAML: {schema_path.as_posix()}"
        ) from exc

    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise ValueError("factor registry root must be a mapping")

    return payload


def _validate_fingerprint(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _FINGERPRINT.fullmatch(value):
        raise ValueError(
            f"{label} fingerprint must be a 64-character lowercase SHA-256 hex digest"
        )
    return value


def _load_reference_entries(
    *,
    field: str,
    entries: object,
) -> tuple[FactorRegistryEntry, ...]:
    """Load a core/reference path list."""

    if entries is None:
        return ()
    if isinstance(entries, (str, bytes)) or not isinstance(entries, (list, tuple)):
        raise ValueError(f"factor registry {field} must be a list")

    references: list[FactorRegistryEntry] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if isinstance(entry, str):
            raw_path = entry
            raw_fingerprint = None
        elif isinstance(entry, Mapping):
            raw_path = entry.get("path")
            raw_fingerprint = entry.get("fingerprint")
        else:
            raw_path = None
            raw_fingerprint = None

        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError(
                f"factor registry {field}[{index}] must provide a factor path"
            )
        try:
            reference = FactorPath.parse(raw_path).label
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid factor registry {field}[{index}]: {raw_path!r}"
            ) from exc

        if raw_fingerprint is not None:
            raw_fingerprint = _validate_fingerprint(
                raw_fingerprint,
                label=f"factor registry {field}[{index}]",
            )
        if reference not in seen:
            references.append(
                FactorRegistryEntry(
                    path=reference,
                    fingerprint=raw_fingerprint,
                )
            )
            seen.add(reference)
    return tuple(references)


def _load_active_packages(entries: object) -> tuple[ActiveFactorPackage, ...]:
    if entries is None:
        return ()
    if isinstance(entries, (str, bytes)) or not isinstance(entries, (list, tuple)):
        raise ValueError("factor registry active must be a list")

    packages: list[ActiveFactorPackage] = []
    seen_packages: set[str] = set()
    seen_paths: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise ValueError(
                f"factor registry active[{index}] must be a package mapping"
            )

        raw_package = entry.get("package")
        if not isinstance(raw_package, str) or not raw_package.strip():
            raise ValueError(
                f"factor registry active[{index}] must provide a package name"
            )
        try:
            package = validate_identifier(raw_package.strip(), "factor_package")
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid factor registry active[{index}] package: {raw_package!r}"
            ) from exc
        if package in seen_packages:
            raise ValueError(f"duplicate active factor package: {package}")

        fingerprint = _validate_fingerprint(
            entry.get("fingerprint"),
            label=f"factor registry active[{index}]",
        )
        raw_columns = entry.get("columns")
        if isinstance(raw_columns, (str, bytes)) or not isinstance(
            raw_columns, (list, tuple)
        ):
            raise ValueError(
                f"factor registry active[{index}] columns must be a non-empty list"
            )
        if not raw_columns:
            raise ValueError(
                f"factor registry active[{index}] columns must be a non-empty list"
            )

        columns: list[str] = []
        seen_package_columns: set[str] = set()
        for column_index, raw_column in enumerate(raw_columns):
            if not isinstance(raw_column, str) or not raw_column.strip():
                raise ValueError(
                    f"factor registry active[{index}].columns[{column_index}] "
                    "must be a factor column name"
                )
            column = raw_column.strip()
            try:
                path = FactorPath(package, column).label
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"invalid factor registry active[{index}].columns[{column_index}]: "
                    f"{raw_column!r}"
                ) from exc
            if column in seen_package_columns:
                raise ValueError(f"duplicate active factor column: {path}")
            if path in seen_paths:
                raise ValueError(f"duplicate active factor path: {path}")
            columns.append(column)
            seen_package_columns.add(column)
            seen_paths.add(path)

        packages.append(
            ActiveFactorPackage(
                package=package,
                fingerprint=fingerprint,
                columns=tuple(columns),
            )
        )
        seen_packages.add(package)
    return tuple(packages)


def load_active_factor_packages(
    *,
    project_root: str | Path,
    factor_zoo_root: str | Path | None = None,
) -> tuple[ActiveFactorPackage, ...]:
    """Load active package-level fingerprints and explicit column allowlists."""

    loaded = _load_factor_registry_payload(
        project_root=project_root,
        factor_zoo_root=factor_zoo_root,
    )
    if loaded is None:
        return ()
    return _load_active_packages(loaded.get("active"))


def load_factor_registry_entries(
    *,
    field: str,
    project_root: str | Path,
    factor_zoo_root: str | Path | None = None,
) -> tuple[FactorRegistryEntry, ...]:
    """Load listed factor paths while preserving package fingerprint metadata."""

    loaded = _load_factor_registry_payload(
        project_root=project_root,
        factor_zoo_root=factor_zoo_root,
    )
    if loaded is None:
        return ()
    if field == "active":
        return tuple(
            FactorRegistryEntry(
                path=f"{package.package}.{column}",
                fingerprint=package.fingerprint,
            )
            for package in _load_active_packages(loaded.get("active"))
            for column in package.columns
        )
    return _load_reference_entries(field=field, entries=loaded.get(field))


def _load_factor_registry_references(
    *,
    field: str,
    project_root: str | Path,
    factor_zoo_root: str | Path | None = None,
) -> tuple[str, ...]:
    """Load listed factor paths from one configured registry field."""

    return tuple(
        entry.path
        for entry in load_factor_registry_entries(
            field=field,
            project_root=project_root,
            factor_zoo_root=factor_zoo_root,
        )
    )


def load_core_factor_references(
    *,
    project_root: str | Path,
    factor_zoo_root: str | Path | None = None,
) -> tuple[str, ...]:
    """Load core factor paths from the ``core`` registry field."""

    return _load_factor_registry_references(
        field="core",
        project_root=project_root,
        factor_zoo_root=factor_zoo_root,
    )


def load_active_factor_references(
    *,
    project_root: str | Path,
    factor_zoo_root: str | Path | None = None,
) -> tuple[str, ...]:
    """Load production factor paths from the ``active`` registry field."""

    return _load_factor_registry_references(
        field="active",
        project_root=project_root,
        factor_zoo_root=factor_zoo_root,
    )


def list_factor_names(
    *,
    project_root: str | Path,
    factor_zoo_root: str | Path | None = None,
) -> list[str]:
    root = resolve_factor_zoo_root(
        project_root=project_root,
        factor_zoo_root=factor_zoo_root,
    )
    names: list[str] = []
    for factor_root in sorted(root.iterdir() if root.exists() else ()):
        module_path = factor_root / "scripts" / "main.py"
        if factor_root.is_dir() and module_path.exists():
            names.append(factor_root.name)
    return names


def list_active_factor_paths(
    *,
    project_root: str | Path,
    factor_zoo_root: str | Path | None = None,
) -> tuple[FactorPath, ...]:
    """Return active factor paths after checking source columns and fingerprints."""

    schema_path = resolve_factor_schema_path(
        project_root=project_root,
        factor_zoo_root=factor_zoo_root,
    )
    if not schema_path.exists():
        return ()

    available = set(
        list_factor_names(
            project_root=project_root,
            factor_zoo_root=factor_zoo_root,
        )
    )
    packages = load_active_factor_packages(
        project_root=project_root,
        factor_zoo_root=factor_zoo_root,
    )
    selected: list[FactorPath] = []
    seen: set[str] = set()
    module_cache: dict[str, Any] = {}
    package_cache: dict[str, FactorPackage] = {}
    for registry_entry in packages:
        package_name = registry_entry.package
        if package_name not in available:
            raise ValueError(
                f"active factor source {package_name!r} has no scripts/main.py"
            )
        package = package_cache.get(package_name)
        if package is None:
            package = resolve_factor_package(
                package_name,
                project_root=project_root,
                factor_zoo_root=factor_zoo_root,
            )
            package_cache[package_name] = package
        current_fingerprint = factor_definition_fingerprint(package)
        if registry_entry.fingerprint != current_fingerprint:
            raise ValueError(
                f"active factor package {package_name!r} fingerprint mismatch: "
                f"schema={registry_entry.fingerprint}, current={current_fingerprint}; "
                "verify the implementation and refresh the active schema fingerprint"
            )
        module = module_cache.get(package_name)
        if module is None:
            module = load_module_from_path(
                package.module_path,
                project_root=project_root,
                factor_name=package_name,
            )
            module_cache[package_name] = module
        available_columns = resolve_factor_columns(module)
        unknown_columns = [
            column
            for column in registry_entry.columns
            if column not in available_columns
        ]
        if unknown_columns:
            raise ValueError(
                f"active factor package {package_name!r} columns are not declared by "
                f"FACTOR_COLUMNS ({', '.join(unknown_columns)}); available columns: "
                f"{', '.join(available_columns)}"
            )
        for column in registry_entry.columns:
            factor_path = FactorPath(package_name, column)
            if factor_path.label not in seen:
                selected.append(factor_path)
                seen.add(factor_path.label)
    return tuple(selected)


def active_factor_columns_by_name(
    *,
    project_root: str | Path,
    factor_zoo_root: str | Path | None = None,
) -> dict[str, tuple[str, ...]]:
    """Return active factor columns grouped by factor source name."""

    grouped: dict[str, list[str]] = {}
    for path in list_active_factor_paths(
        project_root=project_root,
        factor_zoo_root=factor_zoo_root,
    ):
        grouped.setdefault(path.table, []).append(path.column)
    return {name: tuple(columns) for name, columns in grouped.items()}


def list_active_factor_names(
    *,
    project_root: str | Path,
    factor_zoo_root: str | Path | None = None,
) -> list[str]:
    """Return factor sources that have at least one active column.

    ``list_factor_names`` remains the complete package discovery API used by
    audits and research tooling.  The nightly scheduler and aggregate
    evaluation use only the explicit ``active`` registry field.  When an
    isolated custom factor root has no schema, preserve historical discovery
    behaviour for ad-hoc callers.
    """

    schema_path = resolve_factor_schema_path(
        project_root=project_root,
        factor_zoo_root=factor_zoo_root,
    )
    if not schema_path.exists():
        return sorted(
            list_factor_names(
                project_root=project_root,
                factor_zoo_root=factor_zoo_root,
            )
        )
    return list(
        dict.fromkeys(
            path.table
            for path in list_active_factor_paths(
                project_root=project_root,
                factor_zoo_root=factor_zoo_root,
            )
        )
    )


__all__ = [
    "ActiveFactorPackage",
    "FactorRegistryEntry",
    "active_factor_columns_by_name",
    "list_active_factor_names",
    "list_active_factor_paths",
    "list_factor_names",
    "load_active_factor_packages",
    "load_active_factor_references",
    "load_core_factor_references",
    "load_factor_registry_entries",
]
