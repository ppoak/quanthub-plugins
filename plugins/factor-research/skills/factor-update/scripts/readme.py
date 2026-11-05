"""Write the latest measured incremental update into factor README section 7."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from factor_protocol import FactorStorage, resolve_data_root, resolve_factor_data_root, resolve_factor_zoo_root

def _section_bounds(lines: list[str]) -> tuple[int, int]:
    starts = [
        index
        for index, line in enumerate(lines)
        if re.match(r"^##\s+7[.．、]\s+", line.strip())
    ]
    if len(starts) != 1:
        raise ValueError("factor README must contain exactly one level-two section 7")
    start = starts[0]
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if re.match(r"^##\s", lines[index])
        ),
        len(lines),
    )
    return start, end




def _clean_cell(value: Any) -> str:
    text = "—" if value is None else str(value)
    return " ".join(text.replace("|", "/").split()) or "—"


def render_update_record(result: Mapping[str, Any]) -> str:
    fields = (
        ("test_date", "YYYY-MM-DD"),
        ("output_date", "YYYY-MM-DD"),
        ("source_begin", "—"),
        ("threads", "—"),
        ("memory_limit", "—"),
        ("compute_batch_days", "—"),
        ("wall_clock_seconds", "—"),
        ("peak_rss_mb", "—"),
        ("output_rows", "—"),
        ("status", "pass"),
        ("notes", "—"),
    )
    return "增量更新：" + "；".join(
        f"{name}={_clean_cell(result.get(name, default))}" for name, default in fields
    )


def update_factor_readme(readme_path: Path, result: Mapping[str, Any]) -> bool:
    if not readme_path.is_file():
        raise FileNotFoundError(f"factor README not found: {readme_path.as_posix()}")
    original = readme_path.read_text(encoding="utf-8")
    lines = original.splitlines()
    start, end = _section_bounds(lines)
    section = lines[start:end]
    record = render_update_record(result)
    benchmark_start = "<!-- factor-benchmark:results:start -->"
    benchmark_end = "<!-- factor-benchmark:results:end -->"
    if benchmark_start in section and benchmark_end in section:
        left, right = section.index(benchmark_start), section.index(benchmark_end)
        record = "正式增量运行记录（计算、验证及评估刷新）：" + record
        body = [line for line in section[left + 1:right]
                if not line.startswith("正式增量运行记录（")]
        section[left + 1:right] = [*body, "", record]
        updated = "\n".join([*lines[:start], *section, *lines[end:]]) + "\n"
        if updated == original:
            return False
        _write_atomic(readme_path, updated)
        return True
    raise ValueError("factor README requires the performance results block")


def update_readme_after_run(
    *,
    project_root: Path,
    data_root: str | Path,
    factor_zoo_root: str | Path,
    factor_name: str,
    status: str,
    run_started_at: datetime,
    threads: int,
    memory_limit: str,
    compute_batch_days: int | None,
    wall_clock_seconds: float,
    peak_rss_bytes: int | None,
    sampler_error: str | None,
    begin: str | None,
    after_end: str | None,
    source_begin: str | None,
    message: str,
) -> tuple[Path, bool] | None:
    root = Path(project_root).resolve()
    if status == "updated":
        if not begin or not after_end:
            raise ValueError("updated result has no output date range")
        output_rows = count_output_rows(
            project_root=root,
            data_root=data_root,
            factor_name=factor_name,
            begin=begin,
            end=after_end,
        )
        report = {
            "test_date": run_started_at.date().isoformat(),
            "output_date": after_end,
            "source_begin": source_begin or "—",
            "threads": threads,
            "memory_limit": memory_limit or "—",
            "compute_batch_days": compute_batch_days,
            "wall_clock_seconds": round(wall_clock_seconds, 3),
            "peak_rss_mb": (
                round(peak_rss_bytes / (1024 * 1024), 3)
                if peak_rss_bytes is not None
                else "—"
            ),
            "output_rows": output_rows,
            "status": "pass",
            "notes": _update_notes(
                message,
                output_range=f"{begin}..{after_end}",
                sampler_error=sampler_error,
            ),
        }
        _save_readme_result(root, factor_name, report)
    elif status == "up_to_date":
        report = _load_saved_readme_result(root, factor_name)
    else:
        return None

    if report is None:
        return None
    readme_path = (
        resolve_factor_zoo_root(
            project_root=root,
            factor_zoo_root=factor_zoo_root,
        )
        / factor_name
        / "README.md"
    )
    return readme_path, update_factor_readme(readme_path, report)


def update_readme_from_saved_result(
    *,
    project_root: Path,
    factor_zoo_root: str | Path,
    factor_name: str,
) -> tuple[Path, bool] | None:
    root = Path(project_root).resolve()
    report = _load_saved_readme_result(root, factor_name)
    if report is None:
        return None
    readme_path = (
        resolve_factor_zoo_root(
            project_root=root,
            factor_zoo_root=factor_zoo_root,
        )
        / factor_name
        / "README.md"
    )
    return readme_path, update_factor_readme(readme_path, report)


def count_output_rows(
    *,
    project_root: Path,
    data_root: str | Path,
    factor_name: str,
    begin: str,
    end: str,
) -> int:
    root = resolve_data_root(
        project_root=project_root,
        data_root=data_root,
    )
    factor_root = resolve_factor_data_root(project_root=project_root) / factor_name
    parquet_files: list[Path] = []
    for partition in factor_root.glob("date=*"):
        partition_date = partition.name.removeprefix("date=")
        try:
            date.fromisoformat(partition_date)
        except ValueError:
            continue
        if begin <= partition_date <= end:
            parquet_files.extend(
                path for path in partition.rglob("*.parquet") if path.is_file()
            )
    if not parquet_files:
        return 0

    file_list = (
        "["
        + ", ".join(
            FactorStorage.sql_literal(path.as_posix()) for path in sorted(parquet_files)
        )
        + "]"
    )
    storage = FactorStorage(
        "update_readme_count",
        data_root=root,
        threads=1,
        memory_limit="256MB",
    )
    try:
        row = storage.execute(
            f"SELECT COUNT(*) FROM read_parquet({file_list}, union_by_name=TRUE)"
        ).fetchone()
    finally:
        storage.close()
    return int(row[0]) if row else 0


def _update_notes(
    message: str,
    *,
    output_range: str,
    sampler_error: str | None,
) -> str:
    notes = [f"output_range={output_range}", "update=updated"]
    if message:
        notes.append(message)
    if sampler_error:
        notes.append(f"peak RSS sampling unavailable: {sampler_error}")
    return "; ".join(notes)


def _readme_result_path(project_root: Path, factor_name: str) -> Path:
    return project_root / ".tmp" / "factor_update" / factor_name / "readme_result.json"


def _save_readme_result(
    project_root: Path,
    factor_name: str,
    result: dict[str, Any],
) -> None:
    path = _readme_result_path(project_root, factor_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _load_saved_readme_result(
    project_root: Path,
    factor_name: str,
) -> dict[str, Any] | None:
    path = _readme_result_path(project_root, factor_name)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rewrite factor README section 7 from the latest saved update result."
    )
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--factor-name", required=True)
    parser.add_argument("--factor-zoo-root")
    args = parser.parse_args()

    try:
        result = update_readme_from_saved_result(
            project_root=Path(args.project_root),
            factor_zoo_root=args.factor_zoo_root,
            factor_name=args.factor_name,
        )
        if result is None:
            raise FileNotFoundError(
                f"saved update result not found for {args.factor_name}"
            )
        readme_path, changed = result
        action = "updated" if changed else "already current"
        print(f"Factor README {action}: {readme_path.as_posix()}", flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001 - expose writeback failures to the CLI.
        print(
            f"README writeback failed: {type(exc).__name__}: {exc}",
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
