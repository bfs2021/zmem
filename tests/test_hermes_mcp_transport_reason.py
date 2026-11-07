"""Issue #442 AC3 — hermes MCP twin session-prefetch transport reasons.

The hub's ``session_start`` tool (hermes-plugin/server/mcp_server.py) runs
one ``store.py recent --for-injection --json`` child and writes one
compatibility decision line. Normative transport-reason table (#442):

- child exits NONZERO                       -> ``reason=store-error``
- child exits 0, stdout EMPTY               -> ``reason=envelope-error``
- child exits 0, stdout MALFORMED JSON      -> ``reason=envelope-error``
- child exits 0, stdout VALID JSON NON-DICT -> ``reason=envelope-error``
- caller deadline killed the child          -> ``reason=deadline-exceeded``
  (unchanged; handled above the result handling)

At base c78a2d5 the nonzero and malformed arms write ``omitted``, the empty
and scalar-non-dict arms fall through the local ladder to ``empty-pool``,
and a list-non-dict stdout is treated as rows (the tool raises and writes
no decision line at all). Each shape is a DISTINCT leg (the malformed /
non-dict / empty flows are distinct code paths, #442 AC3/AC5).

Seam (established pattern, tests/test_passive_prefetch.py and
tests/test_t07_deadline.py): the module-level SYNC ``_run_store`` that
``_run_store_async`` resolves at call time is replaced by a stub returning
the exact ``{ok, stdout, stderr, returncode}`` dict the real helper builds
— so the real async wrapper, pool semaphore, and decision appender all run.
The observable is the decision line the real ``_append_session_decision``
writes to ``zmem-decisions.log`` beside the pinned scratch store.

Run: python -X utf8 tests/test_hermes_mcp_transport_reason.py
"""
from __future__ import annotations

import asyncio
import atexit
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = REPO_ROOT / "hermes-plugin" / "server"

# Issue #244 isolation: pin the store/data to a fresh scratch BEFORE the
# guard import, and drop every ambient key that could resolve ~/.zmem.
_TMP = tempfile.mkdtemp(prefix="zmem-442-mcp-")
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

try:
    import mcp  # noqa: F401,E402
    MCP_AVAILABLE = True
except ImportError:  # pragma: no cover - CI installs requirements.txt
    MCP_AVAILABLE = False

# Ambient knobs the server reads at import into module constants.
_PIN_KEYS = (
    "ZMEM_HOME", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
    "ZMEM_MCP_TOKEN", "ZMEM_MCP_TOKEN_FILE", "ZMEM_MCP_TOKENS_FILE",
    "ZMEM_MCP_DEFAULT_NS", "ZMEM_NAMESPACE", "ZMEM_CROSS_PROJECT",
    "ZMEM_EMBED_PROFILE", "ZMEM_HOST", "ZMEM_MODELS_DIR",
    "ZMEM_MCP_MAX_CONCURRENT", "ZMEM_MCP_MAX_CONCURRENT_READ",
    "ZMEM_MCP_MAX_CONCURRENT_WRITE", "ZMEM_MCP_QUEUE_TIMEOUT_S",
    "ZMEM_REMOTE_URL",
)

_REASON_RE = re.compile(r"\breason=(\S+)")

# The exact result dicts the real ``_run_store`` builds for each arm.
SHAPE_NONZERO = {"ok": False, "stdout": "", "stderr": "store.py: boom",
                 "returncode": 1}
SHAPE_EMPTY = {"ok": True, "stdout": "", "stderr": "", "returncode": 0}
SHAPE_MALFORMED = {"ok": True, "stdout": "this-is-not-json{{{{\n",
                   "stderr": "", "returncode": 0}
SHAPE_NONDICT_LIST = {"ok": True, "stdout": "[1, 2]\n", "stderr": "",
                      "returncode": 0}
SHAPE_NONDICT_STR = {"ok": True, "stdout": "\"a bare json string\"\n",
                     "stderr": "", "returncode": 0}
SHAPE_DEADLINE = {"ok": False, "stdout": "",
                  "stderr": "store.py timed out after 0.5s",
                  "returncode": 124, "deadline_exceeded": True}
SHAPE_HEALTHY_SILENT = {
    "ok": True, "returncode": 0, "stderr": "",
    "stdout": ('{"results": [], "count": 0, "omitted": 0, '
               '"reason": "empty-pool", "candidate_ids": [], '
               '"rendered": "", "context": ""}\n'),
}


