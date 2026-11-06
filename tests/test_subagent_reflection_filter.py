"""Issue #439 AC12-AC16: the subagent reflection hand-off filter.

At base ``hooks/zmem-subagent-reflect.sh`` writes a parent-hand-off sidecar
for ANY failed tool call in a dispatched subagent (``count > 0``, no
lesson, no rendered rejections) and ``hooks/zmem-reflect.sh`` surfaces any
pending sidecar -- so a single read-only search miss costs the parent an
extra reflection turn. The frozen post-fix contract:

- a subagent whose every failure is a read-only search miss (Bash grep exit
  1, Grep of a nonexistent path, Read of a nonexistent path) writes NO
  hand-off sidecar (AC12);
- a runner failure or a user rejection still writes the sidecar on first
  occurrence (AC13); an unrecognized failure keeps today's behavior (AC14);
- a batch of subagents with only filtered failures emits no reflection
  prompt at the parent Stop (AC15);
- filtered subagents are counted in a per-session counter file under the
  ring, ``<sha256(session_id)[:32]>.filtered.json``, whose JSON object
  carries an integer ``filtered_trivial`` count -- THE frozen contract for
  the counter surface the closeout skill reads (AC16; count semantics only,
  no prose rendering).

Drives both hooks end-to-end via bash with a hermetic temp data dir and
synthesized agent transcripts; ambient ZMEM_/CLAUDE_/ZCODE_ vars are
stripped so the operator's real store/data are never touched.

Run: python tests/test_subagent_reflection_filter.py
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
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SUBAGENT_HOOK = REPO_ROOT / "hooks" / "zmem-subagent-reflect.sh"
PARENT_HOOK = REPO_ROOT / "hooks" / "zmem-reflect.sh"

_NS = "project:ac439filter"
_COUNTER_KEY = "filtered_trivial"


def _bash() -> str:
    bash = shutil.which("bash")
    if os.name == "nt":
        bash = next((str(p) for p in (
            Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
            Path(r"C:\Program Files\Git\bin\bash.exe"),
        ) if p.is_file()), bash)
    if not bash:
        raise unittest.SkipTest("bash is required for hook integration")
    return bash


def _hook_env(data_dir: str, session: str, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("ZMEM_", "CLAUDE_", "ZCODE_"))}
    env.update({
        "ZMEM_CAPTURE": "1",
        "ZMEM_REFLECT": "1",
        "ZMEM_ROOT": str(REPO_ROOT),
        "ZMEM_DATA": data_dir,
        "ZMEM_STORE": os.path.join(data_dir, "store.sqlite"),
        "ZMEM_NAMESPACE": _NS,
        "ZMEM_SESSION": session,
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_AUTO_RETAIN": "0",
        "ZMEM_ZCODE_DB": os.path.join(data_dir, "no-db.sqlite"),
        "PYTHONUTF8": "1",
    })
    env.update(extra)
    return env


def _run_hook(script: Path, env: dict, payload: str = "{}") -> str:
    r = subprocess.run(
        [_bash(), str(script)], input=payload, capture_output=True,
        text=True, env=env, timeout=120)
    # PRR-009a: a crashed hook must fail the test, not pass absence
    # assertions vacuously. Note the wrapper swallows python-block errors
    # (2>/dev/null || echo '{}'), so this catches bash-level failures;
    # file-level siblings are the compensating detector for in-body
    # crashes.
    if r.returncode != 0:
        raise AssertionError(
            "hook %s exited %d: %s" % (script.name, r.returncode,
                                       (r.stderr or "")[-400:]))
    return r.stdout or ""


def _assistant_tool_use(tid: str, name: str, tool_input: dict) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": name,
         "input": tool_input}]}}


def _tool_result(tid: str, text: str, is_error: bool = True) -> dict:
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "is_error": is_error,
         "content": text}]}}


def _write_agent_transcript(data_dir: str, agent_id: str,
                            records: list) -> str:
    path = os.path.join(data_dir, agent_id + ".jsonl")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
    return path


def _ring_dir(data_dir: str) -> Path:
    return Path(data_dir) / "subagent-reflections"


def _sidecars(data_dir: str) -> list:
    """Hand-off sidecars only: the per-session filtered counter (AC16,
    ``<hash>.filtered.json``) shares the ring post-fix and is not a
    sidecar."""
    ring = _ring_dir(data_dir)
    if not ring.is_dir():
        return []
    return sorted(p.name for p in ring.glob("*.json")
                  if not p.name.endswith(".filtered.json"))


def _run_subagent(data_dir: str, session: str, agent_id: str,
                  records: list) -> None:
    transcript = _write_agent_transcript(data_dir, agent_id, records)
    out = _run_hook(
        SUBAGENT_HOOK, _hook_env(
            data_dir, session, ZMEM_AGENT_ID=agent_id,
            ZMEM_AGENT_TYPE="general-purpose",
            ZMEM_AGENT_TRANSCRIPT=transcript))
    # The subagent hook NEVER emits a prompt (issue #204): bare envelope.
    assert "additionalContext" not in out, "subagent hook emitted a prompt"


_TRIVIAL_RECORDS = [
    _assistant_tool_use("t1", "Bash",
                        {"command": "grep -r needle /nonexistent-dir"}),
    _tool_result("t1", "Exit code 1"),
    _assistant_tool_use("t2", "Grep", {"path": "/nonexistent-dir"}),
    _tool_result("t2", "No files found"),
    _assistant_tool_use("t3", "Read", {"file_path": "/nonexistent.md"}),
    _tool_result("t3", "File does not exist"),
]

_RUNNER_RECORDS = [
    _assistant_tool_use("t1", "Bash",
                        {"command": "pytest tests/test_example.py"}),
    _tool_result("t1", "Exit code 1"),
]

_REJECTION_RECORDS = [
    _assistant_tool_use("t1", "Edit", {"file_path": "src/thing.py"}),
    _tool_result(
        "t1",
        "The user doesn't want to proceed with this tool use. "
        "The user said: use the other file instead."),
]

_UNRECOGNIZED_RECORDS = [
    _assistant_tool_use("t1", "Bash", {"command": "npx vitest run"}),
    _tool_result("t1", "Exit code 1"),
]


class _HookTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-ac439-filter-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.session = "ac439-session"

    @property
    def data_dir(self) -> str:
        return self._tmp


class TrivialMissWritesNoSidecarTest(_HookTest):
    """AC12 (DISCRIMINATING): a subagent whose ONLY failures are read-only
    search misses must write no hand-off sidecar."""

    def test_read_only_misses_write_no_sidecar(self):
        _run_subagent(self.data_dir, self.session, "agent-trivial",
                      _TRIVIAL_RECORDS)
        self.assertEqual(
            _sidecars(self.data_dir), [],
            "a subagent with only trivial read-only misses must not cost "
            "the parent a reflection hand-off")


class SignalFailureStillWritesSidecarTest(_HookTest):
    """AC13 (PRESERVING, base-GREEN): runner failures and user rejections
    still surface on first occurrence."""

    def test_runner_failure_writes_sidecar(self):
        _run_subagent(self.data_dir, self.session, "agent-runner",
                      _RUNNER_RECORDS)
        self.assertEqual(len(_sidecars(self.data_dir)), 1,
                         "a runner failure must still write the sidecar")

    def test_user_rejection_writes_sidecar(self):
        _run_subagent(self.data_dir, self.session, "agent-reject",
                      _REJECTION_RECORDS)
        self.assertEqual(len(_sidecars(self.data_dir)), 1,
                         "a rendered user rejection must still write the "
                         "sidecar")


class UnrecognizedFailureStillWritesSidecarTest(_HookTest):
    """AC14 (PRESERVING, base-GREEN): failures outside the trivial filter
    keep today's behavior (regression pin on a single failed runner the
    filter does not recognize)."""

    def test_single_unrecognized_failure_writes_sidecar(self):
        _run_subagent(self.data_dir, self.session, "agent-npx",
                      _UNRECOGNIZED_RECORDS)
        self.assertEqual(len(_sidecars(self.data_dir)), 1,
                         "an unrecognized single failure keeps today's "
                         "one-sidecar behavior")


class BatchTrivialEmitsNoPromptTest(_HookTest):
    """AC15 (DISCRIMINATING): a batch of subagents with only filtered
    failures emits no reflection prompt at the parent Stop."""

    def test_batch_of_trivial_subagents_emits_no_reflection_prompt(self):
        for i in range(3):
            _run_subagent(self.data_dir, self.session,
                          "agent-batch-%d" % i, _TRIVIAL_RECORDS)
        out = _run_hook(
            PARENT_HOOK, _hook_env(self.data_dir, self.session))
        self.assertNotIn(
            "dispatched subagent", out,
            "a batch whose every sidecar was filtered must not emit the "
            "subagent reflection prompt at the parent Stop")


class FilteredCounterTest(_HookTest):
    """AC16 (frozen contract): after N subagents whose only failures were
    filtered as trivial, the per-session counter
    ``<sha256(session_id)[:32]>.filtered.json`` in the ring records the
    ``filtered_trivial`` count (an integer; one trivial failure per
    subagent here, so the count is exactly N under either per-failure or
    per-subagent accounting)."""

    def test_filtered_trivial_counter_records_count(self):
        n = 3
        for i in range(n):
            _run_subagent(self.data_dir, self.session,
                          "agent-count-%d" % i, _TRIVIAL_RECORDS[:2])
        digest = hashlib.sha256(
            self.session.encode("utf-8")).hexdigest()[:32]
        counter = _ring_dir(self.data_dir) / (digest + ".filtered.json")
        self.assertTrue(
            counter.is_file(),
            "filtered subagents must land in the per-session counter file "
            "%s (name frozen by this check)" % counter.name)
        obj = json.loads(counter.read_text(encoding="utf-8"))
        self.assertIsInstance(obj, dict)
        self.assertIn(_COUNTER_KEY, obj)
        self.assertIsInstance(obj[_COUNTER_KEY], int)
        self.assertEqual(obj[_COUNTER_KEY], n)


if __name__ == "__main__":
    unittest.main()
