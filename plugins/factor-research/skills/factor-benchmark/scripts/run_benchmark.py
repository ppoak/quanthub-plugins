"""Measure factor time and RSS across trading-day batch sizes."""

from __future__ import annotations

import argparse
import calendar
import ctypes
import inspect
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from factor_protocol import (
    FactorStorage,
    resolve_data_root,
    resolve_factor_columns,
    resolve_factor_zoo_root,
)
from factor_protocol.config import _dotenv_value
from factor_protocol.identity import parquet_manifest, read_identity, require_identity
from factor_protocol.loader import load_module_from_path, resolve_factor_package
from factor_protocol.readme import set_acceptance, update_acceptance


DEFAULT_COMPUTE_BATCH_DAYS = (1, 5, 10, 20, 30)
DEFAULT_OUTPUT_MONTHS = 3
DEFAULT_TIMEOUT_SECONDS = 7200
OOM_PATTERN = re.compile(
    r"out of memory|memoryerror|memory limit exceeded|"
    r"failed to allocate|cannot allocate memory|insufficient memory",
    re.IGNORECASE,
)
BLOCKED_PATTERN = re.compile(
    r"access is denied|permission denied|operation not permitted|blocked by policy",
    re.IGNORECASE,
)


RESULTS_START_MARKER = "<!-- factor-benchmark:results:start -->"
RESULTS_END_MARKER = "<!-- factor-benchmark:results:end -->"


def _markdown_cell(value: Any) -> str:
    if value is None or value == "":
        return "—"
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _format_markdown_number(value: Any, digits: int = 6) -> str:
    if value is None or value == "":
        return "—"
    return f"{float(value):.{digits}f}".rstrip("0").rstrip(".")


def render_benchmark_table(report: dict[str, Any]) -> str:
    headers = (
        "测试日期",
        "输出日期 / 区间",
        "输出交易日数",
        "每批交易日",
        "内部批次数",
        "线程",
        "内存限制",
        "日均耗时（秒）",
        "峰值 RSS（MB）",
        "状态",
        "原因",
    )
    output = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    configuration = report.get("configuration", {})
    window = report.get("output_window", {})
    test_date = report.get("test_date", "YYYY-MM-DD")
    output_interval = f"{window.get('begin', '—')}..{window.get('end', '—')}"
    for row in report.get("runs", []):
        values = (
            test_date,
            output_interval,
            window.get("trading_days"),
            row.get("compute_batch_days"),
            row.get("internal_batch_count"),
            configuration.get("threads"),
            configuration.get("memory_limit"),
            _format_markdown_number(row.get("average_seconds_per_trading_day")),
            _format_markdown_number(row.get("peak_rss_mb"), digits=3),
            row.get("status", "pending"),
            row.get("details"),
        )
        output.append(
            "| " + " | ".join(_markdown_cell(value) for value in values) + " |"
        )
    if len(output) == 2:
        output.append(
            "| YYYY-MM-DD | — | — | — | — | — | — | — | — | pending | — |"
        )
    daily = report.get("daily_update", {})
    values = (
        test_date, daily.get("date"), 1, 1, 1,
        configuration.get("threads"), configuration.get("memory_limit"),
        _format_markdown_number(daily.get("average_seconds_per_trading_day")),
        _format_markdown_number(daily.get("peak_rss_mb"), 3),
        daily.get("status", "pending"), daily.get("details"),
    )
    output.append("| " + " | ".join(_markdown_cell(value) for value in values) + " |")
    return "\n".join(output)


def _section_7_bounds(lines: list[str]) -> tuple[int, int]:
    matches = [
        index for index, line in enumerate(lines) if line.strip() == "## 7. 性能测试"
    ]
    if len(matches) != 1:
        raise ValueError(
            "factor README must contain exactly one ## 7. 性能测试 section"
        )
    start = matches[0]
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if re.match(r"^##\s", lines[index])
        ),
        len(lines),
    )
    return start, end


