"""Leak-proof gate for tests/test_classifier_parity.py's temp-database hygiene.

The parity suite can stay green while leaking temp files: its assertions
never inspect post-teardown temp state. This driver closes that hole (PR
#441 closeout item NEW-3a) with two legs:

- A subprocess leg runs the parity suite verbosely under an isolated
  TMP/TEMP/TMPDIR and a SEPARATE scratch ZMEM_DATA. It first proves the
  fixture-creating DbWiringIntegrationTest legs actually ran ok (positive
  control: a skipped or renamed class must not pass vacuously), then
  asserts both scratch directories are empty after the child exits.
- An in-process leg pins the registered cleanup itself under a scoped
  ``tempfile.tempdir``, so a regression never litters the real TEMP. The
  fixture runs in sqlite's default DELETE journal mode, whose only sibling
  is a transient ``-journal`` (gone at commit) and which never creates
  ``-wal``/``-shm``; the sibling-removal branch is therefore pinned by
  planting those names directly, and the cleanup must report success.

Run: python tests/test_classifier_parity_hygiene.py
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
PARITY = REPO_ROOT / "tests" / "test_classifier_parity.py"
CHILD_TIMEOUT_S = 300
SIBLINGS = ("-journal", "-wal", "-shm")

# Verbose unittest reports "test_x (__main__.DbWiringIntegrationTest.test_x)
# ... <result>", but unrelated stderr noise (e.g. DeprecationWarnings while a
# cold __pycache__ compiles storelib) can land between the marker and the
# result, so each leg's result is read as the last line of its own segment.
_DB_LEG_START = re.compile(
    r"\([\w.]*DbWiringIntegrationTest\.(test_\w+)\) \.\.\. ")
_NEXT_TEST_LINE = re.compile(r"^test_\w+ \(", re.M)
_SKIPPED_SUMMARY = re.compile(r"^OK \(.*skipped=", re.M)


def _passed_db_legs(out):
    starts = list(_DB_LEG_START.finditer(out))
    passed = set()
    for i, m in enumerate(starts):
        # A segment ends where the NEXT leg's line begins (the marker match
        # starts mid-line, after the "test_x " prefix).
        end = (out.rfind("\n", 0, starts[i + 1].start()) + 1
               if i + 1 < len(starts) else len(out))
        seg = out[m.end():end]
        nxt = _NEXT_TEST_LINE.search(seg)
        if nxt:
            seg = seg[:nxt.start()]
        sep = seg.find("\n------")
        if sep != -1:
            seg = seg[:sep]
        lines = [line.strip() for line in seg.splitlines() if line.strip()]
        if lines and lines[-1] == "ok":
            passed.add(m.group(1))
    return sorted(passed)


def _load_parity():
    spec = importlib.util.spec_from_file_location("parity_mod", PARITY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _db_wiring_test_names(mod):
    return sorted(
        unittest.TestLoader().getTestCaseNames(mod.DbWiringIntegrationTest))


class ParityTempHygieneTest(unittest.TestCase):
    def test_parity_suite_leaves_no_temp_artifacts(self):
        expected = _db_wiring_test_names(_load_parity())
        self.assertTrue(expected, "DbWiringIntegrationTest has no tests")
        tmp_root = tempfile.mkdtemp(prefix="zmem-parity-hygiene-tmp-")
        data_dir = tempfile.mkdtemp(prefix="zmem-parity-hygiene-data-")
        self.addCleanup(shutil.rmtree, tmp_root, ignore_errors=True)
        self.addCleanup(shutil.rmtree, data_dir, ignore_errors=True)
        env = {k: v for k, v in os.environ.items()
               if not k.upper().startswith(("ZMEM_", "CLAUDE_", "ZCODE_"))}
        env.update({
            "TMP": tmp_root, "TEMP": tmp_root, "TMPDIR": tmp_root,
            "ZMEM_DATA": data_dir,
            "ZMEM_STORE": os.path.join(data_dir, "store.sqlite"),
            "PYTHONUTF8": "1",
        })
        try:
            r = subprocess.run(
                [sys.executable, "-X", "utf8", str(PARITY), "-v"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", env=env, timeout=CHILD_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            err = exc.stderr or ""
            if isinstance(err, bytes):
                err = err.decode("utf-8", "replace")
            raise AssertionError(
                "parity child exceeded the %ds budget; stderr tail: %s"
                % (CHILD_TIMEOUT_S, err[-400:])) from None
        out = r.stderr or ""
        self.assertEqual(
            r.returncode, 0,
            "parity child failed before the leak check: %s" % out[-400:])
        # Positive control: the legs that create the temp fixtures ran ok.
        self.assertEqual(
            _passed_db_legs(out), expected,
            "the fixture-creating DbWiringIntegrationTest legs did not all "
            "run ok, so the leak check below would pass vacuously: %s"
            % out[-400:])
        self.assertIsNone(
            _SKIPPED_SUMMARY.search(out),
            "the parity child skipped tests: %s" % out[-400:])
        self.assertEqual(
            sorted(p.name for p in Path(tmp_root).iterdir()), [],
            "the parity suite leaked temp artifacts into its TMP dir")
        self.assertEqual(
            sorted(p.name for p in Path(data_dir).iterdir()), [],
            "the parity suite wrote into its scratch ZMEM_DATA dir")

    def test_cleanup_removes_sqlite_siblings(self):
        scoped = tempfile.mkdtemp(prefix="zmem-parity-hygiene-inproc-")
        self.addCleanup(shutil.rmtree, scoped, ignore_errors=True)
        mod = _load_parity()
        case = mod.DbWiringIntegrationTest(_db_wiring_test_names(mod)[0])
        with mock.patch.object(tempfile, "tempdir", scoped):
            path = case._make_db(with_part_table=True)
        self.assertEqual(
            os.path.dirname(path), scoped,
            "_make_db did not honor the scoped temp dir")
        self.assertTrue(os.path.exists(path))
        for s in SIBLINGS:
            open(path + s, "wb").close()
        self.assertTrue(case.doCleanups(), "the registered cleanup raised")
        for s in ("",) + SIBLINGS:
            self.assertFalse(
                os.path.exists(path + s),
                "cleanup left %s behind" % (path + s))
        self.assertEqual(
            os.listdir(scoped), [],
            "the in-process leg left artifacts in its scoped temp dir")


if __name__ == "__main__":
    unittest.main()
