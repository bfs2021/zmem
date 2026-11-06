"""Issue #435 provider-twin regression: the hermes provider's session-start
reason must classify a SUCCEEDED store child whose stdout is not a JSON dict
as the transport token ``envelope-error`` — never ``empty-pool``.

Plan-critic round 3 executed this exact probe against the pre-fix code and
observed the masquerade (decode -> None -> legacy {} -> empty-pool). This
suite pins the fixed classification through the real provider method with
``_run_store`` stubbed, mirroring that probe. The hook-body ladder and the
miss-rate exclusion are pinned separately (frozen checks C1-C4); this file
is the disclosed supplementary class-sweep disposition for the provider
twin.

Run: python tests/test_provider_envelope_error.py
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import importlib.util
import json
import os
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROVIDER = REPO_ROOT / "hermes-plugin" / "__init__.py"


def _load_provider():
    # Same Hermes-ABC stub as tests/test_hermes_prefetch.py: the provider
    # imports agent.memory_provider, which only exists inside a Hermes
    # host process.
    agent = types.ModuleType("agent")
    mp = types.ModuleType("agent.memory_provider")

    class MemoryProvider:
        pass

    mp.MemoryProvider = MemoryProvider
    agent.memory_provider = mp
    sys.modules.setdefault("agent", agent)
    sys.modules.setdefault("agent.memory_provider", mp)
    spec = importlib.util.spec_from_file_location(
        "zmem_provider_envelope_error_probe", PROVIDER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class ProviderEnvelopeErrorTest(unittest.TestCase):
    def setUp(self) -> None:
        # PRR-009e: the provider reads the kill switch from os.environ at
        # call time — an operator shell with ZMEM_INJECT=0 reds the whole
        # suite for an unrelated reason. Pop in-process, restore after.
        self._saved_inject = os.environ.pop("ZMEM_INJECT", None)
        self.addCleanup(self._restore_inject)
        self.mod = _load_provider()

        self.records = []
        self.mod._append_session_decision = lambda **kw: self.records.append(kw)

        class _Dep:
            _namespace = "user:global"
            _session_id = "s1"

            def _remote_active(self):
                return False

        self.dep = _Dep()

    def _restore_inject(self):
        if self._saved_inject is not None:
            os.environ["ZMEM_INJECT"] = self._saved_inject
        else:
            os.environ.pop("ZMEM_INJECT", None)

    def _run(self, stdout: str) -> dict:
        self.mod._run_store = lambda args, timing=None: {
            "ok": True, "stdout": stdout, "stderr": "", "returncode": 0}
        out = self.mod.ZmemMemoryProvider._tool_session_start(
            self.dep, {"namespace": "user:global", "session_id": "s1"})
        return json.loads(out)

    def test_non_json_zero_exit_stdout_is_transport_classified(self):
        out = self._run("warning: not json")
        self.assertEqual(out.get("reason"), "envelope-error")
        self.assertNotEqual(out.get("reason"), "empty-pool")

    def test_non_dict_json_stdout_is_transport_classified(self):
        out = self._run("[1, 2]")
        self.assertEqual(out.get("reason"), "envelope-error")

    def test_decision_line_carries_the_transport_token(self):
        self._run("warning: not json")
        self.assertTrue(self.records)
        reasons = [r.get("reason") for r in self.records]
        self.assertIn("envelope-error", reasons)
        self.assertNotIn("empty-pool", reasons)

    def test_healthy_silent_envelope_stays_empty_pool(self):
        # A well-formed dict envelope that is silent (no rendered text)
        # remains the healthy no-match classification.
        out = self._run(json.dumps({"ok": True, "results": [],
                                    "reason": ""}))
        self.assertEqual(out.get("reason"), "empty-pool")

    def test_empty_stdout_is_transport_classified(self):
        # Twin parity with the hook (implementation-review r1 F4): a
        # succeeded child printing NOTHING is a corrupted channel, not a
        # healthy no-match.
        out = self._run("")
        self.assertEqual(out.get("reason"), "envelope-error")

    def test_remote_mode_silent_envelope_does_not_raise(self):
        # Implementation-review r1 F1: envelope_error must be bound on the
        # REMOTE path too — a silent hub envelope (hub-unreachable, or an
        # ok envelope with no rendered text) must return a JSON envelope,
        # never UnboundLocalError.
        dep = self.dep
        dep._remote_active = lambda: True

        class _Transport:
            def session_start(self, ns, session_id, lane=None, limit=None):
                return {"reason": "hub-unreachable"}

        dep._transport = _Transport()
        orig = self.mod.ZmemMemoryProvider._tool_session_start
        out = json.loads(orig(dep, {"namespace": "user:global",
                                    "session_id": "s1"}))
        self.assertIsInstance(out, dict)
        # PRR-007a: the hub's own reason must survive — an outage is
        # never reported as a healthy empty pool.
        self.assertEqual(out.get("reason"), "hub-unreachable")

    def test_remote_mode_ok_envelope_no_rendered_does_not_raise(self):
        dep = self.dep
        dep._remote_active = lambda: True

        class _Transport:
            def session_start(self, ns, session_id, lane=None, limit=None):
                return {"ok": True, "ids": [], "omitted": 1}

        dep._transport = _Transport()
        orig = self.mod.ZmemMemoryProvider._tool_session_start
        out = json.loads(orig(dep, {"namespace": "user:global",
                                    "session_id": "s1"}))
        self.assertIsInstance(out, dict)
        # The envelope carries omitted=1, a legitimate store-side silent
        # reason: the ladder keeps it (no transport classification).
        self.assertEqual(out.get("reason"), "omitted")


if __name__ == "__main__":
    unittest.main()