def validate_readme_writeback_target(readme_path: Path) -> None:
    if not readme_path.is_file():
        raise FileNotFoundError(f"factor README not found: {readme_path}")
    lines = readme_path.read_text(encoding="utf-8").splitlines()
    start, end = _section_7_bounds(lines)
    section = lines[start:end]
    starts = [
        i for i, line in enumerate(section) if line.strip() == RESULTS_START_MARKER
    ]
    ends = [i for i, line in enumerate(section) if line.strip() == RESULTS_END_MARKER]
    if len(starts) != 1 or len(ends) != 1:
        raise ValueError("factor README has incomplete or duplicate benchmark markers")
    if starts and starts[0] >= ends[0]:
        raise ValueError("factor README benchmark markers are out of order")


def _replace_results_block(section: list[str], table: str) -> list[str]:
    starts = [
        index
        for index, line in enumerate(section)
        if line.strip() == RESULTS_START_MARKER
    ]
    ends = [
        index
        for index, line in enumerate(section)
        if line.strip() == RESULTS_END_MARKER
    ]
    table_lines = table.splitlines()
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise ValueError("factor README requires one performance results block")
    return [*section[:starts[0] + 1], *table_lines, *section[ends[0]:]]


def update_factor_readme(readme_path: Path, report: dict[str, Any]) -> None:
    lines = readme_path.read_text(encoding="utf-8").splitlines()
    start, end = _section_7_bounds(lines)
    records = [line for line in lines[start:end]
               if line.startswith("正式增量运行记录（")]
    table = render_benchmark_table(report)
    if records:
        table += "\n\n" + "\n".join(records)
    section = _replace_results_block(lines[start:end], table)

    updated = [*lines[:start], *section, *lines[end:]]
    passed = (report.get("all_columns", False)
              and report.get("daily_update", {}).get("status") == "pass"
              and any(row.get("status") == "pass" for row in report.get("runs", []))
              and all(row.get("status") in {"pass", "oom"} for row in report.get("runs", [])))
    _write_atomic(readme_path, set_acceptance("\n".join(updated) + "\n", 5, passed))


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


@dataclass(frozen=True)
class BenchmarkWindow:
    trading_dates: tuple[str, ...]

    @property
    def trading_days(self) -> int:
        return len(self.trading_dates)

    @property
    def begin(self) -> str:
        return self.trading_dates[0]

    @property
    def end(self) -> str:
        return self.trading_dates[-1]


def subtract_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 - months
    year, month_index = divmod(month_index, 12)
    month = month_index + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD format") from exc


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def build_output_window(
    trading_dates: tuple[str, ...] | list[str],
    requested_end: date,
    output_months: int = DEFAULT_OUTPUT_MONTHS,
) -> BenchmarkWindow:
    if output_months <= 0:
        raise ValueError("output_months must be positive")
    dates = sorted(
        {
            date.fromisoformat(value)
            for value in trading_dates
            if date.fromisoformat(value) <= requested_end
        }
    )
    if not dates:
        raise ValueError(
            f"no source calendar dates found through {requested_end.isoformat()}"
        )

    effective_end_date = dates[-1]
    output_begin_date = subtract_months(effective_end_date, output_months)
    if dates[0] > output_begin_date:
        raise ValueError(
            f"source calendar does not cover the full {output_months}-month "
            "benchmark window: "
            f"earliest date is {dates[0].isoformat()}, expected data on or before "
            f"{output_begin_date.isoformat()}"
        )

    window_dates = [
        value for value in dates if output_begin_date <= value <= effective_end_date
    ]
    if not window_dates:
        raise ValueError("no trading dates found for the output benchmark window")
    return BenchmarkWindow(tuple(value.isoformat() for value in window_dates))


def internal_batch_count(window: BenchmarkWindow, compute_batch_days: int) -> int:
    if compute_batch_days <= 0:
        raise ValueError("compute_batch_days must be positive")
    return math.ceil(window.trading_days / compute_batch_days)


def load_trading_dates(
    data_root: Path, requested_end: date, module: Any
) -> tuple[str, ...]:
    with FactorStorage(
        factor_name="_benchmark_calendar",
        data_root=data_root,
        threads=1,
    ) as storage:
        calendar_name = getattr(module, "UPDATE_CALENDAR", "quotes_day")
        tables = (
            getattr(module, "UPDATE_CALENDAR_TABLES", ("quotes_day", "quotes_min"))
            if calendar_name == "quotes_common"
            else (calendar_name,)
        )
        if not tables:
            raise ValueError("benchmark calendar must declare upstream tables")
        return tuple(
            sorted(
                set.intersection(
                    *(
                        set(
                            storage.partition_dates(
                                table, end=requested_end.isoformat()
                            )
                        )
                        for table in tables
                    )
                )
            )
        )


