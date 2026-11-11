"""Read reusable resource configurations from the canonical Benchmark table."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

from factor_protocol.readme import acceptance_items

from .models import UpdateConfig


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
