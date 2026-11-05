"""Demonstrate frozen history summaries and path, time and content changes."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from factor_protocol import FactorStorage, resolve_factor_package
from factor_protocol.artifacts import (
    dataset_manifest,
    manifest_reference,
    manifests_match,
    read_config,
)
from factor_protocol.fingerprint import factor_definition_fingerprint
from factor_protocol.identity import (
    identity_manifest_matches,
    read_identity,
    require_identity,
    stored_identity_matches,
    write_identity,
)


class IdentityExamples(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        entry = self.root / "factor_zoo/sample_factor/scripts/main.py"
        entry.parent.mkdir(parents=True)
        entry.write_text("FACTOR_COLUMNS = ('signal',)\n", encoding="utf-8")
        self.package = resolve_factor_package("sample_factor", project_root=self.root)
        self.fingerprint = factor_definition_fingerprint(self.package)
        self.data = self.root / "data"
        self.storage = FactorStorage("sample_factor", data_root=self.data, artifacts_root=self.root / "artifacts", threads=1)
        self.addCleanup(self.storage.close)
        self.storage.write_factors(
            pd.DataFrame(
                {
                    "date": ["2026-09-24", "2026-09-25", "2026-09-28"],
                    "code": ["AAA"] * 3,
                    "signal": [1.0, 2.0, 3.0],
                }
            )
        )
        self.write_identity()

    def write_identity(self):
        write_identity(
            self.storage,
            package=self.package,
            factor_columns=("signal",),
            fingerprint=self.fingerprint,
        )

    def test_identity_stores_one_compact_history_summary(self):
        identity = read_config(self.storage.factor_root / "identity.json")
        self.assertEqual(
            set(identity), {"factor_name", "columns", "fingerprint", "manifest"}
        )
        self.assertEqual(identity["columns"], ["signal"])
        summary = identity["manifest"]
        self.assertEqual(set(summary), {"begin", "end", "sha256"})
        self.assertEqual(summary["begin"], "2026-09-24")
        self.assertEqual(summary["end"], "2026-09-28")
        self.assertEqual(len(summary["sha256"]), 64)
        self.assertTrue(
            stored_identity_matches(self.storage, fingerprint=self.fingerprint)
        )

    def test_rewriting_an_old_partition_invalidates_the_identity(self):
        self.storage.write_factors(
            pd.DataFrame(
                {
                    "date": ["2026-09-24"],
                    "code": ["AAA"],
                    "signal": [999.0],
                }
            )
        )
        self.assertFalse(identity_manifest_matches(self.storage.factor_root))
        with self.assertRaisesRegex(ValueError, "manifest changed"):
            require_identity(self.storage.factor_root)

    def test_latest_partition_rewrite_invalidates_the_identity(self):
        self.storage.write_factors(
            pd.DataFrame(
                {"date": ["2026-09-28"], "code": ["AAA"], "signal": [999.0]}
            )
        )
        self.assertFalse(identity_manifest_matches(self.storage.factor_root))
        with self.assertRaisesRegex(ValueError, "manifest changed"):
            require_identity(self.storage.factor_root)

    def test_changed_content_with_preserved_metadata_is_rejected(self):
        for day in ("2026-09-24", "2026-09-28"):
            with self.subTest(day=day):
                path = next((self.storage.factor_root / f"date={day}").glob("*.parquet"))
                stat = path.stat()
                original = path.read_bytes()
                before = manifest_reference(
                    dataset_manifest(self.storage.factor_data_root, "sample_factor", through=day)
                )
                changed = bytearray(original)
                changed[len(changed) // 2] ^= 1
                try:
                    path.write_bytes(changed)
                    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                    self.assertEqual(path.stat().st_size, stat.st_size)
                    self.assertEqual(path.stat().st_mtime_ns, stat.st_mtime_ns)
                    self.assertFalse(identity_manifest_matches(self.storage.factor_root))
                    with self.assertRaisesRegex(ValueError, "manifest changed"):
                        require_identity(self.storage.factor_root)
                    current = dataset_manifest(self.storage.factor_data_root, "sample_factor", through=day)
                    self.assertFalse(manifests_match(before, current))
                finally:
                    path.write_bytes(original)
                    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    def test_modification_time_alone_invalidates_the_history(self):
        for day in ("2026-09-24", "2026-09-28"):
            with self.subTest(day=day):
                path = next((self.storage.factor_root / f"date={day}").glob("*.parquet"))
                stat = path.stat()
                original = path.read_bytes()
                before = manifest_reference(
                    dataset_manifest(self.storage.factor_data_root, "sample_factor", through="2026-09-28")
                )
                try:
                    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
                    self.assertEqual(path.read_bytes(), original)
                    self.assertFalse(identity_manifest_matches(self.storage.factor_root))
                    with self.assertRaisesRegex(ValueError, "manifest changed"):
                        require_identity(self.storage.factor_root)
                    current = dataset_manifest(self.storage.factor_data_root, "sample_factor", through="2026-09-28")
                    self.assertFalse(manifests_match(before, current))
                finally:
                    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    def test_relative_path_change_invalidates_the_history(self):
        path = next((self.storage.factor_root / "date=2026-09-25").glob("*.parquet"))
        stat = path.stat()
        original = path.read_bytes()
        before = manifest_reference(
            dataset_manifest(self.storage.factor_data_root, "sample_factor", through="2026-09-28")
        )
        renamed = path.rename(path.with_name("renamed.parquet"))
        self.assertEqual(renamed.read_bytes(), original)
        self.assertEqual(renamed.stat().st_mtime_ns, stat.st_mtime_ns)
        self.assertFalse(identity_manifest_matches(self.storage.factor_root))
        with self.assertRaisesRegex(ValueError, "manifest changed"):
            require_identity(self.storage.factor_root)
        current = dataset_manifest(self.storage.factor_data_root, "sample_factor", through="2026-09-28")
        self.assertFalse(manifests_match(before, current))

    def test_deleted_historical_partition_invalidates_the_identity(self):
        shutil.rmtree(self.storage.factor_root / "date=2026-09-25")
        self.assertFalse(identity_manifest_matches(self.storage.factor_root))

    def test_tail_append_preserves_the_previous_prefix_snapshot(self):
        identity = read_identity(self.storage.factor_root)
        before = manifest_reference(
            dataset_manifest(self.storage.factor_data_root, "sample_factor", through="2026-09-28")
        )
        self.storage.write_factors(
            pd.DataFrame(
                {
                    "date": ["2026-09-29"],
                    "code": ["AAA"],
                    "signal": [4.0],
                }
            )
        )
        self.assertFalse(identity_manifest_matches(self.storage.factor_root))
        current = dataset_manifest(self.storage.factor_data_root, "sample_factor", through="2026-09-28")
        self.assertTrue(manifests_match(before, current))
        self.write_identity()
        summary = read_identity(self.storage.factor_root)["manifest"]
        self.assertEqual(summary["end"], "2026-09-29")
        self.assertNotEqual(summary["sha256"], identity["manifest"]["sha256"])
        self.assertTrue(identity_manifest_matches(self.storage.factor_root))

    def test_relocation_requires_preserved_file_times_and_contents(self):
        moved = self.root / "relocated/sample_factor"
        shutil.copytree(self.storage.factor_root, moved)
        self.assertTrue(identity_manifest_matches(moved))
        require_identity(moved, fingerprint=self.fingerprint, columns=("signal",))
        path = next(moved.rglob("*.parquet"))
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        self.assertFalse(identity_manifest_matches(moved))


    def test_missing_columns_and_changed_code_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "requested columns"):
            require_identity(self.storage.factor_root, columns=("missing",))
        with self.assertRaisesRegex(ValueError, "calculation definition"):
            require_identity(self.storage.factor_root, fingerprint="0" * 64)
        scripts = self.package.module_path.parent
        self.package.module_path.write_text(
            "from .calculation import calculate_factor\n"
            "from .validation import validate_factor\nFACTOR_COLUMNS = ('signal',)\n",
            encoding="utf-8",
        )
        (scripts / "calculation.py").write_text(
            "from . import constants\ndef calculate_factor():\n    return constants.VALUE\n",
            encoding="utf-8",
        )
        (scripts / "constants.py").write_text("VALUE = 1\n", encoding="utf-8")
        fingerprint = factor_definition_fingerprint(self.package)
        (scripts / "validation.py").write_text("raise RuntimeError('independent')\n", encoding="utf-8")
        (scripts / "development.py").write_text("VALUE = 2\n", encoding="utf-8")
        self.assertEqual(fingerprint, factor_definition_fingerprint(self.package))
        (scripts / "constants.py").write_text("VALUE = 2\n", encoding="utf-8")
        self.assertNotEqual(fingerprint, factor_definition_fingerprint(self.package))
        with self.assertRaisesRegex(ValueError, "calculation definition"):
            require_identity(self.storage.factor_root,
                             fingerprint=factor_definition_fingerprint(self.package))



if __name__ == "__main__":
    unittest.main()