def classify_return_code(return_code: int | None, output: str) -> str:
    if BLOCKED_PATTERN.search(output):
        return "blocked"
    if OOM_PATTERN.search(output):
        return "oom"
    if return_code is None:
        return "blocked"
    if (return_code & 0xFFFFFFFF) in {0xC0000017, 0xC000012D}:
        return "oom"
    return "pass" if return_code == 0 else "fail"


def aggregate_status(
    runs: list[dict[str, Any]],
    expected_run_count: int,
) -> str:
    if len(runs) != expected_run_count:
        return "fail"
    statuses = [run.get("status") for run in runs]
    if not all(status in {"pass", "oom"} for status in statuses):
        return "fail"
    if any(
        run.get("cleanup_ok") is not True or run.get("peak_rss_mb") is None
        for run in runs
    ):
        return "fail"
    return "pass" if all(status == "pass" for status in statuses) else "complete"


def _windows_memory_reader(pid: int) -> tuple[Any, Any]:
    from ctypes import wintypes

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Process32FirstW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ProcessEntry32W),
    ]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ProcessEntry32W),
    ]
    kernel32.Process32NextW.restype = wintypes.BOOL
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ProcessMemoryCounters),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

    process_query_information = 0x0400
    process_vm_read = 0x0010
    process_handles: dict[int, int] = {}

    def process_tree_pids() -> set[int]:
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        invalid_handle = ctypes.c_void_p(-1).value
        if not snapshot or snapshot == invalid_handle:
            raise ctypes.WinError(ctypes.get_last_error())

        children_by_parent: dict[int, list[int]] = {}
        try:
            entry = ProcessEntry32W()
            entry.dwSize = ctypes.sizeof(entry)
            has_entry = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while has_entry:
                children_by_parent.setdefault(
                    int(entry.th32ParentProcessID), []
                ).append(int(entry.th32ProcessID))
                has_entry = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)

        process_ids = {pid}
        pending = [pid]
        while pending:
            parent_pid = pending.pop()
            for child_pid in children_by_parent.get(parent_pid, ()):
                if child_pid not in process_ids:
                    process_ids.add(child_pid)
                    pending.append(child_pid)
        return process_ids

    def read_peak() -> int:
        total_working_set = 0
        for process_id in process_tree_pids():
            handle = process_handles.get(process_id)
            if handle is None:
                handle = kernel32.OpenProcess(
                    process_query_information | process_vm_read,
                    False,
                    process_id,
                )
                if not handle:
                    if process_id == pid:
                        raise ctypes.WinError(ctypes.get_last_error())
                    continue
                process_handles[process_id] = handle

            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            if psapi.GetProcessMemoryInfo(
                handle,
                ctypes.byref(counters),
                counters.cb,
            ):
                total_working_set += int(counters.WorkingSetSize)
        return total_working_set

    def close() -> None:
        for handle in process_handles.values():
            kernel32.CloseHandle(handle)

    return read_peak, close


def _linux_memory_reader(pid: int) -> tuple[Any, Any]:
    status_path = Path(f"/proc/{pid}/status")

    def read_peak() -> int:
        for line in status_path.read_text(encoding="ascii").splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) * 1024
        raise OSError("VmHWM was not present in process status")

    return read_peak, lambda: None


class PeakRssSampler:
    """Sample the calculation process tree high-water RSS."""

    def __init__(self, pid: int) -> None:
        if os.name == "nt":
            self._read_peak, self._close = _windows_memory_reader(pid)
        elif sys.platform.startswith("linux"):
            self._read_peak, self._close = _linux_memory_reader(pid)
        else:
            raise OSError("peak RSS sampling is supported on Windows and Linux")
        self.peak_bytes = 0
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample_loop, daemon=True)

    def _sample(self) -> None:
        try:
            self.peak_bytes = max(self.peak_bytes, self._read_peak())
        except OSError as exc:
            self.error = str(exc)

    def _sample_loop(self) -> None:
        while not self._stop.wait(0.1):
            self._sample()

    def start(self) -> None:
        self._sample()
        self._thread.start()

    def close(self) -> int | None:
        self._stop.set()
        self._thread.join()
        self._sample()
        self._close()
        return self.peak_bytes or None


