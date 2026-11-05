from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep
from typing import Sequence

from factor_protocol import (
    resolve_data_root,
    resolve_factor_data_root,
    resolve_factor_schema_path,
    resolve_factor_zoo_root,
)
from factor_protocol.artifacts import resolve_output_root
from factor_protocol.config import resolve_artifacts_root
from factor_protocol.fingerprint import factor_definition_fingerprint
from factor_protocol.identity import require_identity
from factor_protocol.loader import resolve_factor_package
from factor_protocol.readme import acceptance_items, set_acceptance, update_acceptance


METRIC_FILES = (
    "audit.csv",
    "ic.csv",
    "returns.csv",
    "correlation.csv",
)
REPORT_IMAGE_FILE = "overview.png"
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")


def _readme_with_report_links(
    text: str,
    *,
    image_urls: dict[str, str],
) -> str:
    lines = ["## 9. 因子速览", ""]
    for column in sorted(image_urls):
        lines.extend(
            [
                f"### `{column}`",
                "",
                f"![{column} evaluation overview]({image_urls[column]})",
                "",
            ]
        )
    section = "\n".join(lines).rstrip() + "\n"
    heading = re.search(r"(?m)^## 9\. 因子速览\s*$", text)
    if heading is None:
        return text.rstrip() + "\n\n" + section
    next_heading = re.search(r"(?m)^## (?!#)", text[heading.end() :])
    section_end = (
        heading.end() + next_heading.start()
        if next_heading is not None
        else len(text)
    )
    prefix = text[: heading.start()].rstrip()
    suffix = text[section_end:].lstrip()
    return prefix + "\n\n" + section + ("\n" + suffix if suffix else "")


def update_readme_report(
    *,
    factor_name: str,
    factor_zoo_root: str | Path,
    image_urls: dict[str, str],
) -> Path:
    readme = Path(factor_zoo_root) / factor_name / "README.md"
    if not readme.is_file():
        raise FileNotFoundError(f"factor README not found: {readme.as_posix()}")
    original = readme.read_text(encoding="utf-8")
    updated = _readme_with_report_links(original, image_urls=image_urls)
    if updated != original:
        readme.write_text(updated, encoding="utf-8", newline="\n")
    return readme


def readme_title(readme_text: str, *, factor_name: str) -> str:
    heading = re.search(r"(?m)^# ([^\r\n]+)$", readme_text)
    title = heading.group(1) if heading else ""
    if not re.fullmatch(r"[A-Z0-9_][A-Za-z0-9_]*(?: [A-Z0-9_][A-Za-z0-9_]*)*", title):
        raise ValueError("README title must use capitalized English words, digits or underscores separated by spaces")
    if (not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", factor_name)
            or title.lower().replace(" ", "_") != factor_name):
        raise ValueError("README title does not match the factor package name")
    return title


def sync_release_readme(
    *, factor_name: str, readme_text: str, repo: str | None = None,
    project_root: str | Path | None = None, dry_run: bool = False,
) -> None:
    if not readme_text.strip():
        raise ValueError("factor README is empty")
    title = readme_title(readme_text, factor_name=factor_name)
    cwd = Path(project_root).resolve() if project_root is not None else None
    repo_args = ["--repo", repo] if repo else []
    if dry_run:
        print(f"Would synchronize Release title and README: {factor_name}")
        return
    with tempfile.TemporaryDirectory(prefix="factor_release_readme_") as directory:
        notes = Path(directory) / "notes.md"
        notes.write_text(readme_text, encoding="utf-8", newline="\n")
        subprocess.run(
            ["gh", "release", "edit", factor_name, *repo_args,
             "--title", title, "--notes-file", str(notes)],
            cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8",
        )
    result = subprocess.run(
        ["gh", "release", "view", factor_name, *repo_args,
         "--json", "tagName,name,body"],
        cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8",
    )
    state = json.loads(result.stdout)
    if (state.get("tagName") != factor_name or state.get("name") != title
            or state.get("body") != readme_text):
        raise ValueError(f"Release title or README synchronization failed: {factor_name}")
    print(f"Release README synchronized: {factor_name}")


