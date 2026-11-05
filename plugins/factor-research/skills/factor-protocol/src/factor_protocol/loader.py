from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any, Sequence

from .protocol import FactorPackage, resolve_factor_columns
from .config import DEFAULT_FACTOR_ZOO_ROOT as DEFAULT_FACTOR_ZOO_ROOT
from .config import resolve_factor_zoo_root


def _project_path(path: str | Path, *, project_root: Path) -> Path:
    value = Path(path)
    return value if value.is_absolute() else project_root / value


def resolve_factor_package(
    factor_name: str,
    *,
    project_root: str | Path,
    factor_module: str | None = None,
    output_dir: str | Path | None = None,
    factor_zoo_root: str | Path | None = None,
) -> FactorPackage:
    root = Path(project_root)
    factor_zoo_base = resolve_factor_zoo_root(
        project_root=root,
        factor_zoo_root=factor_zoo_root,
    )
    factor_root = factor_zoo_base / factor_name
    module_path = (
        _project_path(factor_module, project_root=root)
        if factor_module
        else factor_root / "scripts" / "main.py"
    )
    runtime_dir = (
        _project_path(output_dir, project_root=root)
        if output_dir is not None
        else root / ".tmp" / "factor_runtime" / factor_name
    )
    return FactorPackage(
        factor_name=factor_name,
        factor_root=factor_root,
        module_path=module_path,
        dist_dir=runtime_dir,
        fig_dir=runtime_dir / "figs",
    )


def load_module_from_path(
    module_path: str | Path,
    *,
    project_root: str | Path,
    factor_name: str | None = None,
) -> Any:
    root = Path(project_root)
    path = _project_path(module_path, project_root=root)
    if not path.exists():
        raise FileNotFoundError(f"factor module not found: {path.as_posix()}")

    package_name = _qualified_factor_module_name(path, project_root=root)
    if package_name is not None:
        importlib.invalidate_caches()
        return importlib.import_module(package_name)

    safe_name = factor_name or path.stem
    package_name = f"factor_zoo_pkg_{safe_name}_{abs(hash(path.as_posix()))}"
    package = types.ModuleType(package_name)
    package.__path__ = [path.parent.as_posix()]  # type: ignore[attr-defined]
    package.__package__ = package_name
    sys.modules[package_name] = package
    module_name = f"{package_name}.{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load module from {path.as_posix()}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _qualified_factor_module_name(path: Path, *, project_root: Path) -> str | None:
    """Resolve repository factor modules without mutating ``sys.path``."""

    try:
        search_roots = {Path(entry).resolve() for entry in sys.path if entry}
    except OSError:
        search_roots = set()
    if project_root.resolve() not in search_roots:
        return None
    try:
        relative = path.resolve().relative_to(project_root.resolve())
    except ValueError:
        return None
    if len(relative.parts) < 4 or relative.parts[-2:] != ("scripts", "main.py"):
        return None
    if not all(part.isidentifier() for part in relative.parts[:-1]):
        return None
    return ".".join(relative.with_suffix("").parts)


def select_factor_columns(
    module: Any, requested: Sequence[str] | None
) -> tuple[str, ...]:
    """Resolve an optional ordered factor-column selection."""

    available = tuple(resolve_factor_columns(module))
    if requested is None:
        return available
    selected = tuple(str(column) for column in requested)
    if not selected:
        return available
    if len(set(selected)) != len(selected):
        duplicates = sorted(
            {column for column in selected if selected.count(column) > 1}
        )
        raise ValueError(f"factor_columns contains duplicates: {', '.join(duplicates)}")
    unknown = [column for column in selected if column not in available]
    if unknown:
        raise ValueError(
            "unknown factor columns: "
            f"{', '.join(unknown)}; available columns: {', '.join(available)}"
        )
    return selected
