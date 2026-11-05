"""Freeze factor history by relative paths, modification times and contents."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence

from .artifacts import read_config, write_config
from .protocol import FactorPackage
from .storage import FactorStorage


def read_identity(factor_root: str | Path) -> dict[str, Any]:
    """Read the committed factor identity."""
    return read_config(Path(factor_root) / "identity.json")


def parquet_manifest(factor_root: str | Path) -> dict[str, dict[str, Any]]:
    """Read current file contents; no cached content hashes are trusted."""
    root = Path(factor_root)
    files: dict[str, dict[str, Any]] = {}
    paths = (
        path
        for partition in sorted(root.glob("date=*"))
        for path in sorted(partition.rglob("*.parquet"))
    )
    for path in paths:
        relative = path.relative_to(root).as_posix()
        stat = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        files[relative] = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": digest.hexdigest(),
        }
    return files


def manifest_digest(manifest: Mapping[str, Any], *, through: str | None = None) -> str:
    """Combine ordered relative paths, nanosecond mtimes and content hashes."""
    digest = hashlib.sha256()
    for relative, metadata in sorted(manifest.items()):
        partition_date = relative.split("/", 1)[0].removeprefix("date=")
        if through is None or partition_date <= through:
            digest.update(
                f"{relative}\0{metadata['mtime_ns']}\0{metadata['sha256']}\n".encode("utf-8")
            )
    return digest.hexdigest()


def _manifest_summary(manifest: Mapping[str, Any]) -> dict[str, Any]:
    dates = [relative.split("/", 1)[0].removeprefix("date=") for relative in manifest]
    return {
        "begin": min(dates, default=None),
        "end": max(dates, default=None),
        "sha256": manifest_digest(manifest),
    }


def identity_manifest_matches(factor_root: str | Path) -> bool:
    identity = read_identity(factor_root)
    stored = identity.get("manifest")
    if not isinstance(stored, Mapping) or not stored:
        return False
    current = parquet_manifest(factor_root)
    return bool(current) and _manifest_summary(current) == stored


def stored_identity_matches(storage: FactorStorage, *, fingerprint: str) -> bool | None:
    identity = read_identity(storage.factor_root)
    if not identity:
        return None
    return identity.get("fingerprint") == fingerprint and identity_manifest_matches(
        storage.factor_root
    )


def require_identity(
    factor_root: str | Path,
    *,
    fingerprint: str | None = None,
    columns: Sequence[str] | None = None,
) -> dict[str, Any]:
    root = Path(factor_root)
    identity = read_identity(root)
    if not identity or not identity_manifest_matches(root):
        raise ValueError(
            f"factor identity is missing or its Parquet manifest changed: {root}"
        )
    if fingerprint is not None and identity.get("fingerprint") != fingerprint:
        raise ValueError(
            f"factor identity does not match the calculation definition: {root}"
        )
    if columns is not None and not set(columns).issubset(identity.get("columns", ())):
        raise ValueError(
            f"factor identity does not cover the requested columns: {root}"
        )
    return identity


def write_identity(
    storage: FactorStorage,
    *,
    package: FactorPackage,
    factor_columns: Sequence[str],
    fingerprint: str,
    manifest: Mapping[str, Any] | None = None,
) -> None:
    if manifest is None:
        manifest = parquet_manifest(storage.factor_root)
    if not manifest:
        raise ValueError("cannot identify an empty factor dataset")
    write_config(
        storage.factor_root / "identity.json",
        {
            "factor_name": package.factor_name,
            "columns": list(factor_columns),
            "fingerprint": fingerprint,
            "manifest": _manifest_summary(manifest),
        },
    )
