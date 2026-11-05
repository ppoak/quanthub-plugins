from __future__ import annotations

import os
from pathlib import Path

DEFAULT_FACTOR_ZOO_ROOT = "factor_zoo"
DEFAULT_FACTOR_SCHEMA_FILENAME = "schema.yaml"
DEFAULT_DATA_ROOT = "data"
DEFAULT_ARTIFACTS_ROOT = "artifacts"


def _dotenv_value(path: Path, name: str) -> str | None:
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        if value.startswith("export "):
            value = value[7:].lstrip()
        key, separator, raw_value = value.partition("=")
        if not separator or key.strip() != name:
            continue
        raw_value = raw_value.strip()
        if raw_value and raw_value[0] in "\"'":
            quote = raw_value[0]
            end = raw_value.find(quote, 1)
            if end >= 0:
                return raw_value[1:end]
        return raw_value.split(" #", maxsplit=1)[0].strip()
    return None


def _configured_value(
    name: str,
    *,
    project_root: str | Path,
    default: str,
) -> str:
    root = Path(project_root).expanduser().resolve()
    environment_value = os.environ.get(name, "").strip()
    dotenv_value = _dotenv_value(root / ".env", name)
    return (
        environment_value
        or (str(dotenv_value).strip() if dotenv_value else "")
        or default
    )


def _configured_path(
    name: str,
    *,
    project_root: str | Path,
    default: str,
) -> Path:
    root = Path(project_root).expanduser().resolve()
    path = Path(
        _configured_value(
            name,
            project_root=root,
            default=default,
        )
    ).expanduser()
    return path if path.is_absolute() else root / path


def resolve_factor_zoo_root(
    *,
    project_root: str | Path,
    factor_zoo_root: str | Path | None = None,
) -> Path:
    configured = factor_zoo_root
    if configured is None:
        return _configured_path(
            "FACTORZOO",
            project_root=project_root,
            default=DEFAULT_FACTOR_ZOO_ROOT,
        )
    path = Path(configured).expanduser()
    if path.is_absolute():
        return path
    return Path(project_root).expanduser().resolve() / path


def resolve_factor_schema_path(
    *,
    project_root: str | Path,
    factor_zoo_root: str | Path | None = None,
) -> Path:
    root = Path(project_root).expanduser().resolve()
    zoo_root = resolve_factor_zoo_root(
        project_root=root,
        factor_zoo_root=factor_zoo_root,
    )
    filename = Path(
        _configured_value(
            "FACTORZOO_SCHEMA",
            project_root=root,
            default=DEFAULT_FACTOR_SCHEMA_FILENAME,
        )
    ).expanduser()
    return filename if filename.is_absolute() else zoo_root / filename


def resolve_data_root(
    *,
    project_root: str | Path,
    data_root: str | Path | None = None,
) -> Path:
    configured = data_root
    if configured is None:
        return _configured_path(
            "DATA",
            project_root=project_root,
            default=DEFAULT_DATA_ROOT,
        )
    path = Path(configured).expanduser()
    if path.is_absolute():
        return path
    return Path(project_root).expanduser().resolve() / path


def resolve_artifacts_root(*, project_root: str | Path, artifacts_root: str | Path | None = None) -> Path:
    if artifacts_root is not None:
        path = Path(artifacts_root).expanduser()
        return path if path.is_absolute() else Path(project_root).resolve() / path
    return _configured_path(
        "ARTIFACTS",
        project_root=project_root,
        default=DEFAULT_ARTIFACTS_ROOT,
    )


def resolve_factor_data_root(*, project_root: str | Path, artifacts_root: str | Path | None = None) -> Path:
    return resolve_artifacts_root(project_root=project_root, artifacts_root=artifacts_root) / "factor_data"