def _terminate_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            pass
        if process.poll() is None:
            process.kill()
        process.wait()
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def _remove_owned_path(path: Path, data_root: Path) -> None:
    if path.parent.resolve() != data_root.resolve():
        raise ValueError(f"refusing to remove a path outside data root: {path}")
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_junction():
        path.rmdir()
    elif path.is_dir():
        shutil.rmtree(path)


def cleanup_isolated_outputs(data_root: Path, storage_factor_name: str) -> list[str]:
    if not re.fullmatch(r"benchmark_[A-Za-z_][A-Za-z0-9_]*_(?:batch_[1-9][0-9]*|daily)", storage_factor_name):
        raise ValueError("refusing to clean an invalid benchmark storage name")

    stage_name = "_materialize_" + storage_factor_name
    paths = [
        data_root / name
        for name in (
            storage_factor_name,
            stage_name,
            "." + storage_factor_name + ".previous",
            "." + storage_factor_name + ".lock",
        )
    ]
    for suffix in ("staging", "backup"):
        paths.extend(data_root.glob(f".{stage_name}.{suffix}-*"))
    paths.extend(data_root.glob(f".{storage_factor_name}.staging-*"))
    paths.extend(data_root.glob(f".{storage_factor_name}.backup-*"))
    errors = []
    for path in paths:
        try:
            _remove_owned_path(path, data_root)
        except OSError as exc:
            errors.append(f"{path.name}: {exc}")
    return errors


def validate_calculation_output(
    *,
    data_root: Path,
    storage_factor_name: str,
    window: BenchmarkWindow,
    factor_columns: tuple[str, ...],
) -> None:
    """Verify actual output identity and dates after Calculate succeeds."""
    factor_root = data_root / storage_factor_name
    if not read_identity(factor_root) and not parquet_manifest(factor_root):
        return
    require_identity(factor_root, columns=factor_columns)
    output_dates = {
        partition.name.removeprefix("date=")
        for partition in factor_root.glob("date=*")
        if partition.is_dir() and any(partition.rglob("*.parquet"))
    }
    if output_dates - set(window.trading_dates):
        raise ValueError("calculation has output outside the benchmark window")


def _build_command(
    args: argparse.Namespace,
    *,
    window: BenchmarkWindow,
    compute_batch_days: int,
    storage_factor_name: str,
    data_root: Path,
    factor_zoo_root: Path,
    project_root: Path,
    temp_directory: Path,
) -> list[str]:
    calculate_script = (
        Path(__file__).resolve().parents[2]
        / "factor-calculate"
        / "scripts"
        / "run_calculate.py"
    )
    command = [
        sys.executable,
        calculate_script.as_posix(),
        "--factor-name",
        args.factor_name,
        "--begin",
        window.begin,
        "--end",
        window.end,
        "--data-root",
        data_root.as_posix(),
        "--artifacts-root",
        (data_root.parent / "artifacts").as_posix(),
        "--factor-zoo-root",
        factor_zoo_root.as_posix(),
        "--storage-factor-name",
        storage_factor_name,
        "--threads",
        str(args.threads),
        "--temp-directory",
        temp_directory.as_posix(),
        "--project-root",
        project_root.as_posix(),
    ]
    if args.factor_module:
        command.extend(("--factor-module", args.factor_module))
    for column in args.factor_columns or ():
        command.extend(("--factor-column", column))
    command.extend(("--compute-batch-days", str(compute_batch_days)))
    if args.memory_limit:
        command.extend(("--memory-limit", args.memory_limit))
    return command


def require_compute_batch_support(module: Any, factor_name: str) -> None:
    """Reject factors whose calculate entry point cannot honor a batch size."""

    if getattr(module, "SUPPORTS_COMPUTE_BATCH_DAYS", True) is False:
        raise ValueError(
            f"factor {factor_name} opts out of compute_batch_days and cannot be "
            "used to measure single-batch scaling"
        )

    calculate = getattr(module, "calculate_factor", None)
    if not callable(calculate):
        raise ValueError(f"factor {factor_name} has no calculate_factor entry point")

    parameters = inspect.signature(calculate).parameters
    if "context" in parameters:
        return
    accepts_batch = "compute_batch_days" in parameters or "batch_days" in parameters
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    if not accepts_batch and not accepts_kwargs:
        raise ValueError(
            f"factor {factor_name} calculate_factor does not accept compute_batch_days"
        )


