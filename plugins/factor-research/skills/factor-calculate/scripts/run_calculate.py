"""Materialize factor data through the standalone Calculate skill."""

from __future__ import annotations

import argparse
import inspect
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Iterator, Mapping, Sequence

from factor_protocol import (
    FactorCalculateContext,
    FactorStorage,
    call_with_supported_kwargs,
    resolve_data_root,
    resolve_factor_data_root,
    resolve_factor_zoo_root,
)
from factor_protocol.config import _dotenv_value
from factor_protocol.fingerprint import factor_definition_fingerprint
from factor_protocol.identity import (
    identity_manifest_matches,
    manifest_digest,
    parquet_manifest,
    read_identity,
    write_identity,
)
from factor_protocol.loader import (
    _project_path,
    load_module_from_path,
    resolve_factor_package,
    select_factor_columns,
)
from factor_protocol.readme import update_acceptance


@contextmanager
def _writer_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            if not stream.read(1):
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _link_or_copy(source: str, target: str) -> str:
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)
    return target


def _committed_materialization_matches(root: Path) -> bool:
    """Verify committed data or a calculation that produced zero rows."""
    if read_identity(root):
        return identity_manifest_matches(root)
    return not parquet_manifest(root)


class Materialization:
    """Stage factor results for an atomic directory replacement."""

    def __init__(self, data_root: Path, factor_name: str) -> None:
        data_root = data_root.resolve()
        factor_name = FactorStorage.identifier(factor_name)
        self.target = data_root / factor_name
        self.stage_name = "_materialize_" + factor_name
        self.stage = data_root / self.stage_name
        self.backup = data_root / ("." + factor_name + ".previous")
        self.lock = data_root / ("." + factor_name + ".lock")

    def recover(self) -> None:
        """Finish or undo an interrupted directory replacement under the lock."""
        if self.backup.exists():
            if not self.target.exists():
                os.replace(self.backup, self.target)
            elif _committed_materialization_matches(self.target):
                shutil.rmtree(self.backup)
            else:
                raise ValueError(
                    "interrupted materialization has invalid committed results"
                )
        if self.stage.exists():
            shutil.rmtree(self.stage)
        for suffix in ("staging", "backup"):
            for path in self.stage.parent.glob(f".{self.stage.name}.{suffix}-*"):
                shutil.rmtree(path)

    @contextmanager
    def prepare(self) -> Iterator[None]:
        with _writer_lock(self.lock):
            self.recover()
            try:
                if self.target.exists():
                    shutil.copytree(
                        self.target, self.stage, copy_function=_link_or_copy
                    )
                else:
                    self.stage.mkdir()
                yield
            finally:
                self.recover()

    def commit(self) -> None:
        if not _committed_materialization_matches(self.stage):
            raise ValueError("staged materialization has no verified result")
        if self.target.exists():
            os.replace(self.target, self.backup)
        try:
            os.replace(self.stage, self.target)
        except BaseException:
            if self.backup.exists():
                os.replace(self.backup, self.target)
            raise


def recover_materialization(data_root: Path, factor_name: str) -> None:
    transaction = Materialization(data_root, factor_name)
    with _writer_lock(transaction.lock):
        transaction.recover()


def prepare_materialization(
    storage: FactorStorage,
    *,
    fingerprint: str,
    factor_name: str,
    columns: Sequence[str],
    begin: str,
    end: str | None,
) -> tuple[dict[str, Any], bool]:
    identity = read_identity(storage.factor_root)
    stored = identity.get("manifest", {})
    before = parquet_manifest(storage.factor_root)
    dates = {path.split("/", 1)[0][5:] for path in before}
    dates.update(day for day in (stored.get("begin"), stored.get("end")) if day)
    dates = sorted(dates)
    rebuild = bool(before or identity) and (
        not identity
        or identity.get("fingerprint") != fingerprint
        or identity.get("factor_name") != factor_name
        or set(identity.get("columns", ())) != set(columns)
        or stored.get("sha256") != manifest_digest(before)
    )
    if (
        rebuild
        and dates
        and (begin > dates[0] or (end is not None and end < dates[-1]))
    ):
        raise ValueError(
            "a changed or unverified factor identity requires recalculating "
            f"the complete stored interval {dates[0]}..{dates[-1]}"
        )
    for day in {path.split("/", 1)[0] for path in before}:
        if begin <= day[5:] and (end is None or day[5:] <= end):
            shutil.rmtree(storage.factor_root / day)
    return before, rebuild


