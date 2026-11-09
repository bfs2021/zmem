"""Issue #442 AC4 — hermes provider local path: a FAILED store child is a
transport failure (``reason=store-error``), never a healthy ``empty-pool``.

The provider's ``_tool_session_start`` local branch gates ``envelope_error``
on ``result.get("ok")``, so at base c78a2d5 a failed child — nonzero exit, or
an exception raised by the store runner (``_run_passive_store`` converts it
to an ``ok=False, returncode=1`` result) — leaves the flag False and the
reason ladder folds the child into ``empty-pool``. Normative table (#442):
nonzero -> ``store-error``; zero-exit empty / malformed / non-dict stdout ->
``envelope-error`` (the #441-fixed arm — pinned here so it never regresses).

Seam (established pattern, tests/test_provider_envelope_error.py): the
provider module is loaded with the Hermes ABC stub, the module-level
``_run_store`` is replaced, and ``_append_session_decision`` is captured.
Both the decision-line reason AND the returned envelope's ``reason`` are
asserted.

Run: python -X utf8 tests/test_provider_store_error.py
"""
from __future__ import annotations

import atexit
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROVIDER = REPO_ROOT / "hermes-plugin" / "__init__.py"

# Issue #244 isolation: pin the store/data to a fresh scratch BEFORE the
# guard import, and drop every ambient key that could resolve ~/.zmem.
_TMP = tempfile.mkdtemp(prefix="zmem-442-prov-")
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


