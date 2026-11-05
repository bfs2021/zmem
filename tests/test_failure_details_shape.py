"""Issue #439 AC11: ``store.py failures --transcript`` detail shape.

At base the transcript miner's pass 1 keeps only tool_use_id -> tool name
(the failed command / file path is discarded) and the detail dict is
``{"tool", "error"}`` -- an information-starved shape that cannot support
the #439 trivial-failure classifier. The frozen post-fix contract: each
transcript-path detail for a Bash failure carries a REDACTED, capped
``command`` (cap constant ``MAX_COMMAND_CHARS``, shared with
capture_quality), and each file-shaped failure carries ``path``.

Drives the real ``store.py failures --transcript`` CLI on a synthesized
Claude Code transcript fixture (assistant tool_use blocks carrying
input.command / input.file_path, paired tool_result is_error blocks) with a
hermetic temp env; the operator's real store is never touched (the failures
command branches before any store connect, and ZMEM_* ambient vars are
stripped from the child).

Run: python tests/test_failure_details_shape.py
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS / "store.py"
sys.path.insert(0, str(SCRIPTS))
from capture_quality import MAX_COMMAND_CHARS  # noqa: E402

_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_SESSION", "ZMEM_AGENT_ID", "ZMEM_AGENT_TRANSCRIPT",
    "ZMEM_AGENT_TYPE", "ZMEM_TRANSCRIPT", "ZMEM_ZCODE_DB",
    "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID", "CLAUDE_PLUGIN_DATA",
    "ZCODE_PLUGIN_DATA", "CLAUDE_PROJECT_DIR", "ZCODE_PROJECT_DIR",
    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR", "ZMEM_AUTO_RETAIN",
)

# A synthetic bearer-shaped secret (redaction pattern vocabulary); fixture
# data authored by this test, never real capture content.
_BEARER_TOKEN = "abcdefghijklmnop0123456789"


def _clean_env(tmp: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env.update({
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONUTF8": "1",
    })
    return env


def _tool_use(tid: str, name: str, tool_input: dict) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": name, "input": tool_input}]}}


def _failed_tool_result(tid: str, text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "is_error": True,
         "content": text}]}}


def _write_transcript(tmp: str, records: list) -> Path:
    path = Path(tmp) / "agent-transcript.jsonl"
    path.write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


class FailureDetailsShapeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-ac439-details-")
        self.addCleanup(_rmtree, self._tmp)

    def _failures(self, records: list) -> dict:
        transcript = _write_transcript(self._tmp, records)
        r = subprocess.run(
            [sys.executable, str(STORE_PY), "failures",
             "--transcript", str(transcript)],
            capture_output=True, text=True, env=_clean_env(self._tmp),
            timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_details_carry_command_and_path(self):
        obj = self._failures([
            _tool_use("t1", "Bash", {"command": "git push origin main"}),
            _failed_tool_result("t1", "error: failed to push some refs"),
            _tool_use("t2", "Read", {"file_path": "src/missing.py"}),
            _failed_tool_result("t2", "File does not exist"),
        ])
        self.assertGreaterEqual(obj.get("count", 0), 2)
        details = obj.get("details") or []
        by_tool = {d.get("tool"): d for d in details}
        bash = by_tool.get("Bash")
        self.assertIsNotNone(bash, "the Bash failure detail is missing")
        self.assertIn("command", bash,
                      "the Bash failure detail must carry the failed command "
                      "(at base pass 1 discards it)")
        self.assertIn("git push", bash["command"])
        read = by_tool.get("Read")
        self.assertIsNotNone(read, "the Read failure detail is missing")
        self.assertIn("path", read,
                      "the file-shaped failure detail must carry the path")
        self.assertIn("src/missing.py", read["path"])

    def test_command_is_capped_at_max_command_chars(self):
        long_command = "echo " + "a" * (4 * MAX_COMMAND_CHARS)
        obj = self._failures([
            _tool_use("t1", "Bash", {"command": long_command}),
            _failed_tool_result("t1", "Exit code 1"),
        ])
        details = obj.get("details") or []
        bash = next((d for d in details if d.get("tool") == "Bash"), None)
        self.assertIsNotNone(bash, "the Bash failure detail is missing")
        self.assertIn("command", bash,
                      "the Bash failure detail must carry the failed command")
        self.assertLessEqual(
            len(bash["command"]), MAX_COMMAND_CHARS,
            "the detail command must be capped at the named "
            "MAX_COMMAND_CHARS constant")

    def test_command_is_redacted(self):
        command = ("curl -s -H 'Authorization: Bearer %s' "
                   "https://internal.example.test/v1/thing" % _BEARER_TOKEN)
        obj = self._failures([
            _tool_use("t1", "Bash", {"command": command}),
            _failed_tool_result("t1", "Exit code 7"),
        ])
        details = obj.get("details") or []
        bash = next((d for d in details if d.get("tool") == "Bash"), None)
        self.assertIsNotNone(bash, "the Bash failure detail is missing")
        self.assertIn("command", bash,
                      "the Bash failure detail must carry the failed command")
        self.assertNotIn(
            _BEARER_TOKEN, bash["command"],
            "a bearer-shaped secret echoed in the failed command must not "
            "survive into the detail (redacted before capping)")


def _rmtree(path: str) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
