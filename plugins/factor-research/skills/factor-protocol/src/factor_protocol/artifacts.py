from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .config import DEFAULT_ARTIFACTS_ROOT, resolve_artifacts_root


def resolve_output_root(
    output_root: str | Path,
    *,
    project_root: str | Path,
) -> Path:
    root = Path(output_root)
    if root.is_absolute():
        return root
    artifacts_anchor = Path(DEFAULT_ARTIFACTS_ROOT)
    if root.parts and root.parts[0] == artifacts_anchor.name:
        suffix = Path(*root.parts[1:])
        return resolve_artifacts_root(project_root=project_root) / suffix
    return Path(project_root) / root


def factor_column_output(
    output_root: str | Path,
    factor_name: str,
    factor_column: str,
    *,
    project_root: str | Path,
) -> Path:
    return (
        resolve_output_root(output_root, project_root=project_root)
        / factor_name
        / factor_column
    )


def read_config(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if not source.exists():
        return {}
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"invalid factor artifact config: {source.as_posix()}"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError(
            f"factor artifact config must be an object: {source.as_posix()}"
        )
    return payload


def write_config(path: str | Path, payload: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    serialized = dict(payload)
    temporary.write_text(
        json.dumps(serialized, ensure_ascii=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


def dataset_manifest(
    data_root: str | Path,
    dataset: str,
    *,
    through: str | None = None,
) -> dict[str, Any]:
    """Summarize a dataset snapshot using its identity or source file metadata."""

    root = Path(data_root) / dataset
    if (root / "identity.json").is_file():
        from .identity import manifest_digest, parquet_manifest, read_identity

        # Reject malformed identity files before taking a content snapshot.
        read_identity(root)
        inventory = parquet_manifest(root)
        selected = {
            relative: metadata
            for relative, metadata in inventory.items()
            if through is None or relative.split("/", 1)[0][5:] <= through
        }
        dates = [relative.split("/", 1)[0][5:] for relative in selected]
        return {
            "dataset": dataset,
            "through": through,
            "files": len(selected),
            "bytes": sum(item["size"] for item in selected.values()),
            "minimum_date": min(dates) if dates else None,
            "maximum_date": max(dates) if dates else None,
            "sha256": manifest_digest(selected),
        }
    digest = hashlib.sha256()
    files = 0
    total_bytes = 0
    minimum_date: str | None = None
    maximum_date: str | None = None
    if root.exists():
        partitions = sorted(path for path in root.glob("date=*") if path.is_dir())
        for partition in partitions:
            partition_date = partition.name[5:]
            if through is not None and partition_date > through:
                continue
            for path in sorted(partition.rglob("*.parquet")):
                stat = path.stat()
                relative = path.relative_to(root).as_posix()
                digest.update(
                    f"{relative}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode("utf-8")
                )
                files += 1
                total_bytes += int(stat.st_size)
                minimum_date = (
                    partition_date
                    if minimum_date is None
                    else min(minimum_date, partition_date)
                )
                maximum_date = (
                    partition_date
                    if maximum_date is None
                    else max(maximum_date, partition_date)
                )
    return {
        "dataset": dataset,
        "through": through,
        "files": files,
        "bytes": total_bytes,
        "minimum_date": minimum_date,
        "maximum_date": maximum_date,
        "sha256": digest.hexdigest(),
    }


def manifest_reference(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only the date boundary and digest in an artifact configuration."""
    return {"through": manifest["through"], "sha256": manifest["sha256"]}


def manifests_match(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    keys = ("through", "sha256")
    return all(left.get(key) == right.get(key) for key in keys)


def stage_column_directory(destination: str | Path) -> Path:
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    # The Windows path limit includes the temporary config filename.
    # Long factor columns (for example ``day_night_composite_*``) can fit in
    # the final directory but exceed MAX_PATH once the old staging prefix and
    # ``.config.json.<uuid>.tmp`` are appended.  Use a short sibling name when
    # the worst-case config path would approach the 260-character boundary.
    long_staging = target.parent / f".{target.name}.staging-{uuid4().hex}"
    short_staging = target.parent / f".staging-{uuid4().hex}"
    config_probe_name = ".config.json." + ("0" * 32) + ".tmp"
    long_probe = long_staging / config_probe_name
    staging = short_staging if len(str(long_probe)) >= 220 else long_staging
    try:
        staging.mkdir(parents=False, exist_ok=False)
        return staging
    except PermissionError:
        # Some managed Windows workspaces deny creation of both a sibling and
        # a child staging directory under an existing output column.  Stage
        # on the user-writable system temporary volume instead;
        # ``replace_column_directory`` performs a rollback-safe file-level
        # swap into the existing target directory.
        if not target.is_dir():
            raise
        return Path(tempfile.mkdtemp(prefix=f"{target.name}.staging-"))


def replace_column_directory(staging: str | Path, destination: str | Path) -> None:
    """Swap a fully staged column directory while retaining rollback safety."""

    staged = Path(staging)
    target = Path(destination)
    if staged.parent.resolve() != target.parent.resolve():
        if target.is_dir():
            _replace_in_place_from_staging(staged, target)
            return
        raise ValueError("staging and destination must have the same parent")
    if not staged.is_dir():
        raise FileNotFoundError(staged)
    backup = target.parent / f".{target.name}.backup-{uuid4().hex}"
    moved_existing = False
    copied_existing = False
    try:
        if target.exists():
            try:
                target.replace(backup)
                moved_existing = True
            except PermissionError:
                # Windows can deny renaming a populated directory even when
                # the caller has Modify permission on the parent (notably in
                # managed/sandboxed workspaces).  Keep the same rollback
                # guarantee by copying the old column before removing it;
                # the staged directory is still swapped in only after the
                # old contents have been preserved.
                shutil.copytree(target, backup)
                copied_existing = True
                shutil.rmtree(target)
        staged.replace(target)
    except Exception:
        if moved_existing and backup.exists() and not target.exists():
            backup.replace(target)
        elif copied_existing and backup.exists() and not target.exists():
            shutil.copytree(backup, target)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def _replace_in_place_from_staging(staged: Path, target: Path) -> None:
    """Commit an external staging directory into an existing target."""

    rollback = staged / f".rollback-{uuid4().hex}"
    rollback.mkdir(parents=False, exist_ok=False)
    target_entries = [path for path in target.iterdir() if path != staged]
    staged_entries = [path for path in staged.iterdir() if path != rollback]
    moved_old: list[Path] = []
    installed: list[Path] = []

    def move_entry(source: Path, destination: Path) -> None:
        try:
            source.replace(destination)
        except OSError:
            if source.is_dir():
                shutil.copytree(source, destination)
                shutil.rmtree(source)
            else:
                shutil.copy2(source, destination)
                source.unlink()

    try:
        for old in target_entries:
            backup = rollback / old.name
            move_entry(old, backup)
            moved_old.append(backup)
        for new in staged_entries:
            destination = target / new.name
            move_entry(new, destination)
            installed.append(destination)
    except Exception:
        for path in installed:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
        for backup in moved_old:
            if backup.exists():
                move_entry(backup, target / backup.name)
        shutil.rmtree(staged, ignore_errors=True)
        raise
    shutil.rmtree(staged, ignore_errors=True)


def copy_if_present(source: str | Path, destination: str | Path) -> bool:
    source_path = Path(source)
    if not source_path.is_file():
        return False
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, destination_path)
    return True