@dataclass(frozen=True)
class EvaluationAsset:
    factor_column: str
    metric_file: str
    path: Path


def _safe_component(value: str) -> str:
    cleaned = _SAFE_COMPONENT.sub("_", str(value)).strip("._-")
    return cleaned or "factor"


def _factor_package_columns(
    *,
    factor_name: str,
    factor_zoo_root: str | Path,
    project_root: str | Path | None = None,
) -> tuple[str, ...]:
    from factor_protocol.loader import load_module_from_path
    from factor_protocol.protocol import resolve_factor_columns

    factor_zoo_path = Path(factor_zoo_root)
    root = (
        Path(project_root).expanduser().resolve()
        if project_root is not None
        else factor_zoo_path.parent
        if factor_zoo_path.is_absolute()
        else Path.cwd()
    )
    package = resolve_factor_package(
        factor_name,
        project_root=root,
        factor_zoo_root=factor_zoo_path,
    )
    module = load_module_from_path(
        package.module_path,
        project_root=root,
        factor_name=factor_name,
    )
    return resolve_factor_columns(module)


def _active_entries(
    *,
    payload: dict[str, object],
    schema_path: Path,
) -> list[dict[str, object]]:
    """Return the active factor package declarations."""
    active = payload.setdefault("active", [])
    if not isinstance(active, list):
        raise ValueError(
            f"factor schema active must be a list: {schema_path.as_posix()}"
        )
    for index, entry in enumerate(active):
        if not isinstance(entry, dict):
            raise ValueError(f"factor schema active[{index}] must be a package mapping")
        package = entry.get("package")
        fingerprint = entry.get("fingerprint")
        columns = entry.get("columns")
        if (
            not isinstance(package, str)
            or not _IDENTIFIER.fullmatch(package)
            or not isinstance(fingerprint, str)
            or not _FINGERPRINT.fullmatch(fingerprint)
            or not isinstance(columns, list)
            or not columns
            or any(
                not isinstance(column, str) or not _IDENTIFIER.fullmatch(column)
                for column in columns
            )
        ):
            raise ValueError(f"invalid factor schema active[{index}]: {entry!r}")
    return active