def _base_result(
    *,
    window: BenchmarkWindow,
    compute_batch_days: int,
) -> dict[str, Any]:
    return {
        "compute_batch_days": compute_batch_days,
        "internal_batch_count": internal_batch_count(window, compute_batch_days),
    }


def run_calculation_case(
    args: argparse.Namespace,
    *,
    window: BenchmarkWindow,
    compute_batch_days: int,
    storage_factor_name: str,
    temp_root: Path,
    project_root: Path,
    data_root: Path,
    factor_zoo_root: Path,
    preserve_output: bool = False,
    append_history: BenchmarkWindow | None = None,
) -> dict[str, Any]:
    package = resolve_factor_package(
        args.factor_name,
        project_root=project_root,
        factor_module=args.factor_module,
        factor_zoo_root=factor_zoo_root,
    )
    module = load_module_from_path(
        package.module_path,
        project_root=project_root,
        factor_name=args.factor_name,
    )
    declared_columns = resolve_factor_columns(module)
    factor_columns = tuple(args.factor_columns or declared_columns)
    unknown_columns = set(factor_columns) - set(declared_columns)
    if unknown_columns:
        raise ValueError(f"unknown factor columns: {','.join(sorted(unknown_columns))}")

    work_dir = temp_root / storage_factor_name
    work_dir.mkdir(parents=True, exist_ok=False)
    temp_directory = work_dir / "duckdb_temp"
    artifacts_root = data_root.parent / "artifacts"
    factor_data_root = artifacts_root / "factor_data"
    storage_root = factor_data_root / storage_factor_name
    history = parquet_manifest(storage_root) if append_history else {}
    cleanup_errors = [] if append_history else cleanup_isolated_outputs(factor_data_root, storage_factor_name)
    if cleanup_errors:
        raise OSError("sample cleanup failed: " + "; ".join(cleanup_errors))

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["QUANTHUB_FACTOR_THREADS"] = str(args.threads)
    env["QUANTHUB_FACTOR_TEMP_DIRECTORY"] = temp_directory.as_posix()
    if args.memory_limit:
        env["QUANTHUB_FACTOR_MEMORY_LIMIT"] = args.memory_limit
    else:
        env.pop("QUANTHUB_FACTOR_MEMORY_LIMIT", None)

    command = _build_command(
        args,
        window=window,
        compute_batch_days=compute_batch_days,
        storage_factor_name=storage_factor_name,
        data_root=data_root,
        factor_zoo_root=factor_zoo_root,
        project_root=project_root,
        temp_directory=temp_directory,
    )
    started = time.perf_counter()
    return_code: int | None = None
    peak_rss_bytes: int | None = None
    rss_error: str | None = None
    details: str | None = None
    interrupted = False
    timed_out = False
    process: subprocess.Popen[Any] | None = None
    sampler: PeakRssSampler | None = None

    output = ""
    output_lines: list[str] = []
    reader: threading.Thread | None = None

    def stream_output() -> None:
        assert process is not None and process.stdout is not None
        for line in process.stdout:
            output_lines.append(line)
            print(line, end="", flush=True)

    try:
        try:
            process = subprocess.Popen(
                command, cwd=work_dir, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
                start_new_session=(os.name != "nt"),
            )
        except OSError as exc:
            details = f"could not start factor-calculate: {exc}"
        else:
            try:
                sampler = PeakRssSampler(process.pid)
                sampler.start()
            except OSError as exc:
                rss_error = str(exc)
            reader = threading.Thread(target=stream_output, daemon=True)
            reader.start()
            try:
                return_code = process.wait(timeout=args.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process(process)
                process.wait()
            except KeyboardInterrupt:
                interrupted = True
                details = "benchmark interrupted by user"
                _terminate_process(process)
                process.wait()
            finally:
                reader.join()
                output = "".join(output_lines)
                if process.stdout is not None:
                    process.stdout.close()
                if sampler is not None:
                    peak_rss_bytes = sampler.close()
                    rss_error = sampler.error
    finally:
        wall_clock_seconds = time.perf_counter() - started

    if timed_out:
        status = "timeout"
        details = f"calculation exceeded {args.timeout_seconds} seconds"
    elif interrupted:
        status = "blocked"
    else:
        status = classify_return_code(return_code, output)
        if status != "pass" and details is None:
            lines = [line.strip() for line in output.splitlines() if line.strip()]
            details = (
                lines[-1] if lines else f"factor-calculate exited with {return_code}"
            )

    if status == "pass":
        try:
            validate_calculation_output(
                data_root=factor_data_root,
                storage_factor_name=storage_factor_name,
                window=append_history or window,
                factor_columns=factor_columns,
            )
        except Exception as exc:
            status = "fail"
            details = f"output integrity check failed: {type(exc).__name__}: {exc}"
    if status == "pass" and append_history:
        current = parquet_manifest(storage_root)
        if any(current.get(key, {}).get("sha256") != value["sha256"] for key, value in history.items()):
            status = "fail"
            details = "incremental calculation changed existing history"
    if status == "pass" and peak_rss_bytes is None:
        status = "blocked"
        details = f"peak RSS could not be measured: {rss_error or 'no sample'}"

    cleanup_errors = ([] if preserve_output and status == "pass"
                      else cleanup_isolated_outputs(factor_data_root, storage_factor_name))
    try:
        shutil.rmtree(work_dir)
    except FileNotFoundError:
        pass
    except OSError as exc:
        cleanup_errors.append(f"{work_dir.name}: {exc}")
    if cleanup_errors:
        if status == "pass":
            status = "blocked"
        cleanup_details = "isolated output cleanup failed: " + "; ".join(cleanup_errors)
        details = f"{details}; {cleanup_details}" if details else cleanup_details

    result = _base_result(
        window=window,
        compute_batch_days=compute_batch_days,
    )
    result.update(
        {
            "average_seconds_per_trading_day": (
                round(wall_clock_seconds / window.trading_days, 6)
                if status == "pass"
                else None
            ),
            "peak_rss_mb": (
                round(peak_rss_bytes / (1024 * 1024), 3)
                if peak_rss_bytes is not None
                else None
            ),
            "status": status,
            "details": details,
            "cleanup_ok": not cleanup_errors,
            "interrupted": interrupted,
        }
    )
    return result


def _resolve_path(value: str, project_root: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (project_root / path).resolve()


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Measure calculation time and peak RSS for trading-day batches "
            "on one fixed output window."
        )
    )
    parser.add_argument("--factor-name", required=True)
    parser.add_argument("--end", required=True, type=parse_iso_date)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--data-root")
    parser.add_argument("--factor-zoo-root")
    parser.add_argument("--factor-module")
    parser.add_argument("--factor-column", action="append", dest="factor_columns")
    parser.add_argument(
        "--output-months", type=positive_int, default=DEFAULT_OUTPUT_MONTHS
    )
    parser.add_argument(
        "--compute-batch-days",
        nargs="+",
        type=positive_int,
        default=list(DEFAULT_COMPUTE_BATCH_DAYS),
        help="Trading dates per calculation batch (default: 1 5 10 20 30).",
    )
    parser.add_argument("--threads", type=positive_int, default=4)
    parser.add_argument("--memory-limit")
    parser.add_argument(
        "--readme-path",
        help=(
            "Factor README to update; defaults to <FACTORZOO>/<factor-name>/README.md."
        ),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=positive_int,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    return parser


def run_benchmark(args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.factor_name):
        raise ValueError("factor-name must be a valid FactorStorage identifier")
    if len(set(args.compute_batch_days)) != len(args.compute_batch_days):
        raise ValueError("compute-batch-days must not contain duplicates")

    project_root = Path(args.project_root).expanduser().resolve()
    data_root = resolve_data_root(
        project_root=project_root,
        data_root=args.data_root,
    )
    source = data_root.resolve()
    data_root = project_root / ".tmp" / "factor_benchmark" / args.factor_name / "data"
    if source != data_root.resolve():
        if data_root.resolve().is_relative_to(source) or source.is_relative_to(data_root.parent):
            raise ValueError("benchmark workspace must be separate from DATA")
        data_root.mkdir(parents=True, exist_ok=True)
        for table in source.iterdir():
            if (not table.is_dir() or table.name == args.factor_name
                    or table.name.startswith((".", "benchmark_", "validation_", "sample_", "_materialize_"))):
                continue
            link = data_root / table.name
            if link.exists():
                if link.resolve() != table.resolve():
                    raise ValueError(f"unexpected benchmark input: {link}")
            elif os.name == "nt":
                subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(table.resolve())],
                               check=True, capture_output=True)
            else:
                link.symlink_to(table.resolve(), target_is_directory=True)
    factor_zoo_root = resolve_factor_zoo_root(
        project_root=project_root,
        factor_zoo_root=args.factor_zoo_root,
    )
    readme_path = (
        _resolve_path(args.readme_path, project_root)
        if args.readme_path
        else factor_zoo_root / args.factor_name / "README.md"
    )
    if not project_root.is_dir():
        raise FileNotFoundError(f"project root does not exist: {project_root}")
    if not data_root.is_dir():
        raise FileNotFoundError(f"data root does not exist: {data_root}")
    if not factor_zoo_root.is_dir():
        raise FileNotFoundError(f"factor zoo root does not exist: {factor_zoo_root}")
    validate_readme_writeback_target(readme_path)
    update_acceptance(readme_path, 5, False)

    factor_package = resolve_factor_package(
        args.factor_name,
        project_root=project_root,
        factor_module=args.factor_module,
        factor_zoo_root=factor_zoo_root.as_posix(),
    )
    factor_module = load_module_from_path(
        factor_package.module_path,
        project_root=project_root,
        factor_name=args.factor_name,
    )
    require_compute_batch_support(factor_module, args.factor_name)

    trading_dates = load_trading_dates(data_root, args.end, factor_module)
    window = build_output_window(
        trading_dates,
        args.end,
        output_months=args.output_months,
    )
    print(f"Benchmark | {args.factor_name} | {window.begin} -> {window.end} | "
          f"threads={args.threads}, memory={args.memory_limit or 'default'} | "
          f"batch_days={args.compute_batch_days}", flush=True)
    report: dict[str, Any] = {
        "test_date": datetime.now().astimezone().date().isoformat(),
        "configuration": {
            "threads": args.threads,
            "memory_limit": args.memory_limit or "DuckDB default",
        },
        "output_window": {
            "trading_days": window.trading_days,
            "begin": window.begin,
            "end": window.end,
        },
        "all_columns": set(args.factor_columns or resolve_factor_columns(factor_module)) == set(resolve_factor_columns(factor_module)),
        "runs": [],
    }

    with tempfile.TemporaryDirectory(prefix="factor-benchmark-") as temporary_root:
        temp_root = Path(temporary_root)
        for index, compute_batch_days in enumerate(args.compute_batch_days, start=1):
            print(
                f"[{index}/{len(args.compute_batch_days)}] "
                f"{compute_batch_days} trading days per batch "
                f"({internal_batch_count(window, compute_batch_days)} batches)",
                flush=True,
            )
            storage_factor_name = f"benchmark_{args.factor_name}_batch_{compute_batch_days}"
            result = run_calculation_case(
                args,
                window=window,
                compute_batch_days=compute_batch_days,
                storage_factor_name=storage_factor_name,
                temp_root=temp_root,
                project_root=project_root,
                data_root=data_root,
                factor_zoo_root=factor_zoo_root,
            )
            report["runs"].append(result)
            if result["interrupted"]:
                break

        print("Daily append | preparing history (not timed)", flush=True)
        daily_name = f"benchmark_{args.factor_name}_daily"
        common = dict(args=args, storage_factor_name=daily_name,
                      temp_root=temp_root, project_root=project_root,
                      data_root=data_root, factor_zoo_root=factor_zoo_root)
        if window.trading_days < 2 or any(r["interrupted"] for r in report["runs"]):
            daily = dict(status="blocked", details="daily append requires history and an uninterrupted run")
        else:
            passed = [r for r in report["runs"] if r["status"] == "pass"]
            seed_batch = (min(passed, key=lambda r: r["average_seconds_per_trading_day"])["compute_batch_days"]
                          if passed else min(args.compute_batch_days))
            seed = run_calculation_case(
                **common, window=BenchmarkWindow(window.trading_dates[:-1]),
                compute_batch_days=seed_batch, preserve_output=True)
            if seed["status"] == "pass":
                print(f"Daily append | {window.end} | batch_days=1", flush=True)
                daily = run_calculation_case(
                    **common, window=BenchmarkWindow((window.end,)),
                    compute_batch_days=1, append_history=window)
            else:
                daily = dict(status="fail", details=f"history preparation failed: {seed.get('details')}")
        report["daily_update"] = {**daily, "date": window.end}

    completed_batches = {run["compute_batch_days"] for run in report["runs"]}
    for compute_batch_days in args.compute_batch_days:
        if compute_batch_days in completed_batches:
            continue
        row = _base_result(
            window=window,
            compute_batch_days=compute_batch_days,
        )
        row.update(
            {
                "average_seconds_per_trading_day": None,
                "peak_rss_mb": None,
                "status": "blocked",
                "details": "benchmark interrupted before this candidate started",
                "cleanup_ok": True,
                "interrupted": True,
            }
        )
        report["runs"].append(row)

    status = aggregate_status(report["runs"], len(args.compute_batch_days))
    if report["daily_update"]["status"] != "pass":
        status = "fail"
    report["readme_path"] = readme_path.as_posix()
    return report, status


