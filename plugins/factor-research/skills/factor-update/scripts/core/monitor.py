

"""Sample a Python process tree's resident memory on Windows or Linux."""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from pathlib import Path
from typing import Callable


def _windows_reader(root_pid: int) -> tuple[Callable[[], int], Callable[[], None]]:
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
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
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

        process_ids = {root_pid}
        pending = [root_pid]
        while pending:
            parent_pid = pending.pop()
            for child_pid in children_by_parent.get(parent_pid, ()):
                if child_pid not in process_ids:
                    process_ids.add(child_pid)
                    pending.append(child_pid)
        return process_ids

    def read_working_set() -> int:
        total = 0
        for process_id in process_tree_pids():
            handle = process_handles.get(process_id)
            if handle is None:
                handle = kernel32.OpenProcess(0x0400 | 0x0010, False, process_id)
                if not handle:
                    if process_id == root_pid:
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
                total += int(counters.WorkingSetSize)
        return total

    def close() -> None:
        for handle in process_handles.values():
            kernel32.CloseHandle(handle)

    return read_working_set, close


def _linux_reader(root_pid: int) -> tuple[Callable[[], int], Callable[[], None]]:
    proc_root = Path("/proc")

    def descendants() -> set[int]:
        children_by_parent: dict[int, list[int]] = {}
        for entry in proc_root.iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                stat = (entry / "stat").read_text(encoding="ascii")
                fields = stat[stat.rfind(")") + 2 :].split()
                parent_pid = int(fields[1])
            except (OSError, IndexError, ValueError):
                continue
            children_by_parent.setdefault(parent_pid, []).append(int(entry.name))

        process_ids = {root_pid}
        pending = [root_pid]
        while pending:
            parent_pid = pending.pop()
            for child_pid in children_by_parent.get(parent_pid, ()):
                if child_pid not in process_ids:
                    process_ids.add(child_pid)
                    pending.append(child_pid)
        return process_ids

    def read_working_set() -> int:
        total = 0
        for process_id in descendants():
            try:
                status = (proc_root / str(process_id) / "status").read_text(
                    encoding="ascii"
                )
            except OSError:
                if process_id == root_pid:
                    raise
                continue
            for line in status.splitlines():
                if line.startswith("VmRSS:"):
                    total += int(line.split()[1]) * 1024
                    break
        return total

    return read_working_set, lambda: None


class ProcessTreeRssSampler:
    """Sample the aggregate working set of a process and its descendants."""

    def __init__(self, pid: int, *, interval_seconds: float = 0.1) -> None:
        self._read, self._close_reader = self._reader(pid)
        self.interval_seconds = interval_seconds
        self.peak_bytes = 0
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample_loop, daemon=True)
        self._started = False

    @staticmethod
    def _reader(pid: int) -> tuple[Callable[[], int], Callable[[], None]]:
        if os.name == "nt":
            return _windows_reader(pid)
        if sys.platform.startswith("linux"):
            return _linux_reader(pid)
        raise OSError("process-tree RSS sampling is supported on Windows and Linux")

    def _sample(self) -> None:
        try:
            self.peak_bytes = max(self.peak_bytes, self._read())
        except OSError as exc:
            self.error = str(exc)

    def _sample_loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self._sample()

    def start(self) -> None:
        self._sample()
        self._started = True
        self._thread.start()

    def close(self) -> int | None:
        if self._started:
            self._stop.set()
            self._thread.join()
            self._sample()
        self._close_reader()
        return self.peak_bytes or None
