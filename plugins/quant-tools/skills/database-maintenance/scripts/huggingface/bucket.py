"""Hugging Face Bucket URIs and retryable CLI synchronization."""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SyncTarget:
    name: str
    source: Path
    destination: str
    include: str | None = None


def hf_executable() -> str:
    exe_name = "hf.exe" if os.name == "nt" else "hf"
    candidate = Path(sys.executable).with_name(exe_name)
    if candidate.exists():
        return str(candidate)
    return "hf"


def destination_uri(bucket_id: str, remote_prefix: str, name: str) -> str:
    prefix = remote_prefix.strip("/")
    return f"hf://buckets/{bucket_id}/{prefix}/{name}" if prefix else f"hf://buckets/{bucket_id}/{name}"


def run_sync_target(target: SyncTarget, *, token: str, dry_run: bool, delete_remote_extra: bool, max_retries: int, retry_sleep_seconds: float) -> None:
    command = [
        hf_executable(),
        "buckets",
        "sync",
        target.source.as_posix(),
        target.destination,
        "--token",
        token,
    ]
    if target.include:
        command.extend(["--include", target.include])
    if dry_run:
        command.append("--dry-run")
    if delete_remote_extra:
        command.append("--delete")

    for attempt in range(1, max_retries + 1):
        started_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
        print(f"SYNC_START name={target.name} attempt={attempt} started_at_utc={started_at}")
        process = subprocess.run(command, text=True)
        if process.returncode == 0:
            finished_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
            print(f"SYNC_DONE name={target.name} attempt={attempt} finished_at_utc={finished_at}")
            return
        print(f"SYNC_FAIL name={target.name} attempt={attempt} return_code={process.returncode}")
        if attempt < max_retries:
            print(f"Sleeping {retry_sleep_seconds:.1f}s before retrying {target.name}")
            time.sleep(retry_sleep_seconds)
    raise RuntimeError(f"Sync failed after {max_retries} attempts: {target.name}")
