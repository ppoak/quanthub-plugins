"""DuckDB-backed upsert for local Hive Parquet tables."""

import shutil
import uuid
from pathlib import Path

import duckdb
import pandas as pd


class DuckTable:
    """Minimal Hive-partitioned Parquet table using DuckDB."""

    def __init__(self, root_path):
        self.root_path = Path(root_path)
        self.con = duckdb.connect(database=":memory:", read_only=False)
        self._view_name = f"t_{uuid.uuid4().hex[:8]}"

    def columns(self):
        """Return column names from the first parquet file, or empty list."""
        files = list(self.root_path.rglob("data_0.parquet"))
        if not files:
            return []
        return list(duckdb.read_parquet(str(files[0]), hive_partitioning=True).columns)

    def upsert(self, df: pd.DataFrame, keys: list, partition_by=None):
        """Upsert df into the partitioned Parquet directory."""
        if df.duplicated(subset=keys).any():
            raise ValueError("DataFrame contains duplicate rows based on keys.")
        if not list(self.root_path.rglob("*.parquet")):
            self._write_new(df, partition_by)
        else:
            self.refresh()
            self._upsert_existing(df, keys, partition_by)
        self.refresh()

    def refresh(self):
        """Re-register the parquet files as a DuckDB view."""
        files = list(self.root_path.rglob("data_0.parquet"))
        if not files:
            return
        paths = ", ".join("'" + str(f).replace("'", "''") + "'" for f in files)
        self.con.execute(
            f"CREATE OR REPLACE VIEW {self.view_name} AS "
            f"SELECT * FROM read_parquet([{paths}], hive_partitioning=true)"
        )

    @property
    def view_name(self):
        return self._view_name

    def _write_new(self, df, partition_by):
        self.root_path.mkdir(parents=True, exist_ok=True)
        tmpdir = self.root_path.parent / f".tmp_{uuid.uuid4().hex[:8]}"
        try:
            self._write_df_to_dir(df, str(tmpdir), partition_by)
            if self.root_path.exists():
                shutil.rmtree(self.root_path)
            shutil.move(str(tmpdir), str(self.root_path))
        finally:
            if tmpdir.exists():
                shutil.rmtree(tmpdir, ignore_errors=True)

    def _upsert_existing(self, df, keys, partition_by):
        base_cols = self.columns()
        all_cols = ", ".join(f'"{c}"' for c in base_cols)
        key_expr = ", ".join(f'"{k}"' for k in keys)
        tmpdir = self.root_path.parent / f".tmp_{uuid.uuid4().hex[:8]}"

        tmp_name = f"new_{uuid.uuid4().hex[:6]}"
        self.con.register(tmp_name, df)
        parts_tbl = None

        try:
            if not partition_by:
                tmpdir.mkdir(parents=True, exist_ok=True)
                out_path = tmpdir / "data_0.parquet"
                sql = f"""
                    COPY (
                        SELECT {all_cols} FROM (
                            SELECT *, ROW_NUMBER() OVER (PARTITION BY {key_expr} ORDER BY is_new DESC) AS rn
                            FROM (
                                SELECT {all_cols}, 0 AS is_new FROM {self.view_name}
                                UNION ALL
                                SELECT {all_cols}, 1 AS is_new FROM {tmp_name}
                            )
                        ) WHERE rn = 1
                    ) TO '{out_path}' (FORMAT 'parquet', COMPRESSION 'zstd')
                """
                self.con.execute(sql)
                dst = self.root_path / "data_0.parquet"
                if dst.exists():
                    dst.unlink()
                shutil.move(str(out_path), str(dst))
            else:
                # For partitioned tables: only rewrite affected partitions
                parts_tbl = f"parts_{uuid.uuid4().hex[:6]}"
                affected = df[partition_by].drop_duplicates()
                self.con.register(parts_tbl, affected)
                part_cols = ", ".join(f'"{c}"' for c in partition_by)

                sql = f"""
                    COPY (
                        SELECT {all_cols} FROM (
                            SELECT *, ROW_NUMBER() OVER (PARTITION BY {key_expr} ORDER BY is_new DESC) AS rn
                            FROM (
                                SELECT {all_cols}, 0 AS is_new
                                FROM {self.view_name} AS e
                                JOIN {parts_tbl} AS p USING ({part_cols})
                                UNION ALL
                                SELECT {all_cols}, 1 AS is_new FROM {tmp_name}
                            )
                        ) WHERE rn = 1
                    ) TO '{tmpdir}' (FORMAT 'parquet', COMPRESSION 'zstd', PARTITION_BY ({part_cols}))
                """
                self.con.execute(sql)

                for subdir in [d for d in tmpdir.iterdir() if d.is_dir()]:
                    dst = self.root_path / subdir.name
                    if dst.exists():
                        shutil.rmtree(dst)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(subdir), str(dst))
        finally:
            self.con.unregister(tmp_name)
            if parts_tbl:
                self.con.unregister(parts_tbl)
            if tmpdir.exists():
                shutil.rmtree(tmpdir, ignore_errors=True)

    def _write_df_to_dir(self, df, target, partition_by):
        Path(target).mkdir(parents=True, exist_ok=True)
        reg = f"src_{uuid.uuid4().hex[:8]}"
        self.con.register(reg, df)
        opts = ["FORMAT 'parquet'", "COMPRESSION 'zstd'"]
        if partition_by:
            cols = ", ".join(f'"{c}"' for c in partition_by)
            opts.append(f"PARTITION_BY ({cols})")
            sql = f"COPY (SELECT * FROM {reg}) TO '{target}' ({', '.join(opts)})"
        else:
            sql = f"COPY (SELECT * FROM {reg}) TO '{target}/data_0.parquet' ({', '.join(opts)})"
        self.con.execute(sql)
        self.con.unregister(reg)