def _active_schema_plan(
    *,
    factor_name: str,
    factor_columns: Sequence[str],
    factor_zoo_root: str | Path,
    project_root: str | Path | None = None,
) -> tuple[Path, dict[str, object], list[tuple[str, str]]]:
    schema_root = (
        Path(project_root).expanduser().resolve()
        if project_root is not None
        else Path(factor_zoo_root).resolve().parent
        if Path(factor_zoo_root).is_absolute()
        else Path.cwd()
    )
    schema_path = resolve_factor_schema_path(
        project_root=schema_root,
        factor_zoo_root=factor_zoo_root,
    )
    if not schema_path.is_file():
        raise FileNotFoundError(f"factor schema not found: {schema_path.as_posix()}")
    try:
        import yaml

        payload = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(
            f"cannot read factor schema: {schema_path.as_posix()}"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError(
            f"factor schema root must be a mapping: {schema_path.as_posix()}"
        )
    active = _active_entries(payload=payload, schema_path=schema_path)
    package = resolve_factor_package(
        factor_name,
        project_root=schema_root,
        factor_zoo_root=str(factor_zoo_root),
    )
    fingerprint = factor_definition_fingerprint(package)
    identity = require_identity(
        resolve_factor_data_root(project_root=schema_root) / factor_name,
        fingerprint=fingerprint,
        columns=factor_columns,
    )
    if identity.get("factor_name") != factor_name:
        raise ValueError("factor identity does not match the requested package")
    declared_columns = set(
        _factor_package_columns(
            factor_name=factor_name,
            factor_zoo_root=factor_zoo_root,
            project_root=project_root,
        )
    )
    if set(factor_columns) != declared_columns:
        raise ValueError(
            "publishing requires every output column declared by the factor package"
        )
    package_locations: dict[str, int] = {}
    seen_paths: set[str] = set()
    for index, entry in enumerate(active):
        package = entry["package"]
        if not isinstance(package, str):
            raise ValueError(f"invalid active package at index {index}")
        if package in package_locations:
            raise ValueError(f"duplicate active factor package: {package}")
        package_locations[package] = index
        columns = entry["columns"]
        if not isinstance(columns, list):
            raise ValueError(f"invalid active columns for package {package!r}")
        for column in columns:
            if not isinstance(column, str):
                raise ValueError(f"invalid active column for package {package!r}")
            path = f"{package}.{column}"
            if path in seen_paths:
                raise ValueError(f"duplicate active factor path: {path}")
            seen_paths.add(path)
    changes: list[tuple[str, str]] = []
    package_index = package_locations.get(factor_name)
    if package_index is None:
        active.append(
            {
                "package": factor_name,
                "fingerprint": fingerprint,
                "columns": list(factor_columns),
            }
        )
        changes.extend(("add", f"{factor_name}.{column}") for column in factor_columns)
        return schema_path, payload, changes

    current = active[package_index]
    previous_columns = current["columns"]
    previous_fingerprint = current["fingerprint"]
    changes.extend(
        ("remove", f"{factor_name}.{column}")
        for column in previous_columns
        if column not in factor_columns
    )
    for column in factor_columns:
        action = (
            "add"
            if column not in previous_columns
            else "update"
            if previous_fingerprint != fingerprint
            else "unchanged"
        )
        changes.append((action, f"{factor_name}.{column}"))
    current["fingerprint"] = fingerprint
    current["columns"] = list(factor_columns)

    return schema_path, payload, changes


def _write_active_schema(schema_path: Path, payload: dict[str, object]) -> None:
    import yaml

    temporary = schema_path.with_name(schema_path.name + ".tmp")
    temporary.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(schema_path)


def _factor_directories(root: Path, declared_columns: Sequence[str]) -> list[Path]:
    directories = [root / column for column in declared_columns]
    missing = [path.name for path in directories if not path.is_dir()]
    if missing:
        raise FileNotFoundError(
            f"missing factor output directories: {', '.join(missing)}"
        )
    if not directories:
        raise ValueError("factor package must declare output columns")
    return directories


def stage_evaluation_assets(
    *,
    factor_name: str,
    output_dir: str | Path,
    stage_dir: str | Path,
    data_root: str | Path,
    project_root: str | Path,
    declared_columns: Sequence[str],
) -> list[EvaluationAsset]:
    root = Path(output_dir) / factor_name
    columns = _factor_directories(root, declared_columns)
    stage = Path(stage_dir)
    stage.mkdir(parents=True, exist_ok=True)
    assets: list[EvaluationAsset] = []
    names: set[str] = set()
    missing: list[str] = []

    checker_path = Path(__file__).resolve().parents[2] / "factor-evaluate/scripts/core/validation.py"
    spec = importlib.util.spec_from_file_location("factor_evaluation_check", checker_path)
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    manifest_cache = {}

    started = monotonic()
    for index, column_dir in enumerate(columns, 1):
        column = column_dir.name
        print(f"Prepare {index}/{len(columns)} | {column}", flush=True)
        checker.check_package_evaluation(
            column_dir,
            factor_name=factor_name,
            data_root=data_root,
            project_root=project_root,
            manifest_cache=manifest_cache,
        )
        paths = {metric: column_dir / metric for metric in METRIC_FILES}
        missing_files = [metric for metric, path in paths.items() if not path.is_file()]
        image_source = column_dir / REPORT_IMAGE_FILE
        if not image_source.is_file() or image_source.stat().st_size <= 0:
            missing_files.append(REPORT_IMAGE_FILE)
        if missing_files:
            missing.append(f"{column}: {', '.join(missing_files)}")
            continue
        safe_name = _safe_component(f"{factor_name}__{column}")
        for metric, source in paths.items():
            asset_name = f"{safe_name}__{Path(metric).stem}.csv"
            if asset_name in names:
                raise ValueError(f"duplicate release asset name: {asset_name}")
            names.add(asset_name)
            target = stage / asset_name
            shutil.copyfile(source, target)
            assets.append(EvaluationAsset(column, metric, target))
        image_name = f"{safe_name}__{REPORT_IMAGE_FILE}"
        if image_name in names:
            raise ValueError(f"duplicate release asset name: {image_name}")
        names.add(image_name)
        image_target = stage / image_name
        shutil.copyfile(image_source, image_target)
        assets.append(EvaluationAsset(column, REPORT_IMAGE_FILE, image_target))
        elapsed = monotonic() - started
        print(f"Prepare: {index / len(columns):.1%} | columns={index}/{len(columns)} | "
              f"elapsed={elapsed:.1f}s | ETA={elapsed / index * (len(columns) - index):.1f}s", flush=True)

    if missing:
        raise FileNotFoundError(
            "incomplete factor evaluation columns: " + "; ".join(missing)
        )
    if not assets:
        raise FileNotFoundError(
            f"no complete factor evaluation columns found: {root.as_posix()}"
        )
    return assets


def _run(
    command: Sequence[str],
    *,
    dry_run: bool,
    cwd: str | Path | None = None,
) -> None:
    printable = " ".join(shlex.quote(str(part)) for part in command)
    if dry_run:
        print("dry-run: " + printable, flush=True)
        return
    result = subprocess.run(list(command), cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        reason = (result.stderr or result.stdout or f"exit code {result.returncode}").strip()
        raise RuntimeError(f"{' '.join(command[:3])}: {reason[:1200]}")


def upload_assets(assets: Sequence[EvaluationAsset], *, tag: str, repo: str | None,
                  cwd: Path, dry_run: bool) -> None:
    def remote_assets() -> dict[str, dict]:
        result = subprocess.run(
            ["gh", "release", "view", tag, *_gh_repo_args(repo), "--json", "assets"],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=True,
        )
        return {item["name"]: item for item in json.loads(result.stdout)["assets"]}

    remote = {} if dry_run else remote_assets()
    started = monotonic()
    total_bytes = sum(asset.path.stat().st_size for asset in assets)
    completed_bytes = 0
    for index, asset in enumerate(assets, 1):
        print(f"Upload {index}/{len(assets)} | {asset.path.name} | "
              f"size={asset.path.stat().st_size / 1048576:.2f}MB", flush=True)
        command = ["gh", "release", "upload", tag, *_gh_repo_args(repo),
                   "--clobber", asset.path.as_posix()]
        with asset.path.open("rb") as stream:
            digest = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
        for attempt in range(1, 4):
            existing = remote.get(asset.path.name, {})
            if (existing.get("state") == "uploaded"
                    and existing.get("size") == asset.path.stat().st_size
                    and existing.get("digest") == digest):
                print(f"Upload verified; skipping: {asset.path.name}", flush=True)
                break
            try:
                _run(command, dry_run=dry_run, cwd=cwd)
                break
            except RuntimeError as exc:
                collision = "releaseasset.name already exists" in str(exc).lower()
                transient = any(text in str(exc).lower() for text in
                                ("eof", "timeout", "timed out", "connection reset", "tls handshake", "http 502", "http 503", "http 504"))
                if attempt == 3 or not (transient or collision):
                    raise RuntimeError(f"Upload failed: {asset.path.name}: {exc}") from None
                remote = remote_assets()
                existing = remote.get(asset.path.name, {})
                if collision and existing and not (
                    existing.get("state") == "uploaded"
                    and existing.get("size") == asset.path.stat().st_size
                    and existing.get("digest") == digest
                ):
                    _run(["gh", "release", "delete-asset", tag, asset.path.name,
                          *_gh_repo_args(repo), "--yes"], dry_run=False, cwd=cwd)
                    remote.pop(asset.path.name, None)
                print(f"Upload retry {attempt}/2 | {asset.path.name} | {exc}", flush=True)
                sleep(attempt)
        completed_bytes += asset.path.stat().st_size
        elapsed = monotonic() - started
        fraction = completed_bytes / total_bytes if total_bytes else index / len(assets)
        remaining = elapsed / fraction * (1 - fraction)
        print(f"Upload{' plan' if dry_run else ''}: {fraction:.1%} | files={index}/{len(assets)} | "
              f"elapsed={elapsed:.1f}s | ETA={remaining:.1f}s", flush=True)


def _gh_repo_args(repo: str | None) -> list[str]:
    return ["--repo", repo] if repo else []


def _repository_url(*, repo: str | None, cwd: str | Path) -> str:
    command = ["gh", "repo", "view"]
    if repo:
        command.append(repo)
    command.extend(["--json", "url", "--jq", ".url"])
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "cannot resolve the GitHub repository from the current project"
            + (f": {repo}" if repo else "")
        )
    url = result.stdout.strip().rstrip("/")
    if not url.startswith(("https://", "http://")):
        raise ValueError(f"invalid GitHub repository URL: {url!r}")
    return url


def _release_state(
    *, repo: str | None, tag: str, cwd: str | Path | None = None
) -> tuple[bool, bool]:
    result = subprocess.run(
        [
            "gh",
            "release",
            "view",
            tag,
            *_gh_repo_args(repo),
            "--json",
            "tagName,isPrerelease,isDraft",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    repository = repo or "current repository"
    if result.returncode != 0:
        raise RuntimeError(f"release not found or inaccessible: {repository}@{tag}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid release response: {repository}@{tag}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("tagName") != tag
        or not isinstance(payload.get("isPrerelease"), bool)
        or not isinstance(payload.get("isDraft"), bool)
    ):
        raise ValueError(f"invalid release state: {repository}@{tag}")
    return payload["isPrerelease"], payload["isDraft"]


def finalize_release(
    *,
    factor_name: str,
    repo: str | None = None,
    tag: str | None = None,
    dry_run: bool = False,
    project_root: str | Path | None = None,
) -> None:
    resolved_tag = tag or factor_name
    cwd = Path(project_root).expanduser().resolve() if project_root else None
    repository = repo or "current repository"
    if resolved_tag != factor_name:
        raise ValueError("release tag must match factor name")
    _, is_draft = _release_state(
        repo=repo,
        tag=resolved_tag,
        cwd=cwd,
    )
    if is_draft:
        raise ValueError(f"release is still a draft: {repository}@{resolved_tag}")
    _run(
        [
            "gh",
            "release",
            "edit",
            resolved_tag,
            *_gh_repo_args(repo),
            "--prerelease=false",
            "--latest=true",
        ],
        dry_run=dry_run,
        cwd=cwd,
    )
    if dry_run:
        return
    is_prerelease, is_draft = _release_state(
        repo=repo,
        tag=resolved_tag,
        cwd=cwd,
    )
    if is_prerelease or is_draft:
        raise RuntimeError(
            f"release finalization did not take effect: {repository}@{resolved_tag}"
        )
    result = subprocess.run(
        ["gh", "release", "view", *_gh_repo_args(repo), "--json", "tagName"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=cwd,
    )
    latest = json.loads(result.stdout)
    if not isinstance(latest, dict) or latest.get("tagName") != resolved_tag:
        raise RuntimeError(f"release is not latest: {repository}@{resolved_tag}")
    print(f"release finalized and marked latest: {repository}@{resolved_tag}", flush=True)


def _asset_url(*, repository_url: str, tag: str, name: str) -> str:
    return f"{repository_url}/releases/download/{tag}/{name}"


def _report_image_urls(
    *,
    assets: Sequence[EvaluationAsset],
    repository_url: str,
    tag: str,
) -> dict[str, str]:
    return {
        asset.factor_column: _asset_url(
            repository_url=repository_url, tag=tag, name=asset.path.name
        )
        for asset in assets
        if asset.metric_file == REPORT_IMAGE_FILE
    }


def verify_release_assets(*, repo: str | None, tag: str, cwd: Path,
                          assets: Sequence[EvaluationAsset], readme_text: str) -> None:
    repository = repo or _repository_url(repo=None, cwd=cwd).removeprefix("https://github.com/")
    result = subprocess.run(["gh", "api", f"repos/{repository}/releases/tags/{tag}"],
                            cwd=cwd, capture_output=True, text=True, check=True)
    payload = json.loads(result.stdout)
    remote = {asset["name"]: asset for asset in payload.get("assets", [])}
    if (not any(name.endswith(".pdf") for name in remote)
            and not re.search(r"(?m)^Reference PDFs: \S.*$", readme_text)):
        raise ValueError("Reference PDFs or their confirmed absence in README are missing; run Design create_readme.py")
    for asset in assets:
        uploaded = remote.get(asset.path.name)
        if not uploaded or uploaded.get("size") != asset.path.stat().st_size:
            raise ValueError(f"missing or incomplete release asset: {asset.path.name}")
        digest = uploaded.get("digest")
        if digest:
            with asset.path.open("rb") as stream:
                expected = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != expected:
                raise ValueError(f"release asset digest mismatch: {asset.path.name}")


def publish_assets(
    *,
    factor_name: str,
    project_root: str | Path | None = None,
    output_dir: str | Path | None = None,
    factor_zoo_root: str | Path | None = None,
    data_root: str | Path | None = None,
    repo: str | None = None,
    tag: str | None = None,
    dry_run: bool = False,
) -> list[str]:
    resolved_tag = tag or factor_name

    factor_project_root = (
        Path(project_root).expanduser().resolve()
        if project_root is not None
        else Path(factor_zoo_root).expanduser().resolve().parent
        if factor_zoo_root is not None and Path(factor_zoo_root).is_absolute()
        else Path.cwd()
    )
    resolved_factor_zoo_root = resolve_factor_zoo_root(
        project_root=factor_project_root,
        factor_zoo_root=factor_zoo_root,
    )
    resolved_data_root = resolve_data_root(
        project_root=factor_project_root,
        data_root=data_root,
    )
    resolved_output_dir = (
        resolve_output_root(output_dir, project_root=factor_project_root)
        if output_dir is not None
        else resolve_artifacts_root(project_root=factor_project_root) / "factor_zoo"
    )
    started = monotonic()
    print(f"Publish | {factor_name} | tag={resolved_tag}", flush=True)
    print(f"Data: {resolved_data_root.as_posix()}\nArtifacts: {resolved_output_dir.as_posix()}", flush=True)
    print("Publish phase: checking repository and preparing assets", flush=True)
    readme = resolved_factor_zoo_root / factor_name / "README.md"
    if not dry_run:
        update_acceptance(readme, 8, False)
    readme_title(readme.read_text(encoding="utf-8"), factor_name=factor_name)
    repository_url = _repository_url(repo=repo, cwd=factor_project_root)

    with tempfile.TemporaryDirectory(prefix="factor_publish_") as temporary:
        declared_columns = _factor_package_columns(
            factor_name=factor_name,
            factor_zoo_root=resolved_factor_zoo_root,
            project_root=factor_project_root,
        )
        assets = stage_evaluation_assets(
            factor_name=factor_name,
            output_dir=resolved_output_dir,
            stage_dir=temporary,
            data_root=resolved_data_root,
            project_root=factor_project_root,
            declared_columns=declared_columns,
        )
        published_columns = sorted({asset.factor_column for asset in assets})
        print("Publish phase: checking data identity and active schema", flush=True)
        schema_path, schema_payload, schema_changes = _active_schema_plan(
            factor_name=factor_name,
            factor_columns=published_columns,
            factor_zoo_root=resolved_factor_zoo_root,
            project_root=factor_project_root,
        )
        asset_paths = [asset.path for asset in assets]
        asset_names = [path.name for path in asset_paths]
        image_urls = _report_image_urls(
            assets=assets,
            repository_url=repository_url,
            tag=resolved_tag,
        )
        print(
            f"prepared evaluation assets: factor={factor_name}; "
            f"columns={len({asset.factor_column for asset in assets})}; "
            f"csv_files={sum(asset.metric_file in METRIC_FILES for asset in assets)}; "
            f"image_files={len(image_urls)}",
            flush=True,
        )


        if resolved_tag != factor_name:
            raise ValueError("release tag must match factor name")
        items = acceptance_items(readme.read_text(encoding="utf-8"))
        if len(items) < 8 or any(match.group()[3].lower() != "x" for match in items[:7]):
            raise ValueError("publishing requires acceptance items 1-7 to pass and item 8 to exist")
        _, is_draft = _release_state(repo=repo, tag=resolved_tag, cwd=factor_project_root)
        if is_draft:
            raise ValueError("release is still a draft")
        print("Publish phase: uploading assets", flush=True)
        upload_assets(assets, tag=resolved_tag, repo=repo, cwd=factor_project_root, dry_run=dry_run)
        if dry_run:
            print("Would verify all CSV/PNG assets, synchronize Release title and complete README, finalize as latest, and check acceptance item 8", flush=True)
        else:
            print("Publish phase: verifying remote assets", flush=True)
            verify_release_assets(repo=repo, tag=resolved_tag, cwd=factor_project_root,
                                  assets=assets, readme_text=readme.read_text(encoding="utf-8"))
            print("Publish phase: updating README image links", flush=True)
            readme = update_readme_report(
                factor_name=factor_name, factor_zoo_root=resolved_factor_zoo_root,
                image_urls=image_urls)
            complete_readme = set_acceptance(readme.read_text(encoding="utf-8"), 8, True)
            print("Publish phase: synchronizing Release title and README", flush=True)
            sync_release_readme(factor_name=factor_name, readme_text=complete_readme,
                                repo=repo, project_root=factor_project_root)
            print("Publish phase: finalizing Release as latest", flush=True)
            finalize_release(factor_name=factor_name, repo=repo, tag=resolved_tag,
                             project_root=factor_project_root)
            if any(action != "unchanged" for action, _ in schema_changes):
                _write_active_schema(schema_path, schema_payload)
            update_acceptance(readme, 8, True)
            print(f"Publish: pass | columns={len(declared_columns)} | assets={len(assets)} | elapsed={monotonic() - started:.1f}s", flush=True)
            print(f"README: {readme.as_posix()}", flush=True)

    return [
        _asset_url(repository_url=repository_url, tag=resolved_tag, name=name)
        for name in asset_names
    ]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Publish factor assets or synchronize the Release README."
    )
    parser.add_argument(
        "--operation", choices=("publish", "readme"), default="publish",
        help="Publish complete assets (default), or only synchronize the Release title and README.",
    )
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--factor-name", required=True)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--factor-zoo-root", default=None)
    parser.add_argument("--data-root", default=None)
    parser.add_argument(
        "--repo",
        default=None,
        help="GitHub repository in owner/name form; defaults to the project repository.",
    )
    parser.add_argument("--tag", default=None)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    project_root = Path(args.project_root).expanduser().resolve()
    try:
        if args.operation == "readme":
            zoo = resolve_factor_zoo_root(
                project_root=project_root, factor_zoo_root=args.factor_zoo_root,
            )
            readme = zoo / args.factor_name / "README.md"
            sync_release_readme(
                factor_name=args.factor_name, readme_text=readme.read_text(encoding="utf-8"),
                repo=args.repo, project_root=project_root, dry_run=args.dry_run,
            )
        else:
            publish_assets(
                factor_name=args.factor_name,
                project_root=project_root,
                output_dir=args.output_dir,
                factor_zoo_root=args.factor_zoo_root,
                data_root=args.data_root,
                repo=args.repo,
                tag=args.tag,
                dry_run=args.dry_run,
            )
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        reason = ((exc.stderr or str(exc)) if isinstance(exc, subprocess.CalledProcessError) else str(exc))
        print(f"Publish failed: {reason[:1400]}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