def update_calculate_settings(project_root: Path, factor_name: str,
                              report: dict[str, Any], status: str) -> dict[str, str] | None:
    """Persist the fastest passing batch and its measured resource settings."""
    if (status not in {"pass", "complete"} or not report.get("all_columns")
            or report.get("daily_update", {}).get("status") != "pass"):
        return None
    candidates = [row for row in report["runs"] if row["status"] == "pass"]
    if not candidates:
        return None
    best = min(candidates, key=lambda row: (row["average_seconds_per_trading_day"],
                                           row["peak_rss_mb"], row["compute_batch_days"]))
    path = project_root / ".env"
    if _dotenv_value(path, "FACTOR_WORKFLOW_NAME") != factor_name:
        raise ValueError("set FACTOR_WORKFLOW_NAME to the benchmark factor before saving its configuration")
    config = report["configuration"]
    values = {
        "FACTOR_CALCULATE_THREADS": str(config["threads"]),
        "FACTOR_CALCULATE_MEMORY_LIMIT": (config["memory_limit"]
                                           if config["memory_limit"] != "DuckDB default" else ""),
        "FACTOR_CALCULATE_COMPUTE_BATCH_DAYS": str(best["compute_batch_days"]),
    }
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    remaining = dict(values)
    for index, line in enumerate(lines):
        match = re.match(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=", line)
        if match and match[1] in values:
            lines[index] = f"{match[1]}={values[match[1]]}"
            remaining.pop(match[1], None)
    lines.extend(f"{key}={value}" for key, value in remaining.items())
    fd, name = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=project_root)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(lines) + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return values


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        report, status = run_benchmark(args)
    except (FileNotFoundError, ValueError, OSError) as exc:
        parser.error(str(exc))

    daily = report["daily_update"]
    print(f"Daily append: {daily['status']} | seconds={daily.get('average_seconds_per_trading_day')} | peak_rss_mb={daily.get('peak_rss_mb')}")
    if daily.get("details"):
        print(f"Daily append reason: {daily['details']}")
    print(f"\nBenchmark status: {status}")
    for row in report["runs"]:
        if row.get("details"):
            print(f"compute_batch_days={row['compute_batch_days']}: {row['details']}")
    try:
        update_factor_readme(Path(report["readme_path"]), report)
        selected = update_calculate_settings(Path(args.project_root).resolve(), args.factor_name, report, status)
        if selected:
            print(f"Calculate config saved | batch_days={selected['FACTOR_CALCULATE_COMPUTE_BATCH_DAYS']} | "
                  f"threads={selected['FACTOR_CALCULATE_THREADS']} | "
                  f"memory={selected['FACTOR_CALCULATE_MEMORY_LIMIT'] or 'default'}", flush=True)
        else:
            print("Calculate config unchanged: benchmark acceptance did not pass", flush=True)
    except (OSError, ValueError) as exc:
        print(f"Benchmark writeback failed: {exc}")
        return 1
    print(f"README: {report['readme_path']}")
    return 0 if status in {"pass", "complete"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
