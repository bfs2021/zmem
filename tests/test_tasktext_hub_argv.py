"""PRR-004 fix: hub-side grant leg coverage — drive the REAL MCP prefetch tool
through the ToolManager (the same seam test_mcp_server.py uses) with a
_run_store_async spy, asserting the store argv carries --consume-tasktext
exactly when the caller passes consume_tasktext=True.

Env pinned before any hermes/storelib import. Never touches the real store.
"""
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

_SCRATCH = tempfile.mkdtemp(prefix="zmem-hub-argv-")
os.environ["ZMEM_STORE"] = os.path.join(_SCRATCH, "store.sqlite")
os.environ["ZMEM_DATA"] = os.path.join(_SCRATCH, "data")
os.environ["ZMEM_MODELS_DIR"] = os.path.join(_SCRATCH, "models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
# build_server refuses to start without an auth token (fail-closed); the
# argv contract under test is token-independent, so mint a scratch token file
# in the legacy single-principal shape. No "namespaces" key = UNSCOPED
# principal, so the namespace guard cannot short-circuit before the argv build.
_TOKEN_FILE = os.path.join(_SCRATCH, "tokens.json")
with open(_TOKEN_FILE, "w", encoding="utf-8") as _tf:
    json.dump({"token": "test-token-329"}, _tf)
os.environ.pop("ZMEM_MCP_TOKEN", None)
os.environ["ZMEM_MCP_TOKEN_FILE"] = _TOKEN_FILE

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT / "skills" / "memory" / "scripts"),
           str(REPO_ROOT / "hermes-plugin" / "server")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class HubArgvGrantTest(unittest.TestCase):
    """The hub prefetch argv carries --consume-tasktext exactly when the
    caller grants it (PRR-004: the leg that turns the remote grant into a
    store invocation had zero executing coverage)."""

    def setUp(self):
        import mcp_server
        self.mcp_server = mcp_server
        self.server = mcp_server.build_server(host="127.0.0.1", port=0, use_tls=False)
        self._original = mcp_server._run_store_async

    def tearDown(self):
        self.mcp_server._run_store_async = self._original
        self.mcp_server._store_semaphore = None
        if hasattr(self.mcp_server, "_read_semaphore"):
            self.mcp_server._read_semaphore = None

    def _call(self, name, **args):
        import asyncio
        return asyncio.run(
            self.server._tool_manager.call_tool(name, args, context=None))

    def _argv_for(self, consume_tasktext):
        captured = {}

        async def _spy(argv, **kwargs):
            captured["argv"] = list(argv)
            return {"ok": True, "returncode": 0,
                    "stdout": '{"ok": true, "results": [], "rendered": "", '
                              '"reason": "empty-pool", "candidate_ids": [], '
                              '"count": 0, "tokens_used": 0, "tokens_budget": 1500}',
                    "stderr": ""}

        self.mcp_server._MAX_CONCURRENT_STORE = 1
        self.mcp_server._store_semaphore = None
        if hasattr(self.mcp_server, "_read_semaphore"):
            self.mcp_server._read_semaphore = None
        self.mcp_server._run_store_async = _spy
        kwargs = dict(query="", namespace=f"user:test-{uuid.uuid4().hex[:8]}",
                      session_id="s-hub-" + uuid.uuid4().hex[:8],
                      moment="subagent")
        if consume_tasktext:
            kwargs["consume_tasktext"] = True
        result = self._call("prefetch", **kwargs)
        return captured.get("argv", []), result

    def test_grant_true_appends_flag(self):
        argv, result = self._argv_for(True)
        self.assertTrue(argv, "prefetch must invoke the store subprocess")
        self.assertIn("--consume-tasktext", argv,
                      "granted hub prefetch must append --consume-tasktext "
                      "to the store argv; got %r" % (argv,))
        self.assertIn("error", result) if isinstance(result, dict) and result.get("error") else None

    def test_grant_false_omits_flag(self):
        argv, _ = self._argv_for(False)
        self.assertTrue(argv, "prefetch must invoke the store subprocess")
        self.assertNotIn("--consume-tasktext", argv,
                         "ungranted hub prefetch must not name the flag; got %r" % (argv,))

    def test_grant_defaults_absent(self):
        argv, _ = self._argv_for(False)
        # The param must be optional with a False default: the no-kwarg call
        # above already exercises the absent-kwarg shape via _argv_for(False).
        self.assertNotIn("--consume-tasktext", argv)


if __name__ == "__main__":
    unittest.main(verbosity=2)
