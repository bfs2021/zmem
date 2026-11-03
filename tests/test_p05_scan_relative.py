"""Issue-tracer bundle 329-340 acceptance check (red checkpoint, base
a988469): C4 / B9 (#333 AC2) — the stranded-code scan in
tests/test_p05_subagent_task_bridge.py (TaskTextStrandedTest.
test_task_text_functions_are_not_stranded, :237-239 at base) filters on
ABSOLUTE ``path.parts``, so a checkout under a directory named ``tests``
(or ``.agents``) skips every file and the scan passes VACUOUSLY (issue
#333 claim 5, executed by the review on a relocated tree).

This pin is functional, not source-textual: it builds a sandbox probe repo
whose ABSOLUTE path contains a ``tests`` directory component and whose
skills/ tree holds a GENUINELY stranded file (a ``def park_task_text``
with no caller), points the REAL scan at it by rebinding the bridge
module's REPO_ROOT, and runs the REAL frozen AC2 test. A repo-relative
scan must scan the sandbox, find the stranded definition, and FAIL with
the frozen polarity signature (``True != False``); the base absolute-parts
scan skips everything, passes vacuously, and this pin goes RED.

The fix itself (making the scan's skip predicate repo-relative) is the
implementer's job; any fix shape that stops ABSOLUTE path components from
disqualifying files flips this check green.

Runs standalone: python -X utf8 tests/test_p05_scan_relative.py
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
BRIDGE = TESTS_DIR / "test_p05_subagent_task_bridge.py"

# The bridge module imports _store_guard; running this file directly puts
# tests/ first on sys.path already.
sys.path.insert(0, str(TESTS_DIR))


def _load_bridge():
    spec = importlib.util.spec_from_file_location("p05_bridge_under_probe",
                                                  BRIDGE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TaskTextStrandedTest(unittest.TestCase):
    """C4 / B9 (#333 AC2): the stranded-code scan must key its skip
    predicate on REPO-RELATIVE path parts, never absolute ones."""

    def test_scan_uses_repo_relative_parts(self):
        # Sandbox: <tmp>/tests/probe-repo/{skills,hooks} — the ABSOLUTE
        # parts contain "tests" (the vacuous-skip shape), while the
        # repo-relative parts of everything inside probe-repo do not.
        outer = tempfile.mkdtemp(prefix="zmem-333-scan-")
        sandbox = os.path.join(outer, "tests", "probe-repo")
        skills = os.path.join(sandbox, "skills", "pkg")
        os.makedirs(skills)
        os.makedirs(os.path.join(sandbox, "hooks"), exist_ok=True)
        # A GENUINELY stranded file: a def with no caller line anywhere in
        # the scanned tree (the def line itself never counts as a caller).
        with open(os.path.join(skills, "stranded_probe.py"), "w",
                  encoding="utf-8") as handle:
            handle.write("def park_task_text():\n    return None\n")

        bridge = _load_bridge()
        original_root = bridge.REPO_ROOT
        try:
            bridge.REPO_ROOT = Path(sandbox)
            suite = unittest.TestSuite([
                bridge.TaskTextStrandedTest(
                    "test_task_text_functions_are_not_stranded")])
            result = unittest.TestResult()
            suite.run(result)
        finally:
            bridge.REPO_ROOT = original_root

        self.assertEqual(
            result.errors, [],
            "the frozen AC2 scan must SCAN, not crash: %r" % result.errors)
        self.assertEqual(
            len(result.failures), 1,
            "on a checkout whose ABSOLUTE path contains a 'tests' "
            "component, the scan must still scan every repository file and "
            "FAIL on a genuinely stranded def — at base it skips everything "
            "and passes vacuously (result: %d failures, %d skips)"
            % (len(result.failures), len(result.skipped)))
        self.assertIn(
            "True != False", result.failures[0][1],
            "the failure must be the frozen stranded polarity "
            "(assertEqual(stranded, False) with stranded True), not an "
            "unrelated accident: %r" % result.failures[0][1][:200])


if __name__ == "__main__":
    unittest.main(verbosity=2)