class _McpHarness:
    """One pinned import of mcp_server + a real build_server() object."""

    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="zmem-442-mcp-srv-", dir=_TMP)
        self.saved = {k: os.environ.get(k) for k in
                      _PIN_KEYS + ("ZMEM_STORE", "ZMEM_DATA")}
        for key in _PIN_KEYS:
            os.environ.pop(key, None)
        os.environ.update({
            "ZMEM_HOME": str(REPO_ROOT),
            "ZMEM_STORE": os.path.join(self.tmp, "store.sqlite"),
            "ZMEM_DATA": self.tmp,
            "ZMEM_MODELS_DIR": os.path.join(self.tmp, "missing-models"),
            "ZMEM_MCP_TOKEN": "issue-442-check-token",
        })
        for mod in [k for k in list(sys.modules)
                    if k == "mcp_server" or k.startswith("auth")
                    or k.startswith("bind_guard")]:
            del sys.modules[mod]
        sys.path.insert(0, str(SERVER_DIR))
        import mcp_server  # noqa: PLC0415
        self.mod = mcp_server
        self.server = mcp_server.build_server(
            host="127.0.0.1", port=0, use_tls=False)
        self.original_run_store = mcp_server._run_store
        self.log = Path(self.tmp) / "zmem-decisions.log"

    def close(self) -> None:
        self.mod._run_store = self.original_run_store
        try:
            sys.path.remove(str(SERVER_DIR))
        except ValueError:
            pass
        for mod in [k for k in list(sys.modules) if k == "mcp_server"]:
            del sys.modules[mod]
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def drive(self, shape: dict) -> tuple[list[str], object]:
        """Run session_start once with every store child returning *shape*.

        Returns (session_start decision lines written by THIS call, tool
        result or the raised exception).
        """
        if self.log.exists():
            self.log.unlink()
        calls: list[list[str]] = []

        def stub(args, input_text=None, timing=None, timeout=None,
                 _shape=dict(shape)):
            calls.append(list(args))
            if timing is not None:
                timing["t_ms"] = 1
            return dict(_shape)

        self.mod._run_store = stub
        try:
            result: object = asyncio.run(
                self.server._tool_manager.call_tool(
                    "session_start",
                    {"namespace": "user:global", "session_id": "sid-442",
                     "lane": "hermes-compat", "deadline_ms": 5000},
                    context=None))
        except Exception as exc:  # the observable is the decision line
            result = exc
        finally:
            self.mod._run_store = self.original_run_store
        assert calls and calls[0][:1] == ["recent"], (
            f"session_start never reached the store child (stub saw "
            f"{calls!r})")
        lines = []
        if self.log.exists():
            lines = [ln for ln in
                     self.log.read_text(encoding="utf-8").splitlines()
                     if "moment=session_start" in ln]
        return lines, result


def assert_reason(test: unittest.TestCase, harness: _McpHarness,
                  shape: dict, expected: str, label: str) -> None:
    lines, result = harness.drive(shape)
    observed = [(_REASON_RE.search(ln) or [None, None])[1] for ln in lines]
    test.assertEqual(
        observed, [expected],
        f"MCP twin {label}: expected exactly one session_start decision "
        f"line with reason={expected}; observed reasons={observed!r} "
        f"lines={lines!r} tool-result={result!r}")


@unittest.skipUnless(MCP_AVAILABLE, "mcp package not installed")
class HermesMcpTransportReasonTest(unittest.TestCase):
    """AC3: the twin's transport arms carry transport tokens."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.h = _McpHarness()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.h.close()

    def test_nonzero_exit_is_store_error(self):
        assert_reason(self, self.h, SHAPE_NONZERO, "store-error",
                      "nonzero exit")

    def test_empty_stdout_is_envelope_error(self):
        assert_reason(self, self.h, SHAPE_EMPTY, "envelope-error",
                      "zero-exit empty stdout")

    def test_malformed_json_is_envelope_error(self):
        assert_reason(self, self.h, SHAPE_MALFORMED, "envelope-error",
                      "zero-exit malformed JSON")

    def test_valid_non_dict_list_is_envelope_error(self):
        assert_reason(self, self.h, SHAPE_NONDICT_LIST, "envelope-error",
                      "zero-exit valid non-dict JSON (list)")

    def test_valid_non_dict_string_is_envelope_error(self):
        assert_reason(self, self.h, SHAPE_NONDICT_STR, "envelope-error",
                      "zero-exit valid non-dict JSON (string)")


@unittest.skipUnless(MCP_AVAILABLE, "mcp package not installed")
class HermesMcpPreservedArmsTest(unittest.TestCase):
    """Deadline arm unchanged; a healthy silent envelope keeps its reason."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.h = _McpHarness()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.h.close()

    def test_deadline_arm_unchanged(self):
        assert_reason(self, self.h, SHAPE_DEADLINE, "deadline-exceeded",
                      "caller-deadline kill")

    def test_healthy_silent_envelope_control(self):
        assert_reason(self, self.h, SHAPE_HEALTHY_SILENT, "empty-pool",
                      "healthy silent dict envelope (control)")


if __name__ == "__main__":
    if not MCP_AVAILABLE:
        print("SKIP: mcp package absent — tests/test_hermes_mcp_transport_"
              "reason.py requires the 'mcp' package")
    unittest.main(verbosity=2)
