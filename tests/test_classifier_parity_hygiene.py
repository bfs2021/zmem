"""Leak-proof gate for tests/test_classifier_parity.py's temp-database hygiene.

The parity suite can stay green while leaking temp files: its assertions
never inspect post-teardown temp state. This driver closes that hole (PR
#441 closeout item NEW-3a) with four legs:

- A subprocess leg runs the parity suite verbosely under an isolated
  TMP/TEMP/TMPDIR and a SEPARATE scratch ZMEM_DATA. It first proves every
  fixture-creating DbWiringIntegrationTest leg actually reported ok
  (positive control: a skipped or renamed DB leg must not pass vacuously),
  then asserts both scratch directories are empty after the child exits.
- An in-process leg runs the registered cleanup under a scoped
  ``tempfile.tempdir``, so its fixtures land in a dedicated temp dir rather
  than the shared TEMP root. The fixture runs in sqlite's default DELETE
  journal mode, whose only sibling is a transient ``-journal`` (gone at
  commit) and which never creates ``-wal``/``-shm``; the sibling-removal
  branch is therefore pinned by planting those names directly, and the
  cleanup must complete without error.
- A third leg pins that the cleanup tolerates ONLY a missing file: any
  other removal failure must surface instead of being swallowed.
- A fourth leg (owner-approved F-04b fold-in) forces a real mid-build
  fixture failure and requires the scoped dir to end empty, pinning that
  the registered connection close fires before the file removal.

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
DB_CLASS = "DbWiringIntegrationTest"

# Verbose unittest opens each leg with "test_x (module.Class.test_x)" and
# ends it with a result line ("... ok", or a bare "ok" when noise such as a
# cold-cache DeprecationWarning interleaved). The id line carries " ... " on
# the same line unless a method docstring adds a description line, so a
# leg's segment runs from its id line to the next id line (or the summary
# separator) and it passes only when that segment's last line reports ok and
# no earlier line in it carries a failing result token.
_TEST_ID = re.compile(r"^(test_\w+) \(([\w.]+)\)")
_BAD_RESULT = re.compile(r"(^|\.\.\. )(FAIL|ERROR|skipped\b|expected failure)")


def _passed_db_legs(out):
    lines = out.splitlines()
    starts = []
    for i, line in enumerate(lines):
        m = _TEST_ID.match(line)
        if m:
            starts.append((i, m))
    passed = set()
    for n, (i, m) in enumerate(starts):
        qual = m.group(2).split(".")
        if len(qual) < 2 or qual[-2] != DB_CLASS:
            continue
        stop = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        seg = [lines[i][m.end():]] + lines[i + 1:stop]
        for j, later in enumerate(seg):
            if later.startswith("------"):
                seg = seg[:j]
                break
        nonempty = [s.strip() for s in seg if s.strip()]
        if any(_BAD_RESULT.search(s) for s in nonempty):
            continue
        if nonempty and (nonempty[-1] == "ok"
                         or nonempty[-1].endswith(" ... ok")
                         or nonempty[-1] == "... ok"):
            passed.add(m.group(1))
    return sorted(passed)


def _load_parity():
    spec = importlib.util.spec_from_file_location("parity_mod", PARITY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _db_wiring_test_names(mod):
    return sorted(
        unittest.TestLoader().getTestCaseNames(getattr(mod, DB_CLASS)))


def _fixture_case():
    mod = _load_parity()
    return getattr(mod, DB_CLASS)(_db_wiring_test_names(mod)[0])


class ParityTempHygieneTest(unittest.TestCase):
    def test_parity_suite_leaves_no_temp_artifacts(self):
        expected = _db_wiring_test_names(_load_parity())
        self.assertTrue(expected, "%s has no tests" % DB_CLASS)
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
        # Positive control: every leg that creates a temp fixture reported
        # ok (a skipped leg reports "skipped", so it cannot satisfy this).
        self.assertEqual(
            _passed_db_legs(out), expected,
            "the fixture-creating %s legs did not all report ok, so the "
            "leak check below would pass vacuously: %s" % (DB_CLASS,
                                                          out[-400:]))
        self.assertEqual(
            sorted(p.name for p in Path(tmp_root).iterdir()), [],
            "the parity suite leaked temp artifacts into its TMP dir")
        self.assertEqual(
            sorted(p.name for p in Path(data_dir).iterdir()), [],
            "the parity suite wrote into its scratch ZMEM_DATA dir")

    def test_cleanup_removes_sqlite_siblings(self):
        scoped = tempfile.mkdtemp(prefix="zmem-parity-hygiene-inproc-")
        self.addCleanup(shutil.rmtree, scoped, ignore_errors=True)
        case = _fixture_case()
        with mock.patch.object(tempfile, "tempdir", scoped):
            path = case._make_db(with_part_table=True)
        self.assertEqual(
            os.path.dirname(path), scoped,
            "_make_db did not honor the scoped temp dir")
        self.assertTrue(os.path.exists(path))
        for s in SIBLINGS:
            open(path + s, "wb").close()
        self.assertTrue(
            case.doCleanups(), "the registered cleanup reported a failure")
        for s in ("",) + SIBLINGS:
            self.assertFalse(
                os.path.exists(path + s),
                "cleanup left %s behind" % (path + s))
        self.assertEqual(
            os.listdir(scoped), [],
            "the in-process leg left artifacts in its scoped temp dir")

    def test_cleanup_surfaces_non_missing_errors(self):
        scoped = tempfile.mkdtemp(prefix="zmem-parity-hygiene-errs-")
        self.addCleanup(shutil.rmtree, scoped, ignore_errors=True)
        case = _fixture_case()
        with mock.patch.object(tempfile, "tempdir", scoped):
            path = case._make_db(with_part_table=True)
        # A directory where a sibling file is expected makes os.remove fail
        # with an OSError that is NOT FileNotFoundError; the cleanup must
        # report that failure rather than swallow it.
        os.mkdir(path + "-wal")
        self.assertFalse(
            case.doCleanups(),
            "the cleanup did not report a non-missing removal failure for "
            "the -wal sibling (it was swallowed, or the sibling was never "
            "attempted)")

    def test_failed_build_leaves_no_artifact(self):
        # F-04b (owner-approved fold-in): if the fixture build itself fails
        # mid-way, the registered close must fire before the file removal,
        # so a Windows open handle cannot block it and the scoped dir ends
        # empty. Without the close registration this leg goes red. The
        # failure is forced through a real seam: pre-creating the table
        # makes the fixture's own CREATE TABLE raise OperationalError.
        import sqlite3
        scoped = tempfile.mkdtemp(prefix="zmem-parity-hygiene-failbuild-")
        self.addCleanup(shutil.rmtree, scoped, ignore_errors=True)
        case = _fixture_case()
        real_connect = sqlite3.connect

        def colliding_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            conn.execute("CREATE TABLE tool_usage(session_id TEXT)")
            return conn

        with mock.patch.object(tempfile, "tempdir", scoped), \
                mock.patch.object(sqlite3, "connect", colliding_connect):
            with self.assertRaises(sqlite3.OperationalError):
                case._make_db(with_part_table=True)
        case.doCleanups()
        self.assertEqual(
            os.listdir(scoped), [],
            "a failed fixture build left an unremovable artifact behind "
            "(the registered connection close is missing or misordered)")


if __name__ == "__main__":
    unittest.main()
