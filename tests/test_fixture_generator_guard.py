"""Fixture-generator guard tests (issue #360, plan-critic R6).

The migration-fixture generator must refuse --input/--store paths inside
the tracked tests/fixtures/ tree without the explicit --allow-tracked
unlock, and permit them with it (the documented regeneration workflow).

Run: python tests/test_fixture_generator_guard.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GEN = REPO_ROOT / "tests" / "fixtures" / "entity" / \
    "generate_migration_expected.py"


class GeneratorGuardTest(unittest.TestCase):
    def _run(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(GEN), *extra],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
            timeout=120, env=dict(os.environ))

    def test_tracked_input_refused_without_flag(self):
        proc = self._run(
            "--input", "tests/fixtures/entity/migration-input.jsonl",
            "--out", os.devnull, "--phase", "seed")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--allow-tracked", proc.stderr)

    def test_tracked_store_refused_without_flag(self):
        proc = self._run(
            "--input", "nonexistent-scratch-input.jsonl",
            "--store", "tests/fixtures/entity/store.sqlite",
            "--out", os.devnull, "--phase", "seed")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--allow-tracked", proc.stderr)

    def test_tracked_out_refused_without_flag(self):
        # --out is a guarded path too (review round): dumping over a tracked
        # fixture without the unlock must refuse before any write.
        proc = self._run(
            "--input", "nonexistent-scratch-input.jsonl",
            "--out", "tests/fixtures/entity/migration-expected.json",
            "--phase", "dump")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--allow-tracked", proc.stderr)

    def test_tracked_paths_permitted_with_flag(self):
        # With the flag the guard itself passes AND the permitted
        # invocation must actually succeed end-to-end (an allowed run that
        # fails later is a regression, not a guard pass): the seeded store
        # is a scratch path so nothing lands in the tracked fixtures tree
        # (copilot round).
        scratch = Path(self.scratch_dir()) / "guard-store.sqlite"
        proc = self._run(
            "--input", "tests/fixtures/entity/migration-input.jsonl",
            "--store", str(scratch),
            "--out", os.devnull, "--phase", "seed",
            "--allow-tracked")
        self.assertNotIn("--allow-tracked", proc.stderr)
        self.assertEqual(
            proc.returncode, 0,
            f"the allowed invocation must succeed: {proc.stderr[-400:]}")

    @staticmethod
    def scratch_dir() -> str:
        import tempfile
        return tempfile.mkdtemp(prefix="zmem-guard-")


if __name__ == "__main__":
    _loader = unittest.TestLoader()
    _suite = _loader.loadTestsFromModule(sys.modules[__name__])
    _result = unittest.TextTestRunner(verbosity=2).run(_suite)
    print("CHECK-PASS" if _result.wasSuccessful() else "CHECK-FAIL")
    sys.exit(0 if _result.wasSuccessful() else 1)
