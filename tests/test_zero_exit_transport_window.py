"""Issue #435 AC4: the ``envelope-error`` decision token must be EXCLUDED
from the miss-rate retrieval-miss count, exactly like ``store-error``.

A zero-exit store child whose stdout is not a JSON dict envelope is a
transport failure, not a retrieval miss: counting it as missed keeps the old
``empty-pool`` masquerade's headline effect under a new name. The post-fix
contract (frozen in the trace design calls): ``envelope-error`` joins the
deliberate-silence/transport exclusion set in ``miss_rate`` so a failure in
such a window classifies as excluded (the ``disabled`` bucket), while a
genuine ``empty-pool`` silent window in the same report still counts as a
real miss -- the changed arm must not absorb the unchanged one.

Fixture style mirrors the issue #94 join tests: a throwaway temp store
seeded with one matching lesson (via ``store.py add``), a synthetic ZCode
episodic db with one matching Bash failure, and a decision log written in the
exact writer line shape. Ambient zmem env is stripped from every child; the
operator's real store is never touched.

Runs standalone: python tests/test_zero_exit_transport_window.py
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS / "store.py"
sys.path.insert(0, str(SCRIPTS))
from storelib import miss_rate  # noqa: E402

NS = "project:ac435window"
LESSON = ("ac435canary: git stash pop conflicts need stash drop after "
          "resolve, verified by test")

_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR", "ZMEM_CONVENTION_INTERVAL",
    "ZMEM_SESSION", "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID",
    "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA", "CLAUDE_PROJECT_DIR",
    "ZCODE_PROJECT_DIR", "ZMEM_EMBED_PROFILE", "ZMEM_TEST_NOW",
    "ZMEM_AUTO_REKEY", "ZMEM_TRANSCRIPT", "ZMEM_ZCODE_DB",
)


def _clean_env(tmp: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env.update({
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONUTF8": "1",
    })
    return env


def _seed_store(tmp: str) -> None:
    r = subprocess.run(
        [sys.executable, str(STORE_PY), "add",
         "--namespace", NS, "--type", "lesson", "--content", LESSON,
         "--signal", "test", "--json"],
        capture_output=True, text=True, env=_clean_env(tmp), timeout=120,
    )
    assert r.returncode == 0, "fixture seed failed (see stderr)"


def _make_fixture_db(path: str, failures: list) -> None:
    """Synthetic ZCode episodic db: tool_usage + part tables with the real
    column shapes the miners query (mirrors the issue #94 fixture helper)."""
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE tool_usage (
        id TEXT, session_id TEXT NOT NULL, turn_id TEXT, trace_id TEXT,
        tool_call_id TEXT NOT NULL, tool_name TEXT NOT NULL,
        side_effect_scope TEXT, read_only INTEGER, destructive INTEGER,
        approval_status TEXT, status TEXT NOT NULL, started_at INTEGER,
        first_output_at INTEGER, completed_at INTEGER, duration_ms INTEGER,
        time_to_first_output_ms INTEGER, exit_code INTEGER,
        output_bytes INTEGER NOT NULL DEFAULT 0,
        stdout_bytes INTEGER NOT NULL DEFAULT 0,
        stderr_bytes INTEGER NOT NULL DEFAULT 0,
        truncated INTEGER NOT NULL DEFAULT 0,
        retry_count INTEGER NOT NULL DEFAULT 0,
        retryable INTEGER NOT NULL DEFAULT 0,
        cancelled_by_user INTEGER, error_type TEXT, error_code TEXT,
        error_message TEXT)""")
    conn.execute("""CREATE TABLE part (
        id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER,
        time_updated INTEGER, data TEXT, sequence INTEGER)""")
    for f in failures:
        conn.execute(
            "INSERT INTO tool_usage (session_id, completed_at, tool_name,"
            " tool_call_id, read_only, status, exit_code, error_message,"
            " error_type) VALUES (?, ?, ?, ?, 0, ?, ?, ?, '')",
            (f["session_id"], int(f["ts_s"]) * 1000, f.get("tool", "Bash"),
             f["call_id"], f.get("status", "completed"),
             f.get("exit_code", 1), f.get("error_message", "")))
        if f.get("operation"):
            conn.execute(
                "INSERT INTO part (session_id, data) VALUES (?, ?)",
                (f["session_id"], json.dumps({
                    "type": "tool", "callID": f["call_id"],
                    "tool": f.get("tool", "Bash"),
                    "state": {"status": "error",
                              "input": {"command": f["operation"]},
                              "error": f.get("error_message", "")},
                })))
    conn.commit()
    conn.close()


def _bg_line(ts: int, ids, sid: str, reason: str) -> str:
    """One decision line in the exact emitted writer shape (status, reason,
    ids/all, sid, moment -- the hook writer's field order)."""
    ids = list(ids)
    return (f"[{ts}] zmem-hook status=silent reason={reason}"
            f" ids={ids} all={ids} sid={sid} moment=user_prompt")


class ZeroExitTransportWindowTest(unittest.TestCase):
    """AC4: envelope-error windows are excluded from the retrieval-miss
    count like store-error; genuine empty-pool misses still count."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-ac435-window-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.now = int(time.time())
        _seed_store(self._tmp)
        self.db = os.path.join(self._tmp, "db.sqlite")

    def _fixture_db(self, failures: list) -> None:
        _make_fixture_db(self.db, failures)

    def _bg_log(self, lines: list) -> None:
        Path(os.path.join(self._tmp, "zmem-decisions.log")).write_text(
            "\n".join(lines) + "\n", encoding="utf-8")

    def _run(self) -> dict:
        return miss_rate.run_miss_report(
            store_path=os.path.join(self._tmp, "store.sqlite"),
            db_path=self.db, data_dir=self._tmp)

    def _failure(self, session: str = "sess-a", ts: int = None) -> dict:
        return {"session_id": session,
                "ts_s": self.now if ts is None else ts,
                "call_id": "call-" + session, "tool": "Bash",
                "operation": "git stash pop", "error_message": "conflict"}

    def test_envelope_error_window_is_excluded_like_store_error(self):
        self._fixture_db([self._failure()])
        self._bg_log([_bg_line(self.now, [], "sess-a", "envelope-error")])
        report = self._run()
        self.assertEqual(
            report["counts"]["disabled"], 1,
            "an envelope-error window is a transport failure, excluded from "
            "the retrieval-miss count exactly like store-error")
        self.assertEqual(report["counts"]["missed"], 0)

    def test_store_error_window_exclusion_is_preserved(self):
        # The 0.139.0 behavior this check must not regress: the same window
        # shape under reason=store-error stays excluded.
        self._fixture_db([self._failure()])
        self._bg_log([_bg_line(self.now, [], "sess-a", "store-error")])
        report = self._run()
        self.assertEqual(report["counts"]["disabled"], 1)
        self.assertEqual(report["counts"]["missed"], 0)

    def test_genuine_empty_pool_miss_still_counts_alongside(self):
        # Two sessions in the same report: sess-a's failure sits in an
        # envelope-error window (excluded), sess-b's sits in a genuine
        # empty-pool silent window (a healthy no-match -- a REAL miss).
        self._fixture_db([self._failure("sess-a"),
                          self._failure("sess-b", ts=self.now - 60)])
        self._bg_log([
            _bg_line(self.now, [], "sess-a", "envelope-error"),
            _bg_line(self.now - 60, [], "sess-b", "empty-pool"),
        ])
        report = self._run()
        self.assertEqual(
            report["counts"]["disabled"], 1,
            "only the envelope-error window is excluded")
        self.assertEqual(
            report["counts"]["missed"], 1,
            "the genuine empty-pool miss in the same report still counts")


if __name__ == "__main__":
    unittest.main()
