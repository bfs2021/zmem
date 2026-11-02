"""Issue-tracer bundle 329-340 acceptance checks (red checkpoint, base
a988469): ADAPTER- and CLI-surface guards for the delegation-recall
family. NEW file because both frozen hosts are at/near the 500-line cap
(tests/test_subagent_tasktext.py is 491 lines; a base-red check appended
to tests/test_p05_subagent_task_bridge.py would break its whole-file
PRESERVING floor).

Checks hosted here (one AC = one check; canonical argv form
``python -X utf8 tests/test_tasktext_adapter_guards.py <Class>.<method>``):

- C5 / B10 (#333 AC3) TaskToolFallbackTest — a pretool event whose
  tool_name is ``Task`` and carries ONLY a ``description`` parks that
  description on the store argv (real adapter, importlib-driven, store
  subprocess captured — the adapter module imports standalone with zero
  storelib imports).
- C8 / B13 (#335 AC1) NulPromptTest — a NUL-bearing delegation prompt
  still builds the store argv, with the NUL stripped (the parent's recall
  survives; no ValueError).
- C14 (supplementary #333 claim 4) MomentGatePinTest — ``recall`` with
  ``--delegation-prompt`` and a NON-pretool moment parks NOTHING, while
  the same argv with ``--moment pretool`` does park (the control that
  keeps the negative non-vacuous). Drives the real CLI in a subprocess.

Env is pinned at the top before anything store-touching; the adapter is
driven through importlib (tests/test_decision_log.py pattern) with
subprocess.check_output/subprocess.run captured so no store child ever
runs. Runs standalone: python -X utf8 tests/test_tasktext_adapter_guards.py
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
BODY = REPO_ROOT / "hooks" / "lib" / "zmem-recall-body.py"

# Scratch env pinned before anything store-touching runs in this process
# (the module imports no storelib, but the guard and the adapter's
# _data_dir()/decision-log writes must land in scratch, never ~/.zmem).
_MODULE_SCRATCH = tempfile.mkdtemp(prefix="zmem-329340-adapter-")
os.environ["ZMEM_STORE"] = os.path.join(_MODULE_SCRATCH, "store.sqlite")
os.environ["ZMEM_DATA"] = _MODULE_SCRATCH
os.environ["ZMEM_MODELS_DIR"] = os.path.join(_MODULE_SCRATCH, "missing-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
for _k in ("ZMEM_SESSION", "CLAUDE_SESSION_ID", "ZMEM_INJECT",
           "ZMEM_QUERY_CONTEXT", "ZMEM_CROSS_PROJECT", "ZMEM_HOST",
           "ZMEM_DELIVER_WINDOW_S"):
    os.environ.pop(_k, None)

# Ambient keys every store-touching child env must shed (the
# tests/test_subagent_tasktext.py _STRIP_ENV convention).
_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
    "ZMEM_INJECT_FLOOR_LEX", "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR",
    "ZMEM_SESSION", "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID",
    "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "CLAUDE_PLUGIN_ROOT", "ZCODE_PLUGIN_ROOT", "PLUGIN_ROOT", "PLUGIN_DATA",
    "ZMEM_PENDING_SIDECAR", "ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP",
    "ZMEM_SESSION_SOURCE", "ZMEM_TRANSCRIPT", "ZMEM_CROSS_PROJECT",
)


def _body_module():
    """Load the REAL hook adapter standalone (zero storelib imports)."""
    spec = importlib.util.spec_from_file_location("zmem_329340_body", BODY)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FakeStdin:
    """The adapter reads the hook event from sys.stdin.buffer."""

    def __init__(self, event: dict):
        self.buffer = io.BytesIO(json.dumps(event).encode("utf-8"))


class _FakeProc:
    returncode = 1
    stdout = b"{}"


def _drive_adapter(body, event: dict, mode: str = "pretool"):
    """Run the adapter's main() with the store subprocess CAPTURED.

    Returns (returncode, store_argv_list). check_output is faked (returns an
    empty envelope) and subprocess.run is faked too, so no real child ever
    spawns (the lane-health probe and the log rotator both ride run()).
    """
    calls: list = []

    def _fake_check_output(command, **kwargs):
        calls.append(list(command))
        return b"{}"

    argv = ["body", str(SCRIPTS / "store.py"), "project:pinadapter",
            "25000", mode]
    sink = io.StringIO()
    with mock.patch.object(body.subprocess, "check_output",
                           _fake_check_output), \
            mock.patch.object(body.subprocess, "run",
                              lambda *a, **k: _FakeProc()), \
            mock.patch.object(sys, "argv", argv), \
            mock.patch.object(sys, "stdin", _FakeStdin(event)), \
            contextlib.redirect_stdout(sink):
        rc = body.main()
    return rc, calls


def _ops_path(tmp: str, sid: str, suffix: str) -> Path:
    h = hashlib.sha256(sid.encode("utf8")).hexdigest()[:32]
    return Path(tmp) / "ops" / (h + suffix)


class TaskToolFallbackTest(unittest.TestCase):
    """C5 / B10 (#333 AC3): a ``Task`` pretool event carrying only a
    ``description`` parks that description (the adapter's prompt/description
    fallback, hooks/lib/zmem-recall-body.py:881-891 — pinned nowhere at
    base)."""

    def test_task_tool_description_is_parked(self):
        desc = "triage the flaky windows pytest teardown quarantine lane"
        rc, calls = _drive_adapter(_body_module(), {
            "hook_event_name": "PreToolUse",
            "tool_name": "Task",
            "tool_input": {"description": desc},
            "session_id": "pin-333-task",
        })
        self.assertEqual(rc, 0, "the adapter must complete cleanly")
        self.assertTrue(calls, "the adapter must attempt a store call")
        cmd = calls[0]
        self.assertIn(
            "--delegation-prompt", cmd,
            "a Task-tool pretool event with only a description must park "
            "that description; store argv was %r" % cmd)
        i = cmd.index("--delegation-prompt")
        self.assertEqual(
            cmd[i + 1], desc,
            "the parked delegation text must be the description itself")


class NulPromptTest(unittest.TestCase):
    """C8 / B13 (#335 AC1): a NUL in the delegating prompt must not kill
    the parent's recall — the store argv is still built, with the NUL
    stripped (adapter :892-897, fixed in 0.98.0, pinned here)."""

    def test_nul_prompt_keeps_parent_recall(self):
        prompt = "fix the merge-queue ratchet\x00flake quarantine lane"
        rc, calls = _drive_adapter(_body_module(), {
            "hook_event_name": "PreToolUse",
            "tool_name": "Agent",
            "tool_input": {"prompt": prompt},
            "session_id": "pin-335-nul",
        })
        self.assertEqual(rc, 0, "a NUL prompt must not fail the adapter")
        self.assertTrue(calls, "the parent's store call must still run")
        cmd = calls[0]
        for part in cmd:
            self.assertNotIn(
                "\x00", part,
                "no argv element may carry a NUL (ValueError: embedded "
                "null character at spawn time)")
        self.assertIn("--delegation-prompt", cmd,
                      "the delegation must still be parked")
        i = cmd.index("--delegation-prompt")
        self.assertEqual(
            cmd[i + 1], prompt.replace("\x00", ""),
            "the parked text must be the prompt with the NUL stripped")


class MomentGatePinTest(unittest.TestCase):
    """C14 (supplementary #333 claim 4): the park is honored ONLY on the
    pretool moment — a NON-pretool ``recall`` carrying --delegation-prompt
    parks NOTHING. Negative plus a pretool CONTROL (the control proves the
    flag is wired, so the absence on other moments is the gate at work)."""

    SID = "pin-333-gate"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-333-gate-")
        env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
        env.update({
            "ZMEM_STORE": os.path.join(self.tmp, "store.sqlite"),
            "ZMEM_DATA": self.tmp,
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "PYTHONUTF8": "1",
            "ZMEM_MODELS_DIR": os.path.join(self.tmp, "missing-models"),
        })
        self.env = env
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "store.py"), "add",
             "--namespace", "project:pin333gate", "--type", "fact",
             "--content", "gate probe seed row", "--signal", "test",
             "--confidence", "0.9"],
            capture_output=True, text=True, env=env, timeout=180,
            stdin=subprocess.DEVNULL)
        assert r.returncode == 0, f"seed failed: {r.stdout}\n{r.stderr}"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _recall(self, moment: str):
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "store.py"), "recall",
             "--namespace", "project:pin333gate",
             "--query", "gate probe query",
             "--limit", "5", "--include-global", "--global-limit", "3",
             "--no-bump", "--for-injection", "--json",
             "--session-id=" + self.SID, "--moment", moment,
             "--lane", "zcode",
             "--delegation-prompt", "must not park off the pretool moment"],
            capture_output=True, text=True, env=self.env, timeout=180,
            stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_non_pretool_moment_does_not_park(self):
        self._recall("subagent")
        self.assertFalse(
            _ops_path(self.tmp, self.SID, ".tasktext").exists(),
            "recall with --delegation-prompt on a NON-pretool moment must "
            "NOT park (moment gate, cli.py _park_delegation_prompt)")
        self._recall("pretool")
        self.assertTrue(
            _ops_path(self.tmp, self.SID, ".tasktext").exists(),
            "CONTROL: the identical argv with --moment pretool must park — "
            "without this the negative above could pass vacuously")


if __name__ == "__main__":
    unittest.main(verbosity=2)
