"""Configuration, planning and result contracts for factor updates."""
from __future__ import annotations
from dataclasses import dataclass
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
