"""Issue #442 AC5 — per-surface expected-token matrix (normative table).

Drives the three store-child consumers across the #442 transport-reason
table and asserts the expected ``reason=`` token per (surface, trigger)
cell. One generated test method per cell, so every cell is a DISTINCT leg
(the MCP twin's malformed-JSON / valid-non-dict / empty-stdout flows are
separate code paths and separate legs).

| trigger                         | session-start  | provider       | MCP twin          |
|---------------------------------|----------------|----------------|-------------------|
| exit 0, stdout empty            | envelope-error | envelope-error | envelope-error    |
| exit 0, stdout malformed JSON   | envelope-error | envelope-error | envelope-error    |
| exit 0, stdout valid non-dict   | envelope-error | envelope-error | envelope-error    |
| child times out                 | omitted        | store-error    | deadline-exceeded |
| child exits nonzero             | omitted        | store-error    | store-error       |

Provider timeout (CHECK_WRONG amend, feedback round PRR-013): the
provider's local runner synthesizes ``returncode: 124, ok: False`` for a
timed-out child, so it rides the failed-child arm and logs
``store-error`` — the adjudication named in the CHANGELOG and the PR
body. The MCP twin's timeout cell stays "handled upstream": its caller
deadline layer sits ABOVE the result handling, and the shipped
``deadline-exceeded`` token is pinned unchanged.

The per-surface drivers are imported from the sibling AC1/AC3/AC4 files
(module aliases only — no TestCase class is re-exported into this module's
namespace, so no test is collected twice).

Run: python -X utf8 tests/test_transport_reason_matrix.py
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Issue #244 isolation: pin the store/data to a fresh scratch BEFORE the
# guard import, and drop every ambient key that could resolve ~/.zmem.
_TMP = tempfile.mkdtemp(prefix="zmem-442-matrix-")
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

import test_session_start_envelope_error as _ss  # noqa: E402
import test_hermes_mcp_transport_reason as _mcp  # noqa: E402
import test_provider_store_error as _prov  # noqa: E402

# Restore this module's own scratch pin (the sibling headers re-pinned it
# to their own scratch dirs at import; both are scratch, this keeps the
# matrix's writes under its own _TMP).
os.environ.update({"ZMEM_STORE": os.path.join(_TMP, "store.sqlite"),
                   "ZMEM_DATA": _TMP})

# ---- session-start hook: stub-mode per trigger ----------------------------
SS_CELLS = {
    "zero_exit_empty": (("empty", None), "envelope-error"),
    "zero_exit_malformed": (("malformed", None), "envelope-error"),
    "zero_exit_non_dict": (("nondict-list", None), "envelope-error"),
    "timeout": (("slow", "0.5"), "omitted"),
    "nonzero": (("fail", None), "omitted"),
}

# ---- hermes provider: _run_store result (or raise) per trigger ------------
PROV_CELLS = {
    "zero_exit_empty": ({"ok": True, "stdout": "", "stderr": "",
                         "returncode": 0}, "envelope-error"),
    "zero_exit_malformed": ({"ok": True, "stdout": "not-json{{",
                             "stderr": "", "returncode": 0},
                            "envelope-error"),
    "zero_exit_non_dict": ({"ok": True, "stdout": "[1, 2]", "stderr": "",
                            "returncode": 0}, "envelope-error"),
    "nonzero": ({"ok": False, "stdout": "", "stderr": "boom",
                 "returncode": 1}, "store-error"),
    "runner_exception": (RuntimeError("runner exploded"), "store-error"),
    "timeout_rc124": ({"ok": False, "stdout": "",
                       "stderr": "store.py timed out after 20.0s",
                       "returncode": 124}, "store-error"),
}

# ---- hermes MCP twin: _run_store result per trigger -----------------------
MCP_CELLS = {
    "zero_exit_empty": (_mcp.SHAPE_EMPTY, "envelope-error"),
    "zero_exit_malformed": (_mcp.SHAPE_MALFORMED, "envelope-error"),
    "zero_exit_non_dict_list": (_mcp.SHAPE_NONDICT_LIST, "envelope-error"),
    "zero_exit_non_dict_string": (_mcp.SHAPE_NONDICT_STR, "envelope-error"),
    "timeout_deadline": (_mcp.SHAPE_DEADLINE, "deadline-exceeded"),
    "nonzero": (_mcp.SHAPE_NONZERO, "store-error"),
}


class SessionStartMatrixTest(unittest.TestCase):
    """Session-start hook column."""


class ProviderMatrixTest(unittest.TestCase):
    """Hermes provider column."""


@unittest.skipUnless(_mcp.MCP_AVAILABLE, "mcp package not installed")
class McpTwinMatrixTest(unittest.TestCase):
    """Hermes MCP twin column."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.h = _mcp._McpHarness()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.h.close()


def _make_ss(mode, timeout_s, expected, cell):
    def test(self):
        reason, line = _ss.drive_session_start(self, mode,
                                               timeout_s=timeout_s)
        self.assertEqual(
            reason, expected,
            f"[session-start x {cell}] expected reason={expected}; "
            f"observed reason={reason} line={line!r}")
    return test


def _make_prov(spec, expected, cell):
    def test(self):
        h = _prov.ProviderHarness(self)
        if isinstance(spec, BaseException):
            lines, env = h.drive_raise(spec)
        else:
            lines, env = h.drive_result(spec)
        _prov.assert_provider_reason(self, lines, env, expected,
                                     f"[provider x {cell}]")
    return test


def _make_mcp(shape, expected, cell):
    def test(self):
        _mcp.assert_reason(self, self.h, shape, expected,
                           f"[mcp-twin x {cell}]")
    return test


for _cell, ((_mode, _to), _exp) in SS_CELLS.items():
    setattr(SessionStartMatrixTest, f"test_session_start__{_cell}",
            _make_ss(_mode, _to, _exp, _cell))
for _cell, (_spec, _exp) in PROV_CELLS.items():
    setattr(ProviderMatrixTest, f"test_provider__{_cell}",
            _make_prov(_spec, _exp, _cell))
for _cell, (_shape, _exp) in MCP_CELLS.items():
    setattr(McpTwinMatrixTest, f"test_mcp_twin__{_cell}",
            _make_mcp(_shape, _exp, _cell))


if __name__ == "__main__":
    unittest.main(verbosity=2)
