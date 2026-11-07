"""Issue #442 AC1 — session-start parse gate transport classification.

WHEN the session-start store child exits ZERO and its stdout does not parse
as a JSON dict (malformed JSON, empty stdout, or valid JSON of a non-dict
type), the session-start decision line SHALL carry ``reason=envelope-error``
instead of ``reason=omitted``. The timeout arm and the nonzero-exit arm keep
``reason=omitted`` (normative transport-reason table in #442: those arms
predate the store-error token and the filed contract preserves them).

Drives the REAL hooks/lib/zmem-session-start-payload.py as a subprocess with
a stub ``store.py`` whose ``recent`` arm emits the shape under test (selected
by ``SS442_MODE``), then reads the ONE ``moment=session_start`` decision line
from the scratch data dir. A healthy dict-envelope control leg proves the
harness reads real decision lines (it must pass at base AND after the fix).

Run: python -X utf8 tests/test_session_start_envelope_error.py
"""
from __future__ import annotations

import atexit
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PAYLOAD = REPO_ROOT / "hooks" / "lib" / "zmem-session-start-payload.py"

# Issue #244 isolation: pin the store/data to a fresh scratch BEFORE the
# guard import, and drop every ambient key that could resolve ~/.zmem.
_TMP = tempfile.mkdtemp(prefix="zmem-442-ss-")
atexit.register(shutil.rmtree, _TMP, True)
os.environ.update({
    "ZMEM_STORE": os.path.join(_TMP, "store.sqlite"),
    "ZMEM_DATA": _TMP,
    "ZMEM_MODEL_AUTODOWNLOAD": "0",
    "PYTHONIOENCODING": "utf-8",
})
for _k in ("ZMEM_HOME", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
           "ZCODE_PROJECT_DIR", "CLAUDE_PROJECT_DIR",
           "ZMEM_SESSION", "ZMEM_SESSION_ID", "ZMEM_BACKUP_DIR"):
    os.environ.pop(_k, None)
sys.path.insert(0, str(REPO_ROOT / "tests"))
import _store_guard  # noqa: E402,F401  (issue #244 isolation guard)

sys.path.insert(0, str(REPO_ROOT / "tests" / "fixtures"))
from eval_store import BASE_ENV  # noqa: E402

NL = chr(10)
# The stub's ``recent`` arm emits exactly the shape named by SS442_MODE.
STUB_LINES = [
    "import os, sys, time, json",
    "mode = os.environ.get('SS442_MODE', 'healthy')",
    "if len(sys.argv) > 1 and sys.argv[1] == 'recent':",
    "    if mode == 'malformed':",
    "        sys.stdout.write('this-is-not-json{{{{' + chr(10))",
    "        sys.exit(0)",
    "    if mode == 'empty':",
    "        sys.exit(0)",
    "    if mode == 'nondict-list':",
    "        sys.stdout.write('[1, 2]' + chr(10))",
    "        sys.exit(0)",
    "    if mode == 'nondict-str':",
    "        sys.stdout.write(json.dumps('a bare json string') + chr(10))",
    "        sys.exit(0)",
    "    if mode == 'slow':",
    "        time.sleep(float(os.environ.get('SS442_SLOW_S', '4')))",
    "        sys.exit(0)",
    "    if mode == 'fail':",
    "        sys.exit(1)",
    "    if mode == 'fail-with-stdout':",
    "        sys.stdout.write(json.dumps({'results': [], 'reason': 'empty-pool'}) + chr(10))",
    "        sys.exit(3)",
    "    sys.stdout.write(json.dumps({'results': [], 'count': 0, 'omitted': 0,"
    " 'reason': 'empty-pool', 'candidate_ids': [], 'rendered': ''}) + chr(10))",
    "    sys.exit(0)",
    "elif len(sys.argv) > 1 and sys.argv[1] == 'promote':",
    "    print('0 promotion candidates')",
    "else:",
    "    print('')",
]

_REASON_RE = re.compile(r"\breason=(\S+)")


