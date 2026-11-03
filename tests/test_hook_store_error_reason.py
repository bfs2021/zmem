"""reason=store-error on a failed store child (PR #427 closeout D-2b).

Proves the hook body's fallback-reason ladder end to end:
- a store child that EXITS NONZERO (partial upgrade: a pre-0.138 store
  rejects the new hook's --consume-tasktext with argparse exit 2) must not
  masquerade as ``reason=empty-pool`` — the decision line carries the new
  single token ``reason=store-error`` and the child's rc rides one bounded
  stderr line (auditable, fail-open, never a raise);
- a healthy store that simply matches nothing still logs
  ``reason=empty-pool`` (the changed arm guards the unchanged one);
- the new token parses: the exact emitted line feeds
  ``miss_rate.parse_bg_log`` and its ``reason`` field comes back as
  ``store-error`` (decision-log reader parity — the reader's reason slot
  is ``\\S+``, so the single-token contract is what keeps old lines and
  this new value parseable by the same regex).

The store stub is a throwaway python file that exits 2 — no real store is
needed for the failure leg, and every child strips ambient zmem env (the
operator's real store is never touched).

Runs standalone: python tests/test_hook_store_error_reason.py
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
BODY = REPO_ROOT / "hooks" / "lib" / "zmem-recall-body.py"

_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR", "ZMEM_SESSION",
    "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID", "CLAUDE_PLUGIN_DATA",
    "ZCODE_PLUGIN_DATA", "ZMEM_EMBED_PROFILE", "ZMEM_TEST_NOW",
    "ZMEM_AUTO_REKEY",
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


def _decision_lines(tmp: str) -> list:
    path = Path(tmp) / "zmem-decisions.log"
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _last_hook_line(tmp: str) -> str:
    # The log also carries maintenance lines (e.g. lane_health=...); the
    # decision line is the one the zmem-hook writer emits.
    lines = [ln for ln in _decision_lines(tmp) if "zmem-hook status=" in ln]
    assert lines, "no hook decision line written"
    return lines[-1]


def _decisions_path(tmp: str) -> Path:
    return Path(tmp) / "zmem-decisions.log"


class HookStoreErrorReasonTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="zmem-hook-store-error-")
        self.addCleanup(_rmtree, self._tmp)
        self.env = _clean_env(self._tmp)

    def _run_body(self, store_py: Path):
        return subprocess.run(
            [sys.executable, str(BODY), str(store_py), "project:stub",
             "25000", "user_prompt"],
            input=json.dumps({"prompt": "anything", "session_id": "s1"}),
            capture_output=True, text=True, env=self.env, timeout=120,
        )

    def test_failing_store_gets_store_error_not_empty_pool(self):
        stub = Path(self._tmp) / "store_stub.py"
        stub.write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
        r = self._run_body(stub)
        # Fail-open: the hook itself still exits 0 and emits an envelope.
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("store child failed (rc=2)", r.stderr)
        line = _last_hook_line(self._tmp)
        self.assertIn(" reason=store-error", line)
        self.assertNotIn(" reason=empty-pool", line)

    def test_failing_store_line_parses_through_reader(self):
        stub = Path(self._tmp) / "store_stub.py"
        stub.write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
        r = self._run_body(stub)
        self.assertEqual(r.returncode, 0, r.stderr)
        line = _last_hook_line(self._tmp)
        # Reader parity: point parse_bg_log at the EXACT log the writer
        # emitted (it takes the active log PATH and is rotation-aware —
        # never run writer tests without the reader).
        sys.path.insert(0, str(SCRIPTS))
        try:
            from storelib.miss_rate import parse_bg_log
        finally:
            sys.path.pop(0)
        rows = parse_bg_log(_decisions_path(self._tmp))
        self.assertTrue(rows)
        self.assertEqual(rows[-1]["reason"], "store-error")

    def test_healthy_empty_store_keeps_empty_pool(self):
        env = dict(self.env)
        r = subprocess.run(
            [sys.executable, str(BODY),
             str(SCRIPTS / "store.py"), "project:stub", "25000",
             "user_prompt"],
            input=json.dumps({"prompt": "anything", "session_id": "s1"}),
            capture_output=True, text=True, env=env, timeout=120,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("store child failed", r.stderr)
        line = _last_hook_line(self._tmp)
        self.assertIn(" reason=empty-pool", line)
        self.assertNotIn(" reason=store-error", line)


def _rmtree(path: str) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


def main() -> int:
    unittest.main()


if __name__ == "__main__":
    main()
