"""Issue #439 supplementary pins for the per-session filtered_trivial
counter's lock semantics (plan-critic r2/NB4 + r3/B4; the frozen C16 pins
the happy path only):

- counting is per trivial DETAIL (one subagent with three trivial misses
  increments by three);
- an orphaned lock (aged, holder provably dead) is reclaimed and the
  increment lands;
- a lock whose holder is ALIVE past the grace age is NOT reclaimed — the
  increment is skipped, never stolen;
- sequential writers account additively (no lost increments).

Drives the real hook end-to-end with the same harness shape as the frozen
tests/test_subagent_reflection_filter.py.

Run: python tests/test_reflection_counter_lock.py
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "hooks" / "zmem-subagent-reflect.sh"
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
NS = "project:counter-lock-test"

_TRIVIALS = [
    ("t1", "Bash", {"command": "grep -r needle /nope"}, "Exit code 1"),
    ("t2", "Bash", {"command": "ls /missing-dir"}, "Exit code 1"),
    ("t3", "Bash", {"command": "wc -l /missing.txt"}, "Exit code 1"),
]


def _bash():
    bash = shutil.which("bash")
    for cand in (r"C:\Program Files\Git\bin\bash.exe",):
        if Path(cand).is_file():
            return cand
    if bash and Path(bash).is_file():
        return bash
    raise unittest.SkipTest("bash is required for hook integration")


def _hook_env(data_dir: str, session: str, agent_id: str,
              transcript: str) -> dict:
    # Same env-driven shape as the frozen filter tests: the hook reads its
    # per-subagent inputs from ZMEM_AGENT_* variables.
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("ZMEM_", "CLAUDE_", "ZCODE_"))}
    env.update({
        "ZMEM_CAPTURE": "1", "ZMEM_REFLECT": "1", "ZMEM_ROOT": str(REPO_ROOT),
        "ZMEM_DATA": data_dir, "ZMEM_STORE": os.path.join(data_dir,
                                                          "store.sqlite"),
        "ZMEM_NAMESPACE": NS, "ZMEM_SESSION": session,
        "ZMEM_MODEL_AUTODOWNLOAD": "0", "ZMEM_AUTO_RETAIN": "0",
        "ZMEM_ZCODE_DB": os.path.join(data_dir, "no-db.sqlite"),
        "ZMEM_AGENT_ID": agent_id, "ZMEM_AGENT_TYPE": "general-purpose",
        "ZMEM_AGENT_TRANSCRIPT": transcript,
        "PYTHONUTF8": "1",
    })
    return env


def _transcript(data_dir: str, agent_id: str, cases) -> str:
    records = [
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": tool, "input": inp}
            for tid, tool, inp, _ in cases]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "is_error": True,
             "content": err}
            for tid, _, _, err in cases]}},
    ]
    path = os.path.join(data_dir, agent_id + ".jsonl")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
    return path


def _run_hook(data_dir: str, session: str, agent_id: str, cases) -> str:
    transcript = _transcript(data_dir, agent_id, cases)
    r = subprocess.run(
        [_bash(), str(HOOK)],
        input="{}", capture_output=True, text=True,
        env=_hook_env(data_dir, session, agent_id, transcript), timeout=120)
    return r.stdout or ""


def _ring(data_dir: str) -> Path:
    return Path(data_dir) / "subagent-reflections"


def _counter(data_dir: str, session: str):
    key = hashlib.sha256(session.encode("utf-8")).hexdigest()[:32]
    path = _ring(data_dir) / (key + ".filtered.json")
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


class CounterLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="zmem-counter-lock-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def test_multi_miss_subagent_counts_details(self):
        out = _run_hook(self._tmp, "s-lock", "a1", _TRIVIALS)
        self.assertNotIn("additionalContext", out)
        obj = _counter(self._tmp, "s-lock")
        self.assertIsNotNone(obj)
        self.assertEqual(obj.get("filtered_trivial"), 3)

    def test_orphaned_lock_is_reclaimed(self):
        key = hashlib.sha256(b"s-orphan").hexdigest()[:32]
        ring = _ring(self._tmp)
        ring.mkdir(parents=True, exist_ok=True)
        lock = ring / ("." + key + ".filtered.lock")
        # Aged 60 s, holder pid provably dead (pid 1 is not this process;
        # os.kill on a dead-or-foreign pid raises OSError on POSIX and is
        # refused on Windows builds where the age+token path applies).
        lock.write_text("999999:deadbeef", encoding="ascii")
        old = time.time() - 60.0
        os.utime(lock, (old, old))
        out = _run_hook(self._tmp, "s-orphan", "a1", _TRIVIALS[:1])
        self.assertNotIn("additionalContext", out)
        obj = _counter(self._tmp, "s-orphan")
        self.assertIsNotNone(obj)
        self.assertEqual(obj.get("filtered_trivial"), 1)

    def test_live_holder_past_grace_is_not_reclaimed(self):
        key = hashlib.sha256(b"s-live").hexdigest()[:32]
        ring = _ring(self._tmp)
        ring.mkdir(parents=True, exist_ok=True)
        lock = ring / ("." + key + ".filtered.lock")
        # Holder is THIS process: alive, and aged past the grace.
        lock.write_text("%d:alive-token" % os.getpid(), encoding="ascii")
        old = time.time() - 60.0
        os.utime(lock, (old, old))
        out = _run_hook(self._tmp, "s-live", "a1", _TRIVIALS[:1])
        self.assertNotIn("additionalContext", out)
        # The lock was never stolen; the counter was skipped, not written.
        self.assertIsNone(_counter(self._tmp, "s-live"))
        self.assertTrue(lock.is_file())
        # Clean up so the retention sweep does not see our artifacts.
        lock.unlink(missing_ok=True)

    def test_sequential_writers_account_additively(self):
        _run_hook(self._tmp, "s-seq", "a1", _TRIVIALS)
        _run_hook(self._tmp, "s-seq", "a2", _TRIVIALS[:2])
        obj = _counter(self._tmp, "s-seq")
        self.assertIsNotNone(obj)
        self.assertEqual(obj.get("filtered_trivial"), 5)


if __name__ == "__main__":
    unittest.main()