def drive_session_start(test: unittest.TestCase, mode: str,
                        timeout_s: str | None = None) -> tuple[str, str]:
    """Run the real payload once against the stub; return (reason, line).

    The returned line is the single ``moment=session_start`` decision line.
    """
    base = Path(tempfile.mkdtemp(prefix="zmem-442-ss-leg-", dir=_TMP))
    test.addCleanup(shutil.rmtree, base, ignore_errors=True)
    (base / "core.md").write_text("SS442-TIER0-CORE", encoding="utf-8",
                                  newline=NL)
    data = base / "data"
    data.mkdir()
    (base / "store.py").write_text(NL.join(STUB_LINES) + NL,
                                   encoding="utf-8", newline=NL)
    env = dict(os.environ)
    env.update(BASE_ENV)
    env.update({
        "ZMEM_STORE": str(data / "store.sqlite"),
        "ZMEM_DATA": str(data),
        "ZMEM_INJECT": "1",
        "ZMEM_NAMESPACE": "project:fixture/zmem",
        "SS442_MODE": mode,
    })
    for key in ("ZMEM_SESSION", "ZMEM_SESSION_ID", "ZMEM_HOME",
                "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
                "ZCODE_PROJECT_DIR", "CLAUDE_PROJECT_DIR", "ZMEM_PROJECT",
                "ZMEM_STORE_RECALL_TIMEOUT_S"):
        env.pop(key, None)
    if timeout_s is not None:
        env["ZMEM_STORE_RECALL_TIMEOUT_S"] = timeout_s
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(PAYLOAD),
         str(base / "core.md"), "", str(base / "store.py"), str(data),
         str(base), str(base), "project:fixture/zmem", "25000", "zcode",
         "", "", "sid-442", "", ""],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(REPO_ROOT), timeout=120)
    test.assertEqual(proc.returncode, 0,
                     f"payload must exit 0 (mode={mode}); stderr="
                     f"{proc.stderr[-400:]!r}")
    log = data / "zmem-decisions.log"
    test.assertTrue(log.exists(),
                    f"mode={mode}: the session-start decision line must land")
    lines = [ln for ln in log.read_text(encoding="utf-8").splitlines()
             if "moment=session_start" in ln]
    test.assertEqual(len(lines), 1,
                     f"mode={mode}: exactly one session-start decision line "
                     f"expected, got {lines!r}")
    match = _REASON_RE.search(lines[0])
    test.assertIsNotNone(match, f"mode={mode}: no reason= in {lines[0]!r}")
    return match.group(1), lines[0]


class SessionStartEnvelopeErrorTest(unittest.TestCase):
    """Zero-exit non-dict stdout -> reason=envelope-error (AC1)."""

    maxDiff = None

    def _assert_envelope_error(self, mode):
        reason, line = drive_session_start(self, mode)
        self.assertEqual(
            reason, "envelope-error",
            f"zero-exit stdout shape {mode!r} must log reason=envelope-error "
            f"(a corrupted output channel, not a deliberate omit); "
            f"observed reason={reason} line={line!r}")

    def test_malformed_json_stdout_is_envelope_error(self):
        self._assert_envelope_error("malformed")

    def test_empty_stdout_is_envelope_error(self):
        self._assert_envelope_error("empty")

    def test_valid_non_dict_list_json_is_envelope_error(self):
        self._assert_envelope_error("nondict-list")

    def test_valid_non_dict_string_json_is_envelope_error(self):
        self._assert_envelope_error("nondict-str")


class SessionStartPreservedOmittedArmsTest(unittest.TestCase):
    """Timeout and nonzero-exit arms keep reason=omitted (unchanged)."""

    def test_timeout_arm_stays_omitted(self):
        # 0.5 s cap (below-8.0 values are honored) vs a 4 s stub sleep.
        reason, line = drive_session_start(self, "slow", timeout_s="0.5")
        self.assertEqual(reason, "omitted",
                         f"timeout arm must stay omitted; line={line!r}")
        self.assertIn("store_timeout=1", line)

    def test_nonzero_exit_stays_omitted(self):
        reason, line = drive_session_start(self, "fail")
        self.assertEqual(reason, "omitted",
                         f"nonzero arm must stay omitted; line={line!r}")
        self.assertNotIn("store_timeout=1", line)

    def test_nonzero_exit_with_dict_stdout_stays_omitted(self):
        # A failed child's stdout is never consulted: a nonzero exit that
        # happens to print a dict envelope is still the nonzero arm.
        reason, line = drive_session_start(self, "fail-with-stdout")
        self.assertEqual(reason, "omitted",
                         f"nonzero arm must stay omitted; line={line!r}")


class SessionStartHarnessControlTest(unittest.TestCase):
    """A healthy silent dict envelope keeps the store's own reason."""

    def test_healthy_dict_envelope_keeps_store_reason(self):
        reason, line = drive_session_start(self, "healthy")
        self.assertEqual(reason, "empty-pool",
                         f"healthy envelope control; line={line!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
