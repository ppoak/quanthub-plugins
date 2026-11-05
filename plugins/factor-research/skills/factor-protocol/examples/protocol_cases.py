"""Runnable examples that also verify the public Factor Protocol contract.

Run from the repository root with:
    .venv/Scripts/python plugins/factor-research/skills/factor-protocol/examples/protocol_cases.py
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import pandas as pd

import factor_protocol
from factor_protocol import FactorPath, FactorStorage, define_factor_validator
from factor_protocol.artifacts import resolve_output_root
from factor_protocol.config import resolve_data_root, resolve_factor_data_root, resolve_factor_schema_path
from factor_protocol.fingerprint import factor_definition_fingerprint
from factor_protocol.loader import resolve_factor_package
from factor_protocol.registry import load_active_factor_packages


class FactorProtocolExamples(unittest.TestCase):
    def test_explicit_factor_directory_preserves_other_table_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scores = root / "research/run_001/signals"
            partition = scores / "date=2026-01-05"
            partition.mkdir(parents=True)
            pd.DataFrame({"code": ["000001.SZ"], "score": [0.25]}).to_parquet(
                partition / "signal.parquet", index=False
            )
            with FactorStorage("composite", project_root=root, data_root="data",
                               artifacts_root="artifacts", factor_root="research/run_001/signals",
                               threads=1) as storage:
                self.assertEqual(storage.paths.factor_root, scores)
                self.assertEqual(storage.table_root("composite"), scores)
                self.assertEqual(storage.table_root("quotes_day"), root / "data/quotes_day")
                self.assertEqual(storage.factor_date_range(), ("2026-01-05", "2026-01-05"))
                values = storage.df(storage.select_sql("composite", columns=("code", "score")))
                self.assertEqual(values.to_dict("records"), [{"code": "000001.SZ", "score": 0.25}])
            self.assertFalse((root / "artifacts/factor_data/composite").exists())

    def test_factor_storage_is_the_only_public_storage_type(self) -> None:
        self.assertIs(factor_protocol.FactorStorage, FactorStorage)
        self.assertFalse(hasattr(factor_protocol, "DataStorage"))
        self.assertFalse(hasattr(FactorStorage, "factor_query"))
        self.assertFalse(hasattr(FactorStorage, "factor_path_panel_query"))
        self.assertFalse(hasattr(FactorStorage, "forward_return_query"))

    def test_schema_filename_defaults_to_schema_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project_root = Path(temporary)
            with patch.dict("os.environ", {"FACTORZOO": "", "FACTORZOO_SCHEMA": ""}):
                schema_path = resolve_factor_schema_path(project_root=project_root)

        self.assertEqual(schema_path, project_root / "factor_zoo" / "schema.yaml")

    def test_configured_schema_file_loads_without_version_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project_root = Path(temporary)
            factor_root = project_root / "research" / "factors"
            factor_root.mkdir(parents=True)
            (project_root / ".env").write_text(
                "FACTORZOO=research/factors\nFACTORZOO_SCHEMA=registry.yml\n",
                encoding="utf-8",
            )
            (factor_root / "registry.yml").write_text(
                "active:\n"
                "  - package: sample_factor\n"
                f'    fingerprint: "{"0" * 64}"\n'
                "    columns: [signal]\n",
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {"FACTORZOO": "", "FACTORZOO_SCHEMA": ""},
            ):
                packages = load_active_factor_packages(
                    project_root=project_root,
                )

        self.assertEqual(len(packages), 1)
        self.assertEqual(packages[0].package, "sample_factor")
        self.assertEqual(packages[0].columns, ("signal",))

    def test_explicit_default_named_paths_override_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".env").write_text(
                "DATA=shared-data\nFACTORZOO=research-factors\n", encoding="utf-8"
            )
            with patch.dict("os.environ", {"DATA": "", "FACTORZOO": ""}):
                self.assertEqual(
                    resolve_data_root(project_root=root), root / "shared-data"
                )
                self.assertEqual(
                    resolve_data_root(project_root=root, data_root="data"),
                    root / "data",
                )
                package = resolve_factor_package(
                    "sample", project_root=root, factor_zoo_root="factor_zoo"
                )
                self.assertEqual(package.factor_root, root / "factor_zoo" / "sample")
                self.assertEqual(resolve_factor_data_root(project_root=root, artifacts_root="artifacts"), root / "artifacts/factor_data")
                with FactorStorage("sample", project_root=root) as storage:
                    self.assertEqual(storage.data_root, root / "shared-data")
            with patch.dict("os.environ", {"DATA": "process-data"}):
                self.assertEqual(
                    resolve_data_root(project_root=root), root / "process-data"
                )
                self.assertEqual(
                    resolve_data_root(project_root=root, data_root="data"),
                    root / "data",
                )

    def test_factor_path_parses_table_and_column(self) -> None:
        self.assertEqual(
            FactorPath.parse("quotes_day.close_post"),
            FactorPath("quotes_day", "close_post"),
        )
        self.assertEqual(
            FactorPath.parse(("momentum", "mom_20")).label, "momentum.mom_20"
        )

    def test_partition_date_batches_count_available_trading_dates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project_root = Path(temporary)
            data_root = project_root / "data"
            for trading_date in ("2025-01-02", "2025-01-03", "2025-01-06"):
                partition = data_root / "quotes_day" / f"date={trading_date}"
                partition.mkdir(parents=True)
                (partition / "part.parquet").write_bytes(b"partition")
            storage = FactorStorage(
                "sample_factor",
                data_root=data_root,
                project_root=project_root,
                threads=1,
            )
            try:
                batches = storage.partition_date_batches(
                    "quotes_day",
                    begin="2025-01-02",
                    end="2025-01-06",
                    batch_days=2,
                )
            finally:
                storage.close()

        self.assertEqual(
            batches,
            (("2025-01-02", "2025-01-03"), ("2025-01-06", "2025-01-06")),
        )

    def test_project_dotenv_controls_factor_and_artifact_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project_root = Path(temporary)
            (project_root / ".env").write_text(
                'FACTORZOO="research/factors" # source packages\n'
                "FACTORZOO_SCHEMA=registry.yaml\n"
                "DATA=market-data\n"
                "ARTIFACTS=run-artifacts\n",
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {
                    "FACTORZOO": "",
                    "FACTORZOO_SCHEMA": "",
                    "DATA": "",
                    "ARTIFACTS": "",
                },
            ):
                package = resolve_factor_package(
                    "sample_factor",
                    project_root=project_root,
                )
                data_root = resolve_data_root(project_root=project_root)
                schema_path = resolve_factor_schema_path(project_root=project_root)
                output_root = resolve_output_root(
                    "artifacts/factor_zoo",
                    project_root=project_root,
                )

            self.assertEqual(
                package.factor_root,
                project_root / "research" / "factors" / "sample_factor",
            )
            self.assertEqual(data_root, project_root / "market-data")
            self.assertEqual(
                schema_path,
                project_root / "research" / "factors" / "registry.yaml",
            )
            self.assertEqual(
                output_root,
                project_root / "run-artifacts" / "factor_zoo",
            )
            with patch.dict("os.environ", {"DATA": "", "ARTIFACTS": ""}):
                storage = FactorStorage(
                    "sample_factor",
                    project_root=project_root,
                    threads=1,
                )
            try:
                self.assertEqual(storage.data_root, project_root / "market-data")
                self.assertEqual(storage.factor_root, project_root / "run-artifacts/factor_data/sample_factor")
            finally:
                storage.close()

    def test_process_environment_overrides_dotenv_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project_root = Path(temporary)
            (project_root / ".env").write_text(
                "FACTORZOO=from-file\n"
                "FACTORZOO_SCHEMA=registry-file.yaml\n"
                "ARTIFACTS=artifacts-from-file\n",
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {
                    "FACTORZOO": "from-environment",
                    "FACTORZOO_SCHEMA": "registry-environment.yaml",
                    "DATA": "data-from-environment",
                    "ARTIFACTS": "output",
                },
            ):
                package = resolve_factor_package(
                    "sample_factor",
                    project_root=project_root,
                )
                output_root = resolve_output_root(
                    "artifacts/factor_zoo",
                    project_root=project_root,
                )
                data_root = resolve_data_root(project_root=project_root)
                schema_path = resolve_factor_schema_path(project_root=project_root)

            self.assertEqual(
                package.factor_root,
                project_root / "from-environment" / "sample_factor",
            )
            self.assertEqual(data_root, project_root / "data-from-environment")
            self.assertEqual(
                schema_path,
                project_root / "from-environment" / "registry-environment.yaml",
            )
            self.assertEqual(output_root, project_root / "output" / "factor_zoo")

    def test_custom_factor_root_loads_relative_package_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project_root = Path(temporary)
            package = resolve_factor_package(
                "sample_factor",
                project_root=project_root,
                factor_zoo_root="signals",
            )
            package.module_path.parent.mkdir(parents=True)
            (package.module_path.parent / "helper.py").write_text(
                "VALUE = 17\n",
                encoding="utf-8",
            )
            package.module_path.write_text(
                "from .helper import VALUE\n",
                encoding="utf-8",
            )

            module = factor_protocol.load_module_from_path(
                package.module_path,
                project_root=project_root,
                factor_name=package.factor_name,
            )

            self.assertEqual(module.VALUE, 17)

    def test_definition_fingerprint_is_independent_of_absolute_root(self) -> None:
        fingerprints = []
        for index in range(2):
            with tempfile.TemporaryDirectory() as temporary:
                project_root = Path(temporary) / f"project_{index}"
                project_root.mkdir()
                (project_root / ".env").write_text(
                    "FACTORZOO=stored-factors\n",
                    encoding="utf-8",
                )
                package = resolve_factor_package(
                    "sample_factor",
                    project_root=project_root,
                )
                package.module_path.parent.mkdir(parents=True)
                package.module_path.write_bytes(
                    b"FACTOR_NAME = 'sample_factor'\n"
                    b"FACTOR_COLUMNS = ('signal',)\n"
                    b"def calculate_factor(*, context):\n    return None\n"
                )
                fingerprints.append(factor_definition_fingerprint(package))

        self.assertEqual(fingerprints[0], fingerprints[1])

    def test_validator_definition_only_captures_factor_owned_callbacks(self) -> None:
        called = False

        def compute_audit(source, cfg):
            nonlocal called
            called = True
            return source

        definition = define_factor_validator(
            compute_audit,
            factor_name="sample_factor",
            factor_columns=("signal",),
            validation_input_columns=("code", "date", "close_post"),
        )

        self.assertEqual(definition.factor_name, "sample_factor")
        self.assertEqual(definition.factor_columns, ("signal",))
        self.assertEqual(
            definition.validation_input_columns,
            ("code", "date", "close_post"),
        )
        self.assertIs(definition.compute_validation_audit, compute_audit)
        self.assertIsNone(definition.compute_validation_audits)
        self.assertFalse(called)

    def test_definition_fingerprint_hashes_current_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = resolve_factor_package("sample_factor", project_root=temporary)
            package.module_path.parent.mkdir(parents=True)
            source = (
                b"from factor_protocol import define_factor_validator\n"
                b"def calculate_factor(*, context):\n    return None\n"
            )
            package.module_path.write_bytes(source)
            expected = hashlib.sha256(b"scripts/main.py\0" + source + b"\0")
            self.assertEqual(
                factor_definition_fingerprint(package), expected.hexdigest()
            )

    def test_definition_fingerprint_normalizes_line_endings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = resolve_factor_package("sample_factor", project_root=temporary)
            package.module_path.parent.mkdir(parents=True)
            source = b"def calculate_factor(*, context):\n    return None\n"
            fingerprints = []
            for newline in (b"\n", b"\r\n", b"\r"):
                package.module_path.write_bytes(source.replace(b"\n", newline))
                fingerprints.append(factor_definition_fingerprint(package))
            self.assertEqual(len(set(fingerprints)), 1)

    def test_definition_fingerprint_tracks_calculation_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = resolve_factor_package("sample_factor", project_root=temporary)
            package.module_path.parent.mkdir(parents=True)
            package.module_path.write_text("FORMULA = 1\n", encoding="utf-8")
            before = factor_definition_fingerprint(package)
            validation = package.module_path.with_name("validation.py")
            validation.write_text("TOLERANCE = 0.1\n", encoding="utf-8")
            self.assertEqual(factor_definition_fingerprint(package), before)
            validation.write_text("TOLERANCE = 0.01\n", encoding="utf-8")
            self.assertEqual(factor_definition_fingerprint(package), before)
            package.module_path.write_text("FORMULA = 2\n", encoding="utf-8")
            self.assertNotEqual(factor_definition_fingerprint(package), before)

    def test_storage_round_trip_preserves_hive_date_partition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            storage = FactorStorage("sample_factor", data_root=root, artifacts_root=root / "artifacts", threads=1)
            try:
                storage.write_factors(
                    pd.DataFrame(
                        {
                            "code": ["000001.SZ", "000002.SZ"],
                            "date": ["2026-01-05", "2026-01-05"],
                            "signal": [0.25, -0.5],
                        }
                    )
                )
                frame = storage.df(
                    storage.select_sql(
                        "sample_factor",
                        columns=("code", "date", "signal"),
                        where=(
                            "CAST(date AS DATE) = "
                            f"CAST({storage.sql_literal('2026-01-05')} AS DATE)"
                        ),
                        order_by=("date", "code"),
                    )
                )
                self.assertEqual(storage.table_root("sample_factor"), storage.factor_root)
                self.assertEqual(storage.table_root("quotes_day"), root / "quotes_day")
                self.assertEqual(len(frame), 2)
                self.assertEqual(set(frame["code"]), {"000001.SZ", "000002.SZ"})
                self.assertEqual(
                    (root / "artifacts" / "factor_data" / "sample_factor" / "date=2026-01-05").is_dir(), True
                )
                values = sorted(frame["signal"].tolist())
                self.assertEqual(values, [-0.5, 0.25])
            finally:
                storage.close()

    @staticmethod
    def _write_partitioned_quotes(storage: FactorStorage, root: Path) -> None:
        storage.register_dataframe(
            "_quotes_fixture",
            pd.DataFrame(
                {
                    "code": ["000001.SZ", "000001.SZ"],
                    "date": ["2026-01-05", "2026-01-06"],
                    "close": [10.0, 11.0],
                }
            ),
        )
        try:
            storage.copy_query_to_parquet(
                "SELECT code, CAST(date AS DATE) AS date, close FROM _quotes_fixture",
                root / "quotes_day",
                partition_by=("date",),
            )
        finally:
            storage.unregister("_quotes_fixture")

    def test_partition_scan_reads_only_requested_hive_dates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with FactorStorage("sample_factor", data_root=root, artifacts_root=root / "artifacts", threads=1) as storage:
                self._write_partitioned_quotes(storage, root)
                source = storage.partition_scan_sql(
                    "quotes_day", begin="2026-01-06", end="2026-01-06"
                )
                dates = storage.df(
                    f"SELECT DISTINCT date::VARCHAR AS date FROM {source} ORDER BY date"
                )["date"].tolist()
                self.assertEqual(dates, ["2026-01-06"])
                self.assertEqual(
                    storage.partition_dates(
                        "quotes_day", begin="2026-01-06", end="2026-01-06"
                    ),
                    ("2026-01-06",),
                )

    def test_partition_dates_ignores_empty_and_malformed_hive_partitions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with FactorStorage("sample_factor", data_root=root, artifacts_root=root / "artifacts", threads=1) as storage:
                self._write_partitioned_quotes(storage, root)
                (root / "quotes_day" / "date=2026-01-07").mkdir()
                (root / "quotes_day" / "date=invalid").mkdir()
                self.assertEqual(
                    storage.partition_dates("quotes_day"),
                    ("2026-01-05", "2026-01-06"),
                )

    def test_factor_overwrite_replaces_one_partition_and_preserves_others(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with FactorStorage("sample_factor", data_root=root, artifacts_root=root / "artifacts", threads=1) as storage:
                storage.write_factors(
                    pd.DataFrame(
                        {
                            "code": ["000001.SZ", "000002.SZ", "000001.SZ"],
                            "date": [
                                "2026-01-05",
                                "2026-01-05",
                                "2026-01-06",
                            ],
                            "signal": [0.1, 0.2, 0.3],
                        }
                    )
                )
                storage.write_factors(
                    pd.DataFrame(
                        {
                            "code": ["000001.SZ"],
                            "date": ["2026-01-05"],
                            "signal": [0.9],
                        }
                    )
                )
                replaced = storage.df(
                    storage.select_sql(
                        "sample_factor",
                        columns=("code", "date", "signal"),
                        where=(
                            "CAST(date AS DATE) = "
                            f"CAST({storage.sql_literal('2026-01-05')} AS DATE)"
                        ),
                        order_by=("date", "code"),
                    )
                )
                preserved = storage.df(
                    storage.select_sql(
                        "sample_factor",
                        columns=("code", "date", "signal"),
                        where=(
                            "CAST(date AS DATE) = "
                            f"CAST({storage.sql_literal('2026-01-06')} AS DATE)"
                        ),
                        order_by=("date", "code"),
                    )
                )
                self.assertEqual(replaced["code"].tolist(), ["000001.SZ"])
                self.assertEqual(replaced["signal"].tolist(), [0.9])
                self.assertEqual(preserved["signal"].tolist(), [0.3])

    def test_storage_sql_helpers_bind_parameters_and_reject_unsafe_identifiers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with FactorStorage(
                "sample_factor", data_root=temporary, artifacts_root=Path(temporary) / "artifacts", threads=1
            ) as storage:
                result = storage.df("SELECT ?::INTEGER AS value", [7])
                self.assertEqual(result.loc[0, "value"], 7)

                storage.register_dataframe(
                    "sample_rows",
                    pd.DataFrame(
                        {
                            "category": ["A", "B", "B"],
                            "value": [1, 2, 3],
                        }
                    ),
                )
                query = storage.select_sql(
                    "sample_rows",
                    columns=("category", "value"),
                    where=("value >= 2", "category = 'B'"),
                    order_by=("value",),
                )
                selected = storage.df(query)
                self.assertEqual(selected["value"].tolist(), [2, 3])

                with self.assertRaisesRegex(ValueError, "invalid SQL identifier"):
                    storage.create_view("unsafe; DROP TABLE sample", "SELECT 1")

    def test_duplicate_factor_keys_are_rejected_without_replacing_partition(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with FactorStorage("sample_factor", data_root=root, artifacts_root=root / "artifacts", threads=1) as storage:
                storage.write_factors(
                    pd.DataFrame(
                        {
                            "code": ["000001.SZ"],
                            "date": ["2026-01-05"],
                            "signal": [0.25],
                        }
                    )
                )
                with self.assertRaisesRegex(
                    ValueError, r"duplicate \(code, date\) keys"
                ):
                    storage.write_factors(
                        pd.DataFrame(
                            {
                                "code": ["000001.SZ", "000001.SZ"],
                                "date": ["2026-01-05", "2026-01-05"],
                                "signal": [0.5, 0.75],
                            }
                        )
                    )
                frame = storage.df(
                    storage.select_sql(
                        "sample_factor",
                        columns=("code", "date", "signal"),
                        where=(
                            "CAST(date AS DATE) = "
                            f"CAST({storage.sql_literal('2026-01-05')} AS DATE)"
                        ),
                        order_by=("date", "code"),
                    )
                )
                self.assertEqual(frame["signal"].tolist(), [0.25])


if __name__ == "__main__":
    unittest.main()