def load_provider(name: str = "zmem_provider_store_error_probe"):
    """Load hermes-plugin/__init__.py with the Hermes ABC stub."""
    agent = types.ModuleType("agent")
    mp = types.ModuleType("agent.memory_provider")

    class MemoryProvider:
        pass

    mp.MemoryProvider = MemoryProvider
    agent.memory_provider = mp
    sys.modules.setdefault("agent", agent)
    sys.modules.setdefault("agent.memory_provider", mp)
    spec = importlib.util.spec_from_file_location(name, PROVIDER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Dep:
    """The provider instance attributes the local branch reads."""

    _namespace = "user:global"
    _session_id = "sid-442"

    def _remote_active(self):
        return False


class ProviderHarness:
    def __init__(self, test: unittest.TestCase) -> None:
        saved_inject = os.environ.pop("ZMEM_INJECT", None)

        def _restore():
            if saved_inject is not None:
                os.environ["ZMEM_INJECT"] = saved_inject

        test.addCleanup(_restore)
        self.mod = load_provider()
        self.records: list[dict] = []
        self.mod._append_session_decision = (
            lambda **kw: self.records.append(kw))

    def drive_result(self, result: dict) -> tuple[list, str]:
        self.mod._run_store = (
            lambda args, timing=None, _r=dict(result): dict(_r))
        return self._call()

    def drive_raise(self, exc: BaseException) -> tuple[list, str]:
        def _boom(args, timing=None):
            raise exc

        self.mod._run_store = _boom
        return self._call()

    def _call(self) -> tuple[list, str]:
        self.records.clear()
        out = json.loads(self.mod.ZmemMemoryProvider._tool_session_start(
            _Dep(), {"namespace": "user:global", "session_id": "sid-442"}))
        return [r.get("reason") for r in self.records], out.get("reason")

    def call_full(self, result: dict) -> tuple[list, dict, dict]:
        """Drive a result and return (decision records, full envelope).

        Wave-2 amend (plan-critic r2 finding 4): return the COMPLETE
        captured decision kwargs (ids/all_ids/context included) — a
        reason-only capture cannot prove failed-child ids never leak into
        the decision record.
        """
        self.mod._run_store = (
            lambda args, timing=None, _r=dict(result): dict(_r))
        self.records.clear()
        out = json.loads(self.mod.ZmemMemoryProvider._tool_session_start(
            _Dep(), {"namespace": "user:global", "session_id": "sid-442"}))
        return self.records, [r.get("reason") for r in self.records], out


def assert_provider_reason(test, line_reasons, envelope_reason, expected,
                           label):
    test.assertEqual(
        line_reasons, [expected],
        f"provider {label}: decision line must carry reason={expected}; "
        f"observed decision reasons={line_reasons!r}")
    test.assertEqual(
        envelope_reason, expected,
        f"provider {label}: returned envelope reason must be {expected}; "
        f"observed {envelope_reason!r}")


class ProviderStoreErrorTest(unittest.TestCase):
    """AC4: a failed local store child -> reason=store-error."""

    def setUp(self) -> None:
        self.h = ProviderHarness(self)

    def test_nonzero_rc_is_store_error(self):
        lines, env = self.h.drive_result(
            {"ok": False, "stdout": "", "stderr": "store.py: boom",
             "returncode": 1})
        assert_provider_reason(self, lines, env, "store-error",
                               "nonzero rc (empty stdout)")

    def test_nonzero_rc_with_dict_stdout_is_store_error(self):
        # A failed child's stdout is never trusted as an envelope.
        lines, env = self.h.drive_result(
            {"ok": False, "returncode": 2, "stderr": "",
             "stdout": json.dumps({"results": [], "reason": "empty-pool",
                                   "rendered": ""})})
        assert_provider_reason(self, lines, env, "store-error",
                               "nonzero rc (dict stdout)")

    def test_runner_exception_is_store_error(self):
        lines, env = self.h.drive_raise(RuntimeError("runner exploded"))
        assert_provider_reason(self, lines, env, "store-error",
                               "exception from the store runner")

    def test_runner_oserror_is_store_error(self):
        lines, env = self.h.drive_raise(OSError("spawn failed"))
        assert_provider_reason(self, lines, env, "store-error",
                               "OSError from the store runner")


class ProviderZeroExitGarbageStaysEnvelopeErrorTest(unittest.TestCase):
    """Do-not-regress: the #441 zero-exit garbage arm stays envelope-error."""

    def setUp(self) -> None:
        self.h = ProviderHarness(self)

    def _ok(self, stdout):
        return self.h.drive_result(
            {"ok": True, "stdout": stdout, "stderr": "", "returncode": 0})

    def test_zero_exit_empty_stdout_stays_envelope_error(self):
        lines, env = self._ok("")
        assert_provider_reason(self, lines, env, "envelope-error",
                               "zero-exit empty stdout")

    def test_zero_exit_malformed_stays_envelope_error(self):
        lines, env = self._ok("this-is-not-json{{{{")
        assert_provider_reason(self, lines, env, "envelope-error",
                               "zero-exit malformed JSON")

    def test_zero_exit_non_dict_stays_envelope_error(self):
        lines, env = self._ok("[1, 2]")
        assert_provider_reason(self, lines, env, "envelope-error",
                               "zero-exit valid non-dict JSON")

    def test_healthy_silent_envelope_stays_empty_pool(self):
        lines, env = self._ok(json.dumps(
            {"results": [], "reason": "empty-pool", "rendered": ""}))
        assert_provider_reason(self, lines, env, "empty-pool",
                               "healthy silent dict envelope (control)")


class ProviderFailedChildContentSuppressedTest(unittest.TestCase):
    """CHECK_WRONG amend (plan-critic r1 finding 2): a FAILED child's
    stdout is never INJECTED. At base the failed child's dict stdout is
    parsed into payload, `rendered` wins the ladder, and the failed
    content ships with reason/status injected — the critic's probe
    (ok=False, returncode=1, rendered="FAILED-CHILD-CONTENT") produced
    reason=injected and the bad row's id. The fix must classify
    store-error BEFORE has_rendered/envelope_error AND suppress the
    failed child's payload entirely (silent decision, empty context).
    """

    def setUp(self) -> None:
        self.h = ProviderHarness(self)

    def test_failed_child_rendered_content_not_injected(self):
        records, reasons, out = self.h.call_full({
            "ok": False, "returncode": 1, "stderr": "store.py: boom",
            "stdout": json.dumps({
                "results": [{"id": "bad", "text": "FAILED-CHILD-CONTENT",
                             "confidence": 0.9, "kind": "lesson"}],
                "rendered": "FAILED-CHILD-CONTENT", "reason": "injected",
                "candidate_ids": ["bad"]})})
        self.assertEqual(
            reasons, ["store-error"],
            f"failed child with injectable stdout must classify "
            f"reason=store-error BEFORE has_rendered wins; observed "
            f"decision reasons={reasons!r}")
        self.assertEqual(
            out.get("reason"), "store-error",
            f"returned envelope reason must be store-error; observed "
            f"{out.get('reason')!r}")
        serialized = json.dumps(out)
        self.assertNotIn(
            "FAILED-CHILD-CONTENT", serialized,
            f"a FAILED child's stdout must NEVER be injected — no rendered "
            f"content, no rows, no candidate ids from the failed child. "
            f"Observed envelope: {serialized[:400]!r}")
        # Wave-2 amend (r2 finding 4): the captured decision record must
        # be a SILENT store-error with EMPTY id sets — a partial fix that
        # parses the failed stdout then clears only `rendered` still leaks
        # the failed child's ids into the decision log and fails here.
        # Wave-3 amend (r3 finding N3-4): pin the COMPLETE kwargs shape
        # and the fixed empty/zero values (omitted=0, excluded=0) plus
        # the returned envelope's empty context/rendered.
        self.assertEqual(len(records), 1,
                         f"exactly one decision record expected; observed "
                         f"{records!r}")
        record = records[0]
        self.assertEqual(
            set(record), {"status", "reason", "ids", "all_ids", "omitted",
                          "excluded", "session_id", "moment", "lane",
                          "t_ms"},
            f"the decision record must carry exactly the "
            f"_append_session_decision kwarg set; observed "
            f"{sorted(record)!r}")
        self.assertEqual(record.get("status"), "silent",
                         f"failed child is a SILENT decision; observed "
                         f"{record!r}")
        self.assertEqual(record.get("ids"), [],
                         f"no failed-child ids on the decision record; "
                         f"observed {record!r}")
        self.assertEqual(record.get("all_ids"), [],
                         f"no failed-child all_ids on the decision record; "
                         f"observed {record!r}")
        self.assertEqual(record.get("omitted"), 0,
                         f"a transport failure reports omitted=0 (no "
                         f"candidates were withheld); observed {record!r}")
        self.assertEqual(record.get("excluded"), 0,
                         f"a transport failure reports excluded=0; "
                         f"observed {record!r}")
        # wave-4 amend (r4 finding N4-2): context and rendered are
        # SEPARATE aliases on the returned envelope — pin each
        # independently (a .get() default chain only checks context).
        self.assertEqual(out.get("context"), "",
                         f"the returned envelope's context must be EMPTY "
                         f"for a failed child; observed "
                         f"{json.dumps(out)[:300]!r}")
        self.assertEqual(out.get("rendered"), "",
                         f"the returned envelope's rendered must be EMPTY "
                         f"for a failed child; observed "
                         f"{json.dumps(out)[:300]!r}")

    def test_failed_child_stdout_never_decoded(self):
        """Wave-2 amend (r2 finding 4): the decoder must not run at all
        for a failed child — `_decode_rendered_envelope` is spied and
        must NOT fire (kills the parse-then-clear mutant)."""
        calls = {"n": 0}
        real_decode = self.h.mod._decode_rendered_envelope

        def _spy(result):
            calls["n"] += 1
            return real_decode(result)

        self.h.mod._decode_rendered_envelope = _spy
        try:
            records, reasons, out = self.h.call_full({
                "ok": False, "returncode": 1, "stderr": "store.py: boom",
                "stdout": json.dumps({
                    "rendered": "FAILED-CHILD-CONTENT",
                    "reason": "injected"})})
        finally:
            self.h.mod._decode_rendered_envelope = real_decode
        self.assertEqual(
            reasons, ["store-error"],
            f"failed child classifies store-error; observed {reasons!r}")
        self.assertEqual(
            calls["n"], 0,
            f"_decode_rendered_envelope must NOT be called for a failed "
            f"child (the stdout is never parsed, never trusted); observed "
            f"{calls['n']} decode call(s)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