def calculation_dates(
    storage: FactorStorage,
    *,
    begin: str,
    end: str | None,
    calendar: str,
    calendar_tables: Sequence[str] = ("quotes_day", "quotes_min"),
) -> tuple[str, ...]:
    tables = tuple(calendar_tables) if calendar == "quotes_common" else (calendar,)
    if not tables:
        raise ValueError("calculation calendar must declare upstream tables")
    date_sets = [
        set(storage.partition_dates(table, begin=begin, end=end)) for table in tables
    ]
    if any(not dates for dates in date_sets):
        raise ValueError(
            "calculation calendar has no source dates in the requested interval"
        )
    expected = tuple(sorted(set.intersection(*date_sets)))
    if not expected:
        raise ValueError("calculation calendar has no common source dates")
    return expected


def verify_materialization(
    storage: FactorStorage,
    *,
    before: Mapping[str, Any],
    columns: Sequence[str],
    begin: str,
    end: str | None,
    calendar: str,
    calendar_tables: Sequence[str] = ("quotes_day", "quotes_min"),
    require_values: bool = False,
) -> dict[str, Any]:
    after = parquet_manifest(storage.factor_root)
    unchanged_before = {
        path: info
        for path, info in before.items()
        if path.split("/", 1)[0][5:] < begin
        or (end is not None and path.split("/", 1)[0][5:] > end)
    }
    unchanged_after = {
        path: info
        for path, info in after.items()
        if path.split("/", 1)[0][5:] < begin
        or (end is not None and path.split("/", 1)[0][5:] > end)
    }
    if manifest_digest(unchanged_before) != manifest_digest(unchanged_after):
        raise ValueError("calculation changed files outside the requested interval")
    dates = tuple(sorted({path.split("/", 1)[0][5:] for path in after}))
    requested = tuple(
        day for day in dates if begin <= day and (end is None or day <= end)
    )
    expected = set(
        calculation_dates(
            storage,
            begin=begin,
            end=end,
            calendar=calendar,
            calendar_tables=calendar_tables,
        )
    )
    unexpected = sorted(set(requested) - expected)
    if unexpected:
        raise ValueError(
            "calculation has unexpected output partitions: " + ",".join(unexpected)
        )
    projections = ", ".join(storage.identifier(column) for column in columns)
    for offset in range(0, len(requested), 20):
        batch = requested[offset : offset + 20]
        scan = storage.partition_scan_sql(
            storage.factor_name, begin=batch[0], end=batch[-1]
        )
        files = [
            (storage.factor_root / path).as_posix()
            for path in after
            if batch[0] <= path.split("/", 1)[0][5:] <= batch[-1]
        ]
        file_list = ",".join(storage.sql_literal(path) for path in files)
        schemas: dict[str, set[str]] = {}
        for file, column in storage.execute(
            f"SELECT file_name, name FROM parquet_schema([{file_list}])"
        ).fetchall():
            schemas.setdefault(file, set()).add(column)
        for file, schema in schemas.items():
            missing = {"code", *columns} - schema
            if missing:
                raise ValueError(
                    f"{file}: missing output columns " + ",".join(sorted(missing))
                )
        query = f"SELECT date, code, {projections} FROM {scan}"
        finite = " OR ".join(
            f"({column} IS NOT NULL AND NOT isfinite({column}))" for column in columns
        )
        empty_columns = " OR ".join(f"count_{i} = 0" for i in range(len(columns)))
        counts = ", ".join(
            f"COUNT({column}) AS count_{i}" for i, column in enumerate(columns)
        )
        result = storage.execute(f"""
            WITH data AS ({query}), duplicates AS (
                SELECT date, code FROM data GROUP BY date, code HAVING COUNT(*) > 1
            ), coverage AS (SELECT date, {counts} FROM data GROUP BY date)
            SELECT COUNT(*), COUNT(*) FILTER (WHERE date IS NULL OR code IS NULL),
                   (SELECT COUNT(*) FROM duplicates), LIST(DISTINCT CAST(date AS VARCHAR)),
                   COUNT(*) FILTER (WHERE {finite}),
                   (SELECT COUNT(*) FROM coverage WHERE {empty_columns if require_values else "FALSE"})
            FROM data
        """).fetchone()
        if (
            not result
            or result[0] == 0
            or result[1]
            or result[2]
            or result[4]
            or result[5]
            or set(result[3] or ()) != set(batch)
        ):
            raise ValueError(
                "calculation has missing/duplicate keys, non-finite values or empty required columns"
            )
    return after


