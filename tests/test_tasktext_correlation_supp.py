"""Issue #428 Phase 4.2 guardrails (supplementary to tests/test_tasktext_correlation.py).

1. ``CorrelationIdParityTest`` — hooks/lib/zmem-recall-body.py's
   ``_correlation_id`` mirrors storelib.delivery_ledger.normalize_correlation_id
   (the #158 boundary forbids the hook adapter from importing storelib); the
   two must agree on every input or the hook would forward an id the store
   discards (or vice versa).

2. ``RemoteReadFlagCompletenessTest`` — the remote-gate honesty contract
   (storelib/remote_gate.py ``_READ_DEFAULTS`` comment: "a future flag
   missing here reverts to silent-drop") had no enforcing test. Every
   ``recall``/``recent`` argparse dest must be either refused-when-supplied
   (in ``_READ_DEFAULTS[cmd]``) or in the explicit forwarded/meta set below.
   The prefetch route keeps its refusals inline, so its dest set is pinned
   literally: a NEW prefetch flag fails here until a remote disposition
   (forward or refuse by name) is decided. This is the guardrail for the
   #428 defect class (an identity/flag dropped before the seam that would
   use it); its first run found ``recall --state-hint`` silently dropped in
   remote mode.

standalone: python -X utf8 tests/test_tasktext_correlation_supp.py
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Pin a scratch store BEFORE tests/_store_guard and before any storelib
# import (storelib freezes the store path at first import).
_SCRATCH = tempfile.mkdtemp(prefix="zmem-428-supp-")
os.environ["ZMEM_STORE"] = os.path.join(_SCRATCH, "store.sqlite")
os.environ["ZMEM_DATA"] = _SCRATCH
os.environ["ZMEM_MODELS_DIR"] = os.path.join(_SCRATCH, "missing-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
for _k in ("ZMEM_REMOTE_URL", "ZMEM_MCP_URL"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _store_guard  # noqa: E402,F401  (issue #244 isolation guard)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "memory" / "scripts"))

from storelib import cli as _cli  # noqa: E402
from storelib import delivery_ledger as _dl  # noqa: E402
from storelib import remote_gate as _rg  # noqa: E402


def _load_body():
    spec = importlib.util.spec_from_file_location(
        "zmem_recall_body_428supp",
        REPO_ROOT / "hooks" / "lib" / "zmem-recall-body.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Stop(Exception):
    pass


def _subparsers() -> dict:
    """Capture the real store.py parser by intercepting parse_args inside
    cli.main() (the parser is built inline there)."""
    captured: dict = {}
    orig = argparse.ArgumentParser.parse_args

    def _capture(self, *a, **k):
        captured["ap"] = self
        raise _Stop()

    argparse.ArgumentParser.parse_args = _capture
    saved_argv = sys.argv
    sys.argv = ["store.py", "recall", "--query", "x"]
    try:
        _cli.main()
    except _Stop:
        pass
    finally:
        argparse.ArgumentParser.parse_args = orig
        sys.argv = saved_argv
    ap = captured.get("ap")
    if ap is None:
        raise AssertionError("could not capture the store.py parser")
    sub = next(a for a in ap._actions
               if isinstance(a, argparse._SubParsersAction))
    return sub.choices


def _dests(parser) -> set:
    return {a.dest for a in parser._actions}


class CorrelationIdParityTest(unittest.TestCase):
    CORPUS = [
        "ag-1", "  ag-1  ", "", "   ", "\t", "a" * 128, "a" * 129,
        " " + "a" * 128 + " ", "-rf", "--x", " -lead", "ag\n1", "ag\x00",
        "ag\x7f", "ag\x1b[31m", "ünïcødé-id", "id with spaces",
        None, 5, 1.5, True, ["ag-1"], {"id": "ag-1"}, b"ag-1",
    ]

    def test_hook_and_store_normalizers_agree(self):
        body = _load_body()
        for value in self.CORPUS:
            with self.subTest(value=value):
                self.assertEqual(body._correlation_id(value),
                                 _dl.normalize_correlation_id(value))

    def test_bounds_pinned_equal(self):
        body = _load_body()
        self.assertEqual(body._CORRELATION_ID_MAX,
                         _dl.TASK_TEXT_CORRELATION_ID_MAX)

    def test_rule_shape(self):
        n = _dl.normalize_correlation_id
        self.assertEqual(n("  ag-1  "), "ag-1")
        self.assertEqual(n("a" * 128), "a" * 128)
        for bad in ("", "  ", "a" * 129, "-rf", "ag\n1", "ag\x7f", None, 5):
            with self.subTest(bad=bad):
                self.assertEqual(n(bad), "")


class RemoteReadFlagCompletenessTest(unittest.TestCase):
    # Forwarded to the hub read tool (query/namespace/limit) or parser
    # meta / client-side presentation that never needs a hub parameter.
    FORWARDED_OR_META = {
        "recall": {"help", "json", "limit", "namespace", "no_auto_rekey",
                   "query"},
        "recent": {"help", "json", "limit", "namespace", "no_auto_rekey"},
    }
    # Pinned prefetch dest set (remote_gate._route_prefetch forwards
    # query/namespace/session_id/moment/lane/reserve/ops_tokens/
    # consume_tasktext and refuses the rest by name). Update ONLY together
    # with a remote disposition for the new flag.
    PREFETCH_PINNED = None  # filled from the live parser in setUpClass

    @classmethod
    def setUpClass(cls):
        cls.subs = _subparsers()

    def test_recall_recent_dests_all_dispositioned(self):
        for cmd in ("recall", "recent"):
            dests = _dests(self.subs[cmd])
            refused = set(_rg._READ_DEFAULTS[cmd])
            undispositioned = sorted(
                dests - refused - self.FORWARDED_OR_META[cmd])
            with self.subTest(cmd=cmd):
                self.assertEqual(
                    undispositioned, [],
                    "remote %s would SILENTLY DROP these flags: add each to "
                    "remote_gate._READ_DEFAULTS[%r] (refused by name) or "
                    "forward it to the hub" % (cmd, cmd))

    def test_read_defaults_name_real_dests(self):
        # The reverse direction: a stale _READ_DEFAULTS key would make the
        # refusal check read an attribute the parser never sets.
        for cmd in ("recall", "recent"):
            dests = _dests(self.subs[cmd])
            with self.subTest(cmd=cmd):
                self.assertEqual(
                    sorted(set(_rg._READ_DEFAULTS[cmd]) - dests), [])

    def test_428_flags_refused_remotely(self):
        for cmd, flags in (("recall", ("delegation_agent_id",
                                       "consume_agent_id", "state_hint")),
                           ("recent", ("delegation_agent_id",
                                       "consume_agent_id"))):
            for dest in flags:
                with self.subTest(cmd=cmd, dest=dest):
                    self.assertIn(dest, _rg._READ_DEFAULTS[cmd])

    def test_prefetch_dest_set_pinned(self):
        expected = {
            "help", "json", "no_auto_rekey", "query", "namespace",
            "session_id", "moment", "lane", "reserve", "ops_tokens",
            "consume_tasktext", "consume_agent_id", "exclude_ids",
            "principal", "harness", "state_hint", "for_injection",
            "authorized_namespace",
            # --no-bump is an explicit no-op marker (cli.py help: "the
            # selector never bumps retrieval_count", issue #159): dropping
            # it remotely changes nothing, so it needs no hub parameter.
            "no_bump",
        }
        actual = _dests(self.subs["prefetch"])
        self.assertEqual(
            sorted(actual - expected), [],
            "new prefetch flag(s): decide the remote disposition in "
            "remote_gate._route_prefetch (forward or refuse by name), then "
            "add them here")


if __name__ == "__main__":
    names = [a for a in sys.argv[1:] if not a.startswith("-")]
    loader = unittest.TestLoader()
    suite = (unittest.TestSuite(loader.loadTestsFromName(n, sys.modules[__name__])
                                for n in names)
             if names else loader.loadTestsFromModule(sys.modules[__name__]))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print("CHECK-PASS" if result.wasSuccessful() else "CHECK-FAIL")
    sys.exit(0 if result.wasSuccessful() else 1)
