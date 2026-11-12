"""Resolve update targets, shared contracts and measured resource configurations."""
from __future__ import annotations

import math
import re
import yaml
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date
from factor_protocol import FactorPath, resolve_factor_columns, resolve_factor_schema_path
from factor_protocol.fingerprint import factor_definition_fingerprint
from factor_protocol.loader import load_module_from_path, resolve_factor_package
from factor_protocol.readme import acceptance_items
from factor_protocol.registry import load_factor_registry_entries
from pathlib import Path




@dataclass(frozen=True)
class UpdateConfig:
    factor_name: str
    storage_factor_name: str | None = None
    factor_module: str | None = None
    data_root: str | Path | None = None
    factor_zoo_root: str | Path | None = None
    threads: int | None = None
    compute_batch_days: int | None = None
    bootstrap_begin: str | None = None
    dry_run: bool = False
    refresh_validation: bool = True
    refresh_evaluation: bool = True
    factor_output_root: str = "artifacts/factor_zoo"
    memory_limit: str | None = None
    temp_directory: str | Path | None = None
    validation_memory_limit: str | None = None
    project_root: str | Path | None = None


@dataclass(frozen=True)
class UpdateResult:
    factor_name: str
    status: str
    begin: str | None = None
    end: str | None = None
    before_end: str | None = None
    after_end: str | None = None
    missing_dates: tuple[str, ...] = ()
    message: str = ""
    source_begin: str | None = None
    compute_batch_days: int | None = None


@dataclass(frozen=True)
class UpdatePlan:
    factor_name: str
    begin: str
    end: str
    before_end: str | None
    calculate: bool
    refresh_validation: bool


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


class BenchmarkRequired(ValueError):
    """The factor has no completed, reusable Benchmark configuration."""


@dataclass(frozen=True)
class BenchmarkResources:
    threads: int
    memory_limit: str
    compute_batch_days: int
    average_seconds: float
    peak_rss_mb: float


HEADERS = (
    "测试日期", "输出日期 / 区间", "输出交易日数", "每批交易日", "内部批次数",
    "线程", "内存限制", "日均耗时（秒）", "峰值 RSS（MB）", "状态", "原因",
)


def _cells(line: str) -> list[str]:
    return [value.strip() for value in re.split(r"(?<!\\)\|", line.strip())[1:-1]]


def _positive_int(value: str) -> int:
    result = int(value)
    if result < 1:
        raise ValueError("resource and batch counts must be positive")
    return result


def _number(value: str, *, positive: bool = False) -> float:
    result = float(value)
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise ValueError("Benchmark measurements must be finite and nonnegative")
    return result


def _memory(value: str) -> str:
    compact = re.sub(r"\s+", "", value).upper()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGTPE]I?B|B)", compact)
    if not match or float(match[1]) <= 0:
        raise ValueError("Benchmark must record an explicit memory limit")
    return compact


def read_benchmark_resources(readme: Path) -> tuple[BenchmarkResources, ...]:
    """Accept only the standard table emitted by the current Benchmark script."""
    try:
        content = readme.read_text(encoding="utf-8")
        items = acceptance_items(content)
        if len(items) < 5 or not re.match(r"- \[[xX]\]", items[4].group()):
            raise ValueError("Benchmark acceptance item is not checked")
        section = re.search(r"(?ms)^## 7\. 性能测试\s*\n(.*?)(?=^## |\Z)", content)
        if section is None:
            raise ValueError("performance section is missing")
        start = "<!-- factor-benchmark:results:start -->"
        end = "<!-- factor-benchmark:results:end -->"
        body = section[1]
        if body.count(start) != 1 or body.count(end) != 1 or body.index(start) >= body.index(end):
            raise ValueError("performance result markers are missing or invalid")
        body = body.split(start, 1)[1].split(end, 1)[0]
        rows = [_cells(line) for line in body.splitlines() if line.strip().startswith("|")]
        if len(rows) < 4 or tuple(rows[0]) != HEADERS:
            raise ValueError("standard Benchmark table is missing")
        if len(rows[1]) != len(HEADERS) or not all(re.fullmatch(r":?-+:?", c) for c in rows[1]):
            raise ValueError("invalid Benchmark table separator")
        candidates = []
        daily = []
        for cells in rows[2:]:
            if len(cells) != len(HEADERS):
                raise ValueError("incomplete Benchmark row")
            row = dict(zip(HEADERS, cells))
            date.fromisoformat(row["测试日期"])
            count = _positive_int(row["输出交易日数"])
            batch = _positive_int(row["每批交易日"])
            if _positive_int(row["内部批次数"]) != math.ceil(count / batch):
                raise ValueError("Benchmark batch counts do not match")
            threads = _positive_int(row["线程"])
            memory = _memory(row["内存限制"])
            peak = _number(row["峰值 RSS（MB）"], positive=True)
            interval = row["输出日期 / 区间"]
            if ".." in interval:
                begin, end_date = interval.split("..")
                if date.fromisoformat(begin) > date.fromisoformat(end_date):
                    raise ValueError("invalid Benchmark interval")
                if row["状态"] not in {"pass", "oom"}:
                    raise ValueError("Benchmark candidates are incomplete or failed")
                candidates.append((row, threads, memory, batch, peak))
            else:
                date.fromisoformat(interval)
                if row["状态"] != "pass" or (count, batch) != (1, 1):
                    raise ValueError("single-day append did not pass")
                _number(row["日均耗时（秒）"])
                daily.append(row)
        if not candidates or len(daily) != 1:
            raise ValueError("candidate or single-day append results are missing")
        first = candidates[0][0]
        same_fields = ("测试日期", "输出日期 / 区间", "输出交易日数", "线程", "内存限制")
        if any(tuple(r[k] for k in same_fields) != tuple(first[k] for k in same_fields)
               for r, *_ in candidates):
            raise ValueError("Benchmark candidates use inconsistent comparison settings")
        day = daily[0]
        if (day["测试日期"], day["输出日期 / 区间"], day["线程"], day["内存限制"]) != (
                first["测试日期"], first["输出日期 / 区间"].split("..")[1], first["线程"], first["内存限制"]):
            raise ValueError("single-day append does not match the candidate run")
        passed = tuple(
            BenchmarkResources(threads, memory, batch, _number(row["日均耗时（秒）"]), peak)
            for row, threads, memory, batch, peak in candidates if row["状态"] == "pass"
        )
        if not passed:
            raise ValueError("no passing Benchmark candidate")
        return passed
    except (OSError, ValueError) as exc:
        raise BenchmarkRequired(f"{readme}: {exc}; run factor-benchmark before factor-update") from exc


def configure_resources(cfg: UpdateConfig) -> UpdateConfig:
    readme = Path(cfg.factor_zoo_root) / cfg.factor_name / "README.md"
    candidates = read_benchmark_resources(readme)
    matching = [row for row in candidates
                if (cfg.threads is None or cfg.threads == row.threads)
                and (cfg.memory_limit is None or _memory(cfg.memory_limit) == row.memory_limit)
                and (cfg.compute_batch_days is None or cfg.compute_batch_days == row.compute_batch_days)]
    if not matching:
        raise BenchmarkRequired("requested resource combination has no passing Benchmark row; run factor-benchmark first")
    best = min(matching, key=lambda row: (row.average_seconds, row.peak_rss_mb, row.compute_batch_days))
    return replace(cfg, threads=best.threads, memory_limit=best.memory_limit,
                   validation_memory_limit=best.memory_limit, compute_batch_days=best.compute_batch_days)
