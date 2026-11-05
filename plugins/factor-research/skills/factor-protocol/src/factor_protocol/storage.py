from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import os
from pathlib import Path
import re
import shutil
from typing import Any, Callable, Iterable, Mapping, Sequence
from uuid import uuid4

import duckdb
import pandas as pd

from .config import resolve_artifacts_root, resolve_data_root, resolve_factor_zoo_root
from .paths import validate_identifier


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class FactorStoragePaths:
    data_root: Path
    artifacts_root: Path
    factor_data_root: Path
    factor_root: Path


class FactorStorage:
    """DuckDB access to project tables and factor outputs."""

    def __init__(
        self,
        factor_name: str,
        data_root: str | Path | None = None,
        *,
        artifacts_root: str | Path | None = None,
        factor_root: str | Path | None = None,
        db_path: str | Path = ":memory:",
        threads: int | None = None,
        temp_directory: str | Path | None = None,
        memory_limit: str | None = None,
        duckdb_config: Mapping[str, Any] | None = None,
        duckdb_settings: Mapping[str, Any] | None = None,
        project_root: str | Path | None = None,
    ) -> None:
        # Nested validators may create another FactorStorage.
        # Keep those connections inside the same resource envelope.
        effective_threads = int(
            threads
            if threads is not None
            else os.environ.get("QUANTHUB_FACTOR_THREADS", "4")
        )
        effective_temp_directory = temp_directory or os.environ.get(
            "QUANTHUB_FACTOR_TEMP_DIRECTORY"
        )
        effective_memory_limit = memory_limit or os.environ.get(
            "QUANTHUB_FACTOR_MEMORY_LIMIT"
        )

        self.data_root = resolve_data_root(
            project_root=project_root or Path.cwd(),
            data_root=data_root,
        )
        self.con = duckdb.connect(str(db_path), config=dict(duckdb_config or {}))
        self.configure("threads", effective_threads)
        self.configure("preserve_insertion_order", False)
        if effective_temp_directory is not None:
            temp_path = Path(effective_temp_directory)
            temp_path.mkdir(parents=True, exist_ok=True)
            self.configure("temp_directory", temp_path.as_posix())
        if effective_memory_limit is not None:
            self.configure("memory_limit", effective_memory_limit)
        for name, value in (duckdb_settings or {}).items():
            self.configure(name, value)

        self.factor_name = self.identifier(factor_name)
        self.artifacts_root = resolve_artifacts_root(
            project_root=project_root or Path.cwd(), artifacts_root=artifacts_root,
        )
        self.factor_zoo_root = resolve_factor_zoo_root(project_root=project_root or Path.cwd())
        self.factor_data_root = self.artifacts_root / "factor_data"
        self.factor_root = (
            Path(factor_root) if factor_root is not None
            else self.factor_data_root / self.factor_name
        )
        if not self.factor_root.is_absolute():
            self.factor_root = Path(project_root or Path.cwd()) / self.factor_root
        self.paths = FactorStoragePaths(
            data_root=self.data_root,
            artifacts_root=self.artifacts_root,
            factor_data_root=self.factor_data_root,
            factor_root=self.factor_root,
        )
        self._validation_factor_scope: tuple[str, str] | None = None

    def table_root(self, table: str) -> Path:
        name = self.identifier(table)
        if name == self.factor_name:
            return self.factor_root
        if ((self.factor_data_root / name).is_dir()
                or (self.factor_zoo_root / name / "scripts" / "main.py").is_file()):
            return self.factor_data_root / name
        return self.data_root / name

    @staticmethod
    def identifier(value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError(f"invalid SQL identifier: {value!r}")
        return value

    @staticmethod
    def sql_literal(value: str | Path) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    def configure(self, name: str, value: Any) -> None:
        setting = self.identifier(name)
        if isinstance(value, bool):
            literal = "TRUE" if value else "FALSE"
        elif isinstance(value, (int, float)):
            literal = str(value)
        else:
            literal = self.sql_literal(str(value))
        self.execute(f"SET {setting} = {literal}")

    def execute(
        self,
        sql: str,
        parameters: Sequence[Any] | Mapping[str, Any] | None = None,
    ) -> duckdb.DuckDBPyConnection:
        if parameters is None:
            return self.con.execute(sql)
        return self.con.execute(sql, parameters)

    def executemany(
        self,
        sql: str,
        parameters: Iterable[Sequence[Any]],
    ) -> duckdb.DuckDBPyConnection:
        return self.con.executemany(sql, parameters)

    def df(
        self,
        sql: str,
        parameters: Sequence[Any] | Mapping[str, Any] | None = None,
    ) -> pd.DataFrame:
        return self.execute(sql, parameters).df()

    def create_view(
        self,
        name: str,
        query: str,
        *,
        replace: bool = True,
        temporary: bool = False,
    ) -> None:
        view = self.identifier(name)
        replace_sql = "OR REPLACE " if replace else ""
        temporary_sql = "TEMP " if temporary else ""
        self.execute(f"CREATE {replace_sql}{temporary_sql}VIEW {view} AS {query}")

    def drop_relation(
        self,
        name: str,
        *,
        kind: str = "TABLE",
        if_exists: bool = True,
    ) -> None:
        relation = self.identifier(name)
        normalized_kind = kind.upper()
        if normalized_kind not in {"TABLE", "VIEW"}:
            raise ValueError("kind must be TABLE or VIEW")
        exists_sql = "IF EXISTS " if if_exists else ""
        self.execute(f"DROP {normalized_kind} {exists_sql}{relation}")

    def register_dataframe(self, name: str, frame: pd.DataFrame) -> None:
        self.con.register(self.identifier(name), frame)

    def unregister(self, name: str) -> None:
        self.con.unregister(self.identifier(name))

    def register_parquet_view(
        self,
        name: str,
        path: str | Path,
        *,
        hive_partitioning: bool = True,
        union_by_name: bool = True,
    ) -> None:
        options = [
            f"HIVE_PARTITIONING={int(hive_partitioning)}",
            f"union_by_name={int(union_by_name)}",
        ]
        query = (
            f"SELECT * FROM parquet_scan({self.sql_literal(path)}, "
            + ", ".join(options)
            + ")"
        )
        self.create_view(name, query)

    @staticmethod
    def _partition_candidates(
        root: Path,
        start: date | None,
        stop: date | None,
    ) -> Iterable[tuple[date, Path]]:
        if start is not None and stop is not None:
            current = start
            while current <= stop:
                yield current, root / f"date={current.isoformat()}"
                current += timedelta(days=1)
            return

        for partition in sorted(root.glob("date=*"), key=lambda path: path.name):
            if not partition.is_dir():
                continue
            try:
                partition_date = date.fromisoformat(
                    partition.name.removeprefix("date=")
                )
            except ValueError:
                continue
            yield partition_date, partition

    def partition_scan_sql(
        self,
        table: str,
        *,
        begin: str | None = None,
        end: str | None = None,
    ) -> str:
        """Return a scan over existing date-partitioned parquet files.

        The regular table view intentionally uses a recursive glob for broad
        exploratory queries.  Long-running factor materialization instead
        needs to avoid schema discovery over every historical file on every
        connection, so this helper resolves the requested Hive partitions once
        and passes the concrete files directly to DuckDB.  Bounded calls
        resolve only the calendar dates inside the requested range.
        """
        table_name = self.identifier(table)
        source = self.table_root(table_name)
        if not source.exists():
            raise FileNotFoundError(f"partitioned table does not exist: {source}")

        start = date.fromisoformat(begin) if begin is not None else None
        stop = date.fromisoformat(end) if end is not None else None
        if start is not None and stop is not None and start > stop:
            raise ValueError("begin must not be after end")

        paths: list[str] = []
        for partition_date, partition in self._partition_candidates(
            source, start, stop
        ):
            if start is not None and partition_date < start:
                continue
            if stop is not None and partition_date > stop:
                continue
            if not partition.is_dir():
                continue
            paths.extend(
                file.as_posix()
                for file in sorted(
                    partition.rglob("*.parquet"), key=lambda path: path.as_posix()
                )
                if file.is_file() and file.stat().st_size > 0
            )

        if not paths:
            raise FileNotFoundError(
                f"no non-empty parquet partitions found for {table_name}"
            )
        literals = ", ".join(self.sql_literal(path) for path in paths)
        return f"parquet_scan([{literals}], HIVE_PARTITIONING=1, union_by_name=1)"

    def partition_dates(
        self,
        table: str,
        *,
        begin: str | None = None,
        end: str | None = None,
    ) -> tuple[str, ...]:
        """Return non-empty Hive partition dates without opening DuckDB files.

        Date-end probes used to register a full-history parquet view and run
        ``MAX(date)``.  Factor calculate paths only need the directory
        calendar at that point; resolving it from Hive partition names avoids
        a second full-file schema/data scan before the bounded batch begins.
        """

        table_name = self.identifier(table)
        root = self.table_root(table_name)
        if not root.exists():
            raise FileNotFoundError(f"partitioned table does not exist: {root}")
        start = date.fromisoformat(begin) if begin is not None else None
        stop = date.fromisoformat(end) if end is not None else None
        if start is not None and stop is not None and start > stop:
            raise ValueError("begin must not be after end")
        values: list[str] = []
        for value, partition in self._partition_candidates(root, start, stop):
            if start is not None and value < start:
                continue
            if stop is not None and value > stop:
                continue
            try:
                has_data = any(
                    path.is_file() and path.stat().st_size > 0
                    for path in partition.rglob("*.parquet")
                )
            except OSError:
                has_data = False
            if has_data:
                values.append(value.isoformat())
        return tuple(values)

    def partition_date_batches(
        self,
        table: str,
        *,
        batch_days: int,
        begin: str | None = None,
        end: str | None = None,
    ) -> tuple[tuple[str, str], ...]:
        """Group available partition dates into fixed-size date ranges."""

        if batch_days <= 0:
            raise ValueError("batch_days must be positive")
        dates = self.partition_dates(table, begin=begin, end=end)
        return tuple(
            (dates[offset], dates[min(offset + batch_days, len(dates)) - 1])
            for offset in range(0, len(dates), batch_days)
        )

    def copy_query_to_parquet(
        self,
        query: str,
        path: str | Path,
        *,
        partition_by: Sequence[str] = (),
        overwrite: bool = True,
        compression: str = "zstd",
        validate_staged: Callable[[Path], None] | None = None,
    ) -> None:
        """Copy a query to parquet, optionally validating staged partitions.

        ``validate_staged`` is called with the temporary staging root after
        DuckDB has materialized the query and before any target partition is
        replaced.  It is intentionally limited to the atomic, partitioned
        overwrite path so a failed validation leaves the existing dataset
        untouched.
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if validate_staged is not None and (not partition_by or not overwrite):
            raise ValueError(
                "validate_staged requires partitioned overwrite materialization"
            )
        options = [
            "FORMAT PARQUET",
            f"COMPRESSION {self.identifier(compression)}",
        ]
        if partition_by:
            columns = ", ".join(self.identifier(column) for column in partition_by)
            options.append(f"PARTITION_BY ({columns})")
        if overwrite:
            options.append("OVERWRITE_OR_IGNORE TRUE")
        if not partition_by or not overwrite:
            if partition_by:
                target.mkdir(parents=True, exist_ok=True)
            self.execute(
                f"COPY ({query}) TO {self.sql_literal(target.as_posix())} "
                f"({', '.join(options)})"
            )
            return

        # DuckDB's partitioned OVERWRITE_OR_IGNORE overwrites colliding file
        # names, but a smaller rerun can leave older ``data_N.parquet`` files
        # behind in the same Hive partition.  Readers then see duplicate
        # (date, code) rows.  Write the new partitions to a sibling staging
        # root first and replace each completed partition directory as a unit.
        staging_root = target.parent / f".{target.name}.staging-{uuid4().hex}"
        backup_root = target.parent / f".{target.name}.backup-{uuid4().hex}"
        copied_partitions: list[Path] = []
        backups: list[tuple[Path, Path]] = []
        try:
            staging_options = [
                option for option in options if option != "OVERWRITE_OR_IGNORE TRUE"
            ]
            self.execute(
                f"COPY ({query}) TO {self.sql_literal(staging_root.as_posix())} "
                f"({', '.join(staging_options)})"
            )

            depth = len(partition_by)
            partition_relatives = sorted(
                {
                    Path(*file.relative_to(staging_root).parts[:depth])
                    for file in staging_root.rglob("*.parquet")
                    if len(file.relative_to(staging_root).parts) > depth
                },
                key=lambda value: value.as_posix(),
            )
            if not partition_relatives:
                return
            if validate_staged is not None:
                validate_staged(staging_root)

            for relative in partition_relatives:
                staged_partition = staging_root / relative
                target_partition = target / relative
                backup_partition = backup_root / relative
                if not staged_partition.exists():
                    raise RuntimeError(
                        f"staged parquet partition is missing: {staged_partition}"
                    )
                target_partition.parent.mkdir(parents=True, exist_ok=True)
                if target_partition.exists():
                    backup_partition.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(target_partition, backup_partition)
                    backups.append((target_partition, backup_partition))
                try:
                    os.replace(staged_partition, target_partition)
                    copied_partitions.append(target_partition)
                except Exception:
                    if backup_partition.exists():
                        os.replace(backup_partition, target_partition)
                    raise
            if backup_root.exists():
                shutil.rmtree(backup_root)
        except Exception:
            for target_partition in reversed(copied_partitions):
                if target_partition.exists():
                    shutil.rmtree(target_partition)
            for target_partition, backup_partition in reversed(backups):
                if backup_partition.exists():
                    target_partition.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup_partition, target_partition)
            raise
        finally:
            if staging_root.exists():
                shutil.rmtree(staging_root)
            if backup_root.exists():
                shutil.rmtree(backup_root)

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> FactorStorage:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def set_validation_factor_scope(
        self,
        begin: str | None,
        end: str | None,
    ) -> None:
        """Bound factor reads to the current audit window."""

        if begin is None or end is None:
            self._validation_factor_scope = None
            return
        self._validation_factor_scope = (str(begin), str(end))

    def clear_validation_factor_scope(self) -> None:
        self._validation_factor_scope = None

    def register_data_view(
        self,
        name: str,
        *,
        path: str | Path | None = None,
        begin: str | None = None,
        end: str | None = None,
    ) -> None:
        """Register a base-table view, optionally restricted to date partitions.

        Factor materialisation is normally split into rolling batches.  A
        plain parquet glob still makes DuckDB inspect the complete history for
        every batch, even when the SQL has a date predicate.  When the table
        uses the repository's standard ``date=YYYY-MM-DD`` layout, resolve the
        requested partitions up front and expose only those files through the
        view.  The unbounded path remains the default for probes and validators
        that intentionally need the whole table.
        """
        if (begin is not None or end is not None) and path is None:
            source_sql = self.partition_scan_sql(name, begin=begin, end=end)
            self.create_view(name, f"SELECT * FROM {source_sql}")
            return
        source = Path(path) if path is not None else self.table_root(name)
        glob = source.as_posix().rstrip("/") + "/date=*/*.parquet"
        self.register_parquet_view(name, glob)

    def factor_date_range(self) -> tuple[str, str] | None:
        """Return the materialized factor date range from Hive partitions."""
        if not self.factor_root.exists():
            return None
        dates: list[str] = []
        for partition in self.factor_root.glob("date=*"):
            if not partition.is_dir():
                continue
            match = re.fullmatch(r"date=(\d{4}-\d{2}-\d{2})", partition.name)
            if match:
                dates.append(match.group(1))
        if not dates:
            return None
        dates.sort()
        return dates[0], dates[-1]

    def register_factor_view(
        self,
        name: str = "factors",
        *,
        allow_missing: bool = False,
    ) -> None:
        scope = self._validation_factor_scope if name == "factors" else None
        if scope is not None:
            try:
                source = self.partition_scan_sql(
                    self.factor_name,
                    begin=scope[0],
                    end=scope[1],
                )
            except FileNotFoundError:
                if not allow_missing:
                    raise
                self.create_view(
                    name,
                    """
                    SELECT
                        CAST(NULL AS VARCHAR) AS code,
                        CAST(NULL AS DATE) AS date
                    WHERE FALSE
                    """,
                )
                return
            self.create_view(name, f"SELECT * FROM {source}")
            return
        glob = self.factor_root.as_posix().rstrip("/") + "/date=*/*.parquet"
        try:
            self.register_parquet_view(name, glob)
        except duckdb.IOException:
            if not allow_missing:
                raise
            self.create_view(
                name,
                """
                SELECT
                    CAST(NULL AS VARCHAR) AS code,
                    CAST(NULL AS DATE) AS date
                WHERE FALSE
                """,
            )


    def write_factor_query(self, query: str, *, check_duplicates: bool = True) -> None:
        """Materialize a factor query with a single production SQL execution.

        The query is copied to an isolated staging root first.  Duplicate-key
        checks read those staged parquet files, so a failed check leaves the
        current factor partitions untouched and does not execute the factor
        SQL a second time.
        """
        self.create_view("_factor_output", query, temporary=True)
        try:
            columns = [
                row[0] for row in self.execute("DESCRIBE _factor_output").fetchall()
            ]
            if "code" not in columns or "date" not in columns:
                raise ValueError("factor query must return code and date columns")
            factor_columns = [
                column for column in columns if column not in ("code", "date", "time")
            ]
            if not factor_columns:
                raise ValueError("factor query must return at least one factor column")
            selected = ["CAST(code AS VARCHAR) AS code", "CAST(date AS DATE) AS date"]
            selected.extend(
                f"CAST({self.identifier(column)} AS DOUBLE) AS {self.identifier(column)}"
                for column in factor_columns
            )

            def validate_staged(staging_root: Path) -> None:
                if not check_duplicates:
                    return
                staged_glob = staging_root.as_posix().rstrip("/") + "/**/*.parquet"
                staged_source = (
                    f"parquet_scan({self.sql_literal(staged_glob)}, "
                    "HIVE_PARTITIONING=1, union_by_name=1)"
                )
                duplicate = self.execute(
                    f"""
                    SELECT 1
                    FROM {staged_source}
                    GROUP BY code, date
                    HAVING COUNT(*) > 1
                    LIMIT 1
                    """
                ).fetchone()
                if duplicate is not None:
                    raise ValueError(
                        "factor output contains duplicate (code, date) keys"
                    )

            self.copy_query_to_parquet(
                f"SELECT {', '.join(selected)} FROM _factor_output",
                self.factor_root,
                partition_by=("date",),
                validate_staged=validate_staged,
            )
        finally:
            self.drop_relation("_factor_output", kind="VIEW")

    def write_factors(self, frame: pd.DataFrame) -> None:
        """Materialize a factor DataFrame using the standard parquet writer."""
        if not isinstance(frame, pd.DataFrame):
            raise TypeError("factor frame must be a pandas DataFrame")
        if "code" not in frame.columns or "date" not in frame.columns:
            if frame.index.names and {"code", "date"}.issubset(frame.index.names):
                frame = frame.reset_index()
        if "code" not in frame.columns or "date" not in frame.columns:
            raise ValueError("factor frame must contain code and date columns")
        factor_columns = [
            column for column in frame.columns if column not in ("code", "date", "time")
        ]
        if not factor_columns:
            raise ValueError("factor frame must contain at least one factor column")
        self.register_dataframe("_factor_frame", frame)
        try:
            selected = ["code", "date", *factor_columns]
            self.write_factor_query(
                "SELECT "
                + ", ".join(self.identifier(column) for column in selected)
                + " FROM _factor_frame"
            )
        finally:
            self.unregister("_factor_frame")

    def select_sql(
        self,
        table: str,
        columns: Sequence[str] | None = None,
        *,
        where: str | Sequence[str] | None = None,
        order_by: Sequence[str] = (),
    ) -> str:
        """Assemble a SELECT statement for a registered relation or data table.

        ``where`` accepts caller-authored SQL conditions without the ``WHERE``
        keyword. A sequence is joined with ``AND``. Values should be escaped
        with :meth:`sql_literal` or bound in a separately authored query.
        """
        table_name = validate_identifier(table, "table")
        source = self._table_scan_source(table_name)
        if columns is None:
            selected = "*"
        else:
            if not columns:
                raise ValueError("columns must not be empty")
            selected = ", ".join(self.identifier(column) for column in columns)

        query = f"SELECT {selected} FROM {source}"
        if isinstance(where, str):
            condition = where.strip()
        elif where is None:
            condition = ""
        else:
            condition = " AND ".join(part.strip() for part in where if part.strip())
        if condition:
            query += f" WHERE {condition}"
        if order_by:
            order = ", ".join(self.identifier(column) for column in order_by)
            query += f" ORDER BY {order}"
        return query

    def _table_scan_source(self, table: str) -> str:
        table = validate_identifier(table, "table")
        if table == self.factor_name and self._validation_factor_scope is not None:
            begin, end = self._validation_factor_scope
            # Never fall back to the historical wildcard while a bounded
            # validation is active.  A missing target partition should fail
            # with a useful startup diagnostic instead of loading the full
            # factor history into the validator.
            return self.partition_scan_sql(table, begin=begin, end=end)
        if self._relation_exists(table):
            return self.identifier(table)
        glob = self.table_root(table).as_posix().rstrip("/") + "/**/*.parquet"
        return (
            f"parquet_scan({self.sql_literal(glob)}, "
            "HIVE_PARTITIONING=1, union_by_name=1)"
        )

    def _relation_exists(self, name: str) -> bool:
        relation = self.identifier(name)
        row = self.execute(
            """
            SELECT count(*)
            FROM information_schema.tables
            WHERE table_name = ?
            """,
            [relation],
        ).fetchone()
        return bool(row and row[0])