@dataclass(frozen=True)
class CalculateConfig:
    factor_name: str
    begin: str
    end: str | None = None
    storage_factor_name: str | None = None
    factor_module: str | None = None
    data_root: str | Path | None = None
    artifacts_root: str | Path | None = None
    factor_zoo_root: str | Path | None = None
    threads: int = 4
    compute_batch_days: int | None = None
    factor_columns: tuple[str, ...] | None = None
    memory_limit: str | None = None
    temp_directory: str | Path | None = None
    project_root: str | Path | None = None
    require_values: bool = False
    readme_update: bool = True


def run_calculation(cfg: CalculateConfig) -> Path:
    if cfg.compute_batch_days is not None and cfg.compute_batch_days <= 0:
        raise ValueError("compute_batch_days must be positive")

    project_root = (
        Path(cfg.project_root) if cfg.project_root is not None else Path.cwd()
    )
    package = resolve_factor_package(
        cfg.factor_name,
        project_root=project_root,
        factor_module=cfg.factor_module,
        factor_zoo_root=cfg.factor_zoo_root,
    )
    module = load_module_from_path(
        package.module_path,
        project_root=project_root,
        factor_name=cfg.factor_name,
    )
    factor_columns = select_factor_columns(module, cfg.factor_columns)
    calculate = getattr(module, "calculate_factor", None)
    if not callable(calculate):
        raise ValueError(  # noqa: TRY004 - preserve the existing CLI error contract.
            "calculate function not found: calculate_factor "
            f"in {package.module_path.as_posix()}"
        )
    signature = inspect.signature(calculate)
    accepts_context = "context" in signature.parameters
    storage_factor_name = cfg.storage_factor_name or cfg.factor_name
    data_root = resolve_data_root(
        project_root=project_root,
        data_root=cfg.data_root,
    )
    factor_data_root = resolve_factor_data_root(project_root=project_root, artifacts_root=cfg.artifacts_root)
    readme = (resolve_factor_zoo_root(project_root=project_root, factor_zoo_root=cfg.factor_zoo_root)
              / cfg.factor_name / "README.md")
    maintain_acceptance = cfg.readme_update and storage_factor_name == cfg.factor_name and readme.is_file()
    if maintain_acceptance:
        update_acceptance(readme, 6, False)
    print(f"Output: {(factor_data_root / storage_factor_name).as_posix()}", flush=True)
    print("Calculate phase: preparing data", flush=True)
    source_factors = resolve_factor_data_root(project_root=project_root)
    if source_factors.resolve() != factor_data_root.resolve():
        for name in getattr(module, "UPDATE_SOURCE_FACTORS", ()):
            name = FactorStorage.identifier(name)
            source = source_factors / name
            target = factor_data_root / name
            if target.exists():
                continue
            if not source.is_dir():
                raise FileNotFoundError(f"factor input does not exist: {source}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                subprocess.run(["cmd", "/c", "mklink", "/J", str(target), str(source.resolve())],
                               check=True, capture_output=True)
            else:
                target.symlink_to(source.resolve(), target_is_directory=True)
    transaction = Materialization(factor_data_root, storage_factor_name)
    with transaction.prepare():
        storage = FactorStorage(
            data_root=data_root,
            artifacts_root=cfg.artifacts_root,
            project_root=project_root,
            factor_name=transaction.stage_name,
            threads=cfg.threads,
            memory_limit=cfg.memory_limit,
            temp_directory=(
                _project_path(cfg.temp_directory, project_root=project_root)
                if cfg.temp_directory is not None
                else None
            ),
        )
        try:
            fingerprint = factor_definition_fingerprint(package)
            calendar = getattr(module, "UPDATE_CALENDAR", "quotes_day")
            calendar_tables = getattr(
                module, "UPDATE_CALENDAR_TABLES", ("quotes_day", "quotes_min")
            )
            expected = calculation_dates(
                storage,
                begin=cfg.begin,
                end=cfg.end,
                calendar=calendar,
                calendar_tables=calendar_tables,
            )
            full_history = False
            if maintain_acceptance:
                full_dates = calculation_dates(storage, begin="0001-01-01", end=None,
                                               calendar=calendar, calendar_tables=calendar_tables)
                full_begin = _dotenv_value(project_root / ".env", "FACTOR_CALCULATE_BEGIN") or full_dates[0]
                full_end = _dotenv_value(project_root / ".env", "FACTOR_CALCULATE_END") or full_dates[-1]
                required_dates = tuple(day for day in full_dates if full_begin <= day <= full_end)
                full_history = (bool(required_dates) and expected[0] <= required_dates[0]
                                and expected[-1] >= required_dates[-1]
                                and set(factor_columns) == set(select_factor_columns(module, None)))
            before, replace_history = prepare_materialization(
                storage,
                fingerprint=fingerprint,
                factor_name=package.factor_name,
                columns=factor_columns,
                begin=cfg.begin,
                end=expected[-1],
            )
            supports_batch_days = getattr(module, "SUPPORTS_COMPUTE_BATCH_DAYS", True)
            compute_batch_days = cfg.compute_batch_days if supports_batch_days else None
            if cfg.compute_batch_days is not None and not supports_batch_days:
                print(
                    "compute_batch_days is not supported by this factor; "
                    "using the factor's configured batch size",
                    flush=True,
                )

            print(f"Calculate phase: computing | trading_days={len(expected)} | columns={len(factor_columns)}", flush=True)
            batch_size = compute_batch_days or len(expected)
            batches = [expected[offset:offset + batch_size]
                       for offset in range(0, len(expected), batch_size)]
            started = monotonic()
            completed = 0
            for index, dates in enumerate(batches, 1):
                batch_begin, batch_end = dates[0], dates[-1]
                batch_started = monotonic()
                print(f"Batch {index}/{len(batches)} | {batch_begin} -> {batch_end} | "
                      f"completed={completed}/{len(expected)} trading days", flush=True)
                if accepts_context:
                    calculate(
                        context=FactorCalculateContext(
                            storage=storage,
                            factor_name=cfg.factor_name,
                            begin=batch_begin,
                            end=batch_end,
                            data_root=data_root.as_posix(),
                            output_dir=package.dist_dir,
                            threads=cfg.threads,
                            factor_columns=factor_columns,
                            compute_batch_days=compute_batch_days,
                        )
                    )
                else:
                    calculation_kwargs = {
                        "storage": storage,
                        "factor_name": cfg.factor_name,
                        "storage_factor_name": transaction.stage_name,
                        "begin": batch_begin,
                        "end": batch_end,
                        "data_root": data_root.as_posix(),
                        "threads": cfg.threads,
                        "memory_limit": cfg.memory_limit,
                        "temp_directory": (
                            _project_path(cfg.temp_directory, project_root=project_root)
                            if cfg.temp_directory is not None
                            else None
                        ),
                        "factor_zoo_root": cfg.factor_zoo_root,
                        "output_dir": package.dist_dir,
                        "factor_columns": factor_columns,
                    }
                    if supports_batch_days:
                        calculation_kwargs["compute_batch_days"] = compute_batch_days
                    call_with_supported_kwargs(calculate, calculation_kwargs)
                completed += len(dates)
                elapsed = monotonic() - started
                remaining = elapsed / completed * (len(expected) - completed)
                print(f"Progress: {completed / len(expected):.1%} | "
                      f"batches={index}/{len(batches)} | days={completed}/{len(expected)} | "
                      f"batch={monotonic() - batch_started:.1f}s | elapsed={elapsed:.1f}s | "
                      f"ETA={remaining:.1f}s", flush=True)
            print("Calculate phase: verifying output", flush=True)
            manifest = verify_materialization(
                storage,
                before=before,
                columns=factor_columns,
                begin=cfg.begin,
                end=cfg.end,
                calendar=calendar,
                calendar_tables=calendar_tables,
                require_values=cfg.require_values,
            )
            if manifest and (replace_history or manifest != before):
                write_identity(
                    storage,
                    package=package,
                    factor_columns=factor_columns,
                    fingerprint=fingerprint,
                    manifest=manifest,
                )
            elif not manifest:
                (storage.factor_root / "identity.json").unlink(missing_ok=True)

        finally:
            storage.close()
        print("Calculate phase: committing data", flush=True)
        transaction.commit()
    if maintain_acceptance:
        update_acceptance(readme, 6, bool(manifest) and full_history)
    return factor_data_root / storage_factor_name


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Materialize a factor package with DuckDB."
    )
    parser.add_argument("--factor-name", required=True)
    parser.add_argument("--begin")
    parser.add_argument("--end")
    parser.add_argument(
        "--data-root",
        help="Raw data root; defaults to DATA in the project .env file.",
    )
    parser.add_argument("--artifacts-root", help="Derived data and evaluation root; defaults to ARTIFACTS.")
    parser.add_argument("--factor-zoo-root")
    parser.add_argument("--output-dir", help="Canonical audit directory for calculation finalization.")
    parser.add_argument("--factor-module")
    parser.add_argument(
        "--storage-factor-name",
        help="Write results under a different factor storage name.",
    )
    parser.add_argument("--no-readme-update", action="store_true")
    parser.add_argument("--factor-column", action="append", dest="factor_columns")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument(
        "--compute-batch-days",
        type=int,
        help="Number of trading dates produced by each calculation batch.",
    )
    parser.add_argument("--memory-limit")
    parser.add_argument("--temp-directory")
    parser.add_argument("--project-root", default=".")
    parser.add_argument(
        "--require-values",
        action="store_true",
        help="Require a finite non-null value per column and output date.",
    )
    parser.add_argument(
        "--recover-only",
        action="store_true",
        help="Recover an interrupted dataset replacement and exit.",
    )
    args = parser.parse_args()
    root = Path(args.project_root).resolve()
    if args.recover_only:
        recover_materialization(
            resolve_factor_data_root(project_root=root, artifacts_root=args.artifacts_root),
            FactorStorage.identifier(args.storage_factor_name or args.factor_name),
        )
        return 0
    if not args.begin:
        parser.error("--begin is required for calculation")
    finalize = (not args.no_readme_update and not args.factor_columns
                and (args.storage_factor_name is None or args.storage_factor_name == args.factor_name))
    readme = (resolve_factor_zoo_root(project_root=root, factor_zoo_root=args.factor_zoo_root)
              / args.factor_name / "README.md")
    if finalize and readme.is_file():
        update_acceptance(readme, 4, False)
    print(f"Calculate | {args.factor_name} | {args.begin} -> {args.end or 'latest'}", flush=True)
    print(f"Resources: threads={args.threads}, memory={args.memory_limit or 'default'}, "
          f"batch_days={args.compute_batch_days or 'factor_default'}", flush=True)
    path = run_calculation(
        CalculateConfig(
            factor_name=args.factor_name,
            begin=args.begin,
            end=args.end,
            data_root=args.data_root,
            artifacts_root=args.artifacts_root,
            factor_zoo_root=args.factor_zoo_root,
            factor_module=args.factor_module,
            storage_factor_name=args.storage_factor_name,
            factor_columns=(
                tuple(args.factor_columns) if args.factor_columns else None
            ),
            threads=args.threads,
            compute_batch_days=args.compute_batch_days,
            memory_limit=args.memory_limit,
            temp_directory=args.temp_directory,
            project_root=root,
            require_values=args.require_values,
            readme_update=not args.no_readme_update,
        )
    )
    if finalize:
        print("Calculate phase: independent sample audit", flush=True)
        command = [sys.executable, str(Path(__file__).resolve().parents[2]
                   / "factor-validate" / "scripts" / "run_validate.py"),
                   "--project-root", str(root), "--factor-name", args.factor_name,
                   "--data-root", str(resolve_data_root(project_root=root, data_root=args.data_root)),
                   "--artifacts-root", str(path.parent.parent), "--begin", args.begin,
                   "--end", args.end or max((part.name.removeprefix("date=") for part in path.glob("date=*") if part.is_dir()), default=args.begin),
                   "--threads", str(args.threads)]
        for flag, value in (("--factor-zoo-root", args.factor_zoo_root),
                            ("--output-dir", args.output_dir),
                            ("--memory-limit", args.memory_limit),
                            ("--temp-directory", args.temp_directory)):
            if value is not None:
                command.extend((flag, str(value)))
        try:
            result = subprocess.run(command, check=False)
        except OSError:
            if readme.is_file():
                update_acceptance(readme, 6, False)
            raise
        if result.returncode:
            if readme.is_file():
                update_acceptance(readme, 6, False)
            print("Calculate failed: independent sample audit did not pass", flush=True)
            return result.returncode
    print(f"Calculate pass | output={path.as_posix()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
