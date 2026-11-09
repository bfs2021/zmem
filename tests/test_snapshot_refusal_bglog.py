"""Issue #449: snapshot-refresh refusals also land in the decision log.

At base every refusal branch of ``fleet/snapshot.py``'s ``_refresh`` is
observable ONLY as one bounded stderr line -- the decision log
(miss-rate's join input) never records that a refresh was refused, so a
hub outage is indistinguishable from a healthy fleet in every report.
The frozen post-fix contract (trace design call, api-contract-spec.md
sections B and C):

- ``_log_refusal_decision(env, refusal_class)`` appends exactly one line
  ``[<unix-ts>] zmem-hook status=silent reason=<class> ids=[] all=[]
  snapshot_refresh=refused`` to ``<data_dir>/zmem-decisions.log`` (utf-8,
  newline="\\n", rotation-aware, whole body fail-open) next to the
  byte-identical stderr line;
- the shipped reader (``storelib.miss_rate.parse_bg_log`` /
  ``_BG_LINE_RE``) gains ONE terminal optional ``snapshot_refresh=`` group
  so the new line parses WITHOUT breaking any pre-#449 line shape and
  WITHOUT the enrichment-trio veto (no lane=/ver=/t_ms= on the line).

Drives the real ``fleet.snapshot`` module with the same stub-http harness
as tests/test_snapshot_refusal_observability.py, per-test temp data dirs
(env pinned; never touches a real store). Transport fixtures follow the
production routing the observability test pins: a raising transport OR a
production-shape dict ``{"ok": False, "reason": ...}`` classes as
hub-unreachable, a hub-ANSWERED dict (error key, no transport reason)
classes as hub-refused, and a non-int payload_bytes classes as
payload-invalid.

Run: python tests/test_snapshot_refusal_bglog.py
     python tests/test_snapshot_refusal_bglog.py ReaderParityTest
"""

from __future__ import annotations

import atexit
import io
import os
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

# Hermetic store pin BEFORE the isolation guard (and before any storelib
# import): storelib freezes STORE_PATH at first import, and _store_guard
# refuses an ambient run on operator boxes whose real default store exists
# (mirrors tests/test_snapshot_refusal_observability.py).
_MODULE_SCRATCH = tempfile.mkdtemp(prefix="zmem-ac449-bglog-")
os.environ["ZMEM_STORE"] = os.path.join(_MODULE_SCRATCH, "store.sqlite")
os.environ["ZMEM_DATA"] = os.path.join(_MODULE_SCRATCH, "data")
os.makedirs(os.environ["ZMEM_DATA"], exist_ok=True)
atexit.register(lambda: shutil.rmtree(_MODULE_SCRATCH, ignore_errors=True))

import _store_guard  # noqa: F401,E402  (issue #244 isolation guard)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from fleet import snapshot as fsnap  # noqa: E402

# Module-level import of the NEW writer symbol: at the pre-fix base this
# fails with ImportError (the deterministic RED signature under this
# repo's direct-python CI loop -- never a skip).
from fleet.snapshot import _log_refusal_decision  # noqa: E402

from storelib.miss_rate import parse_bg_log  # noqa: E402

DECISION_LOG_NAME = "zmem-decisions.log"
DECISION_LINE_RE = (
    r"\[\d+\] zmem-hook status=silent reason=%s"
    r" ids=\[\] all=\[\] snapshot_refresh=refused")


class _StaticPayloadHttp:
    """Fake hub transport serving a pre-built payload dict verbatim."""

    def __init__(self, payload):
        self._payload = payload

    def snapshot(self, deadline_ms=None):
        return self._payload


class _RaisingHttp:
    """Hub transport whose snapshot call raises (hub-unreachable)."""

    def snapshot(self, deadline_ms=None):
        raise RuntimeError("hub socket refused")


def _ok_payload(rows: list, **overrides) -> dict:
    payload = {
        "ok": True, "rows": rows, "payload_bytes": 512,
        "principal": "ac449-principal", "snapshot_revision": 7,
    }
    payload.update(overrides)
    return payload


class _BgLogBase(unittest.TestCase):
    """Per-test scratch: env pinned into a fresh temp dir so the decision
    writer can never touch the real ~/.zmem (ZMEM_STORE wins the data-dir
    derivation, so the log lands at <tmp>/zmem-decisions.log)."""

    def _bind_scratch(self):
        directory = tempfile.TemporaryDirectory(prefix="zmem-ac449-bg-")
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)
        self.env = {
            "ZMEM_STORE": str(self.tmp / "store.sqlite"),
            "ZMEM_DATA": str(self.tmp / "data"),
        }
        self.log_path = self.tmp / DECISION_LOG_NAME

    def setUp(self):
        self._bind_scratch()

    def _decision_lines(self) -> list:
        if not self.log_path.exists():
            return []
        text = self.log_path.read_text(encoding="utf-8")
        return [ln for ln in text.splitlines() if ln.strip()]

    def _refresh_capturing(self, http):
        """Run _refresh with stderr captured; return (accepted, stderr)."""
        buf = io.StringIO()
        with redirect_stderr(buf):
            accepted = fsnap._refresh(http, self.env, None)
        return accepted, buf.getvalue()

    def _refuse_and_read_line(self, http) -> str:
        self.assertFalse(
            self._decision_lines(),
            "harness precondition: no decision line before the refresh")
        accepted, _stderr = self._refresh_capturing(http)
        self.assertFalse(accepted, "the refused fixture must still refuse")
        lines = self._decision_lines()
        self.assertTrue(
            self.log_path.exists(),
            "a refused refresh must write the decision log "
            "(%s)" % self.log_path)
        self.assertEqual(
            len(lines), 1,
            "the refused refresh must append exactly ONE new decision "
            "line; got %d" % len(lines))
        return lines[0]

    def _assert_decision_line(self, line: str, refusal_class: str):
        pattern = DECISION_LINE_RE % re.escape(refusal_class)
        self.assertIsNotNone(
            re.fullmatch(pattern, line),
            "the decision line must read '[<ts>] zmem-hook status=silent "
            "reason=%s ids=[] all=[] snapshot_refresh=refused'; got %r"
            % (refusal_class, line))

    def _assert_stderr_line(self, stderr: str, refusal_class: str):
        lines = [ln for ln in stderr.splitlines() if ln.strip()]
        self.assertEqual(
            len(lines), 1,
            "the stderr refusal line must STILL appear exactly once "
            "(unchanged #437 contract); got %d lines" % len(lines))
        self.assertIn(refusal_class, lines[0])


class RefreshRefusalBgLogTest(_BgLogBase):
    """AC1: a refused refresh writes one decision line next to the stderr
    line; AC2: every refusal class is named."""

    def test_refused_refresh_writes_decision_line(self):
        http = _StaticPayloadHttp({"ok": False, "reason": "hub-unreachable"})
        accepted, stderr = self._refresh_capturing(http)
        self.assertFalse(accepted)
        self.assertTrue(
            self.log_path.exists(),
            "a refused refresh must write <%s> in the pinned data dir"
            % self.log_path)
        lines = self._decision_lines()
        self.assertEqual(
            len(lines), 1,
            "the refused refresh must gain exactly one new decision-line; "
            "got %d" % len(lines))
        self._assert_decision_line(lines[0], "hub-unreachable")
        # Co-existence with the unchanged #437 stderr contract.
        self._assert_stderr_line(stderr, "hub-unreachable")

    def test_each_refusal_class_writes_the_line(self):
        fixtures = {
            # production transport dict (never raises, transport reason)
            "hub-unreachable": _StaticPayloadHttp(
                {"ok": False, "reason": "hub-unreachable"}),
            # hub ANSWERED without ok=True (error key, no transport reason)
            "hub-refused": _StaticPayloadHttp(
                {"ok": False, "error": "snapshot cap"}),
            # hub-answered nonsense: non-int payload_bytes
            "payload-invalid": _StaticPayloadHttp(
                _ok_payload([], payload_bytes="12x")),
        }
        for refusal_class, http in fixtures.items():
            with self.subTest(refusal_class=refusal_class):
                # a fresh scratch per class so each assertion sees exactly
                # its own line
                self._bind_scratch()
                line = self._refuse_and_read_line(http)
                self._assert_decision_line(line, refusal_class)

    def test_successful_refresh_writes_no_decision_line(self):
        # PR #451 feedback (TF-004): a SUCCESSFUL refresh must not write
        # a phantom refusal line -- a writer that records every outcome
        # would make a healthy fleet indistinguishable from a refusing
        # one in every report.
        self._bind_scratch()
        http = _StaticPayloadHttp(_ok_payload([]))
        accepted, _stderr = self._refresh_capturing(http)
        self.assertTrue(accepted, "precondition: the fixture must accept")
        self.assertFalse(
            self.log_path.exists(),
            "a successful refresh must write NO decision line")

    def test_each_refusal_branch_writes_the_line(self):
        # Critic round 2 (Important 2): ALL SIX refusal branches
        # (snapshot.py:588/:613/:616/:626/:630/:645) each write exactly one
        # decision line. Branches 2/3/4 are covered above; this leg pins
        # branches 1 (raising transport), 5 (oversize payload), and 6
        # (build failure via the id-gate ValueError).
        hostile_row = (
            ["x'-- DROP TABLE memory; --"]
            + [None] * (len(fsnap._SNAPSHOT_COLUMNS.split(",")) - 1))
        fixtures = {
            # branch 1: raising transport -> hub-unreachable
            "hub-unreachable": _RaisingHttp(),
            # branch 5: payload_bytes over the 64 MB cap -> payload-invalid
            "payload-invalid-oversize": _StaticPayloadHttp(_ok_payload(
                [], payload_bytes=fsnap.MAX_SNAPSHOT_BYTES + 1)),
            # branch 6: build failure (id-gate ValueError) -> payload-invalid
            "payload-invalid-build": _StaticPayloadHttp(
                _ok_payload([hostile_row])),
        }
        for label, http in fixtures.items():
            with self.subTest(branch=label):
                self._bind_scratch()
                line = self._refuse_and_read_line(http)
                expected_class = (
                    "hub-unreachable" if label == "hub-unreachable"
                    else "payload-invalid")
                self._assert_decision_line(line, expected_class)

    def test_stderr_failure_does_not_suppress_decision_line(self):
        # Critic round 2 (Important 1): a closed or RAISING stderr must
        # never suppress the persistent line -- the two writes are
        # failure-independent guards.
        class _FailingStderr:
            def write(self, *_args, **_kwargs):
                raise OSError("stderr is closed")

            def flush(self):
                pass

        saved = sys.stderr
        http = _StaticPayloadHttp({"ok": False, "reason": "hub-unreachable"})
        try:
            sys.stderr = _FailingStderr()
            accepted = fsnap._refresh(http, self.env, None)
        finally:
            sys.stderr = saved
        self.assertFalse(accepted)
        lines = self._decision_lines()
        self.assertTrue(
            self.log_path.exists() and len(lines) == 1,
            "the decision line must land even when every stderr write "
            "raises (independent guards); got %r" % (lines,))
        self._assert_decision_line(lines[0], "hub-unreachable")

    def test_rotation_is_invoked_with_log_path(self):
        # Implementation-review round 1 (question 3, resolved as a contract
        # pin): rotate-before-append is part of the spec — a writer that
        # never calls rotate_on_append passes the failure-independence
        # leg alone. A RECORDING fake proves the call happens with the
        # log path (presence; the rotate-before-append order is the
        # implementation's, disclosed in 08b round 2).
        import types
        calls = []
        fake = types.ModuleType("storelib.log_rotate")
        fake.rotate_on_append = lambda path: calls.append(str(path))
        saved = sys.modules.get("storelib.log_rotate")
        sys.modules["storelib.log_rotate"] = fake
        try:
            _log_refusal_decision(self.env, "hub-refused")
        finally:
            if saved is None:
                sys.modules.pop("storelib.log_rotate", None)
            else:
                sys.modules["storelib.log_rotate"] = saved
        self.assertEqual(
            calls, [str(self.log_path)],
            "rotate_on_append must be invoked exactly once with the "
            "decision-log path before the append")

    def test_rotation_failure_does_not_suppress_line(self):
        # Plan-critic r1 Important 1 (probe 5.5f): the lazy rotation
        # import/call sits in its OWN guard — a rotation failure must
        # never suppress the append. Inject a failing rotate_on_append
        # into the lazy-import target and require the line to land.
        import types
        fake = types.ModuleType("storelib.log_rotate")
        fake.rotate_on_append = lambda *_a, **_k: (_ for _ in ()).throw(
            RuntimeError("rotation exploded (synthetic)"))
        saved = sys.modules.get("storelib.log_rotate")
        sys.modules["storelib.log_rotate"] = fake
        try:
            _log_refusal_decision(self.env, "hub-refused")
        finally:
            if saved is None:
                sys.modules.pop("storelib.log_rotate", None)
            else:
                sys.modules["storelib.log_rotate"] = saved
        lines = self._decision_lines()
        self.assertTrue(
            self.log_path.exists() and len(lines) == 1,
            "the decision line must land even when rotation raises; got "
            "%r" % (lines,))
        self._assert_decision_line(lines[0], "hub-refused")

    def test_line_is_parseable_by_shipped_reader(self):
        # parse_bg_log takes the ACTIVE log PATH (rotation-aware reader).
        http = _StaticPayloadHttp({"ok": False, "error": "snapshot cap"})
        self._refuse_and_read_line(http)
        rows = parse_bg_log(self.log_path)
        self.assertEqual(
            len(rows), 1,
            "the shipped reader must not skip the writer's decision line")
        row = rows[0]
        self.assertEqual(row["status"], "silent")
        self.assertEqual(row["reason"], "hub-refused")
        self.assertEqual(row["ids"], [])
        self.assertEqual(row["all"], [])
        self.assertEqual(
            row.get("snapshot_refresh"), "refused",
            "the parsed row must carry the additive snapshot_refresh field")


class DirectWriterContractTest(_BgLogBase):
    """Critic round 1 (Important 2 + Important 5): env=None ambient
    resolution, absent data-dir creation, and exact-bytes stderr stability."""

    def test_ambient_env_resolution(self):
        # env=None must resolve through the ambient os.environ (dict copy),
        # never crash on None, and land the line in the ambient-resolved
        # data dir (dirname of the ambient ZMEM_STORE).
        saved = {k: os.environ.get(k) for k in ("ZMEM_STORE", "ZMEM_DATA")}
        try:
            os.environ["ZMEM_STORE"] = str(self.tmp / "ambient.sqlite")
            os.environ["ZMEM_DATA"] = str(self.tmp / "ambient-data")
            _log_refusal_decision(None, "hub-unreachable")
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        log = self.tmp / DECISION_LOG_NAME
        self.assertTrue(
            log.exists(),
            "env=None must resolve through the ambient environment and "
            "write the decision line")
        self._assert_decision_line(
            [ln for ln in log.read_text(encoding="utf-8").splitlines()
             if ln.strip()][-1],
            "hub-unreachable")

    def test_absent_data_dir_is_created(self):
        # The data dir (ZMEM_STORE's parent) does not exist yet: the writer
        # must create it and still land the line (rekey.py precedent).
        env = {
            "ZMEM_STORE": str(self.tmp / "nested" / "deep" / "store.sqlite"),
            "ZMEM_DATA": str(self.tmp / "nested" / "deep"),
        }
        self.assertFalse((self.tmp / "nested").exists())
        _log_refusal_decision(env, "payload-invalid")
        log = self.tmp / "nested" / "deep" / DECISION_LOG_NAME
        self.assertTrue(
            log.exists(),
            "the writer must create a missing data dir before appending")
        self._assert_decision_line(
            [ln for ln in log.read_text(encoding="utf-8").splitlines()
             if ln.strip()][-1],
            "payload-invalid")

    def test_stderr_line_bytes_stable_collapse_and_cap(self):
        # Critic round 1 (Important 5): byte-stability proof for the #437
        # stderr surface this PR must not change: whitespace-collapsed
        # detail, single line, capped at 200 chars.
        buf = io.StringIO()
        with redirect_stderr(buf):
            fsnap._refuse_refresh(
                "hub-refused", "line  one\nline\ttwo ", self.env)
        self.assertEqual(
            buf.getvalue(),
            "[zmem] snapshot refresh refused (hub-refused): line one "
            "line two\n",
            "the stderr line must stay byte-identical: detail "
            "whitespace-collapsed to one line before the cap")
        buf = io.StringIO()
        with redirect_stderr(buf):
            fsnap._refuse_refresh(
                "hub-refused", "x" * 300, self.env)
        expected = (
            "[zmem] snapshot refresh refused (hub-refused): "
            + "x" * 200 + "\n")
        self.assertEqual(
            buf.getvalue(), expected,
            "the stderr detail must be capped at exactly 200 characters "
            "(unchanged from the anchor implementation)")


class ReaderParityTest(_BgLogBase):
    """AC6/AC-B2: reader-side parity for the new terminal field."""

    def _parse_lines(self, *lines) -> list:
        path = self.tmp / "reader-parity.log"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return parse_bg_log(path)

    def test_literal_shape_parses(self):
        rows = self._parse_lines(
            "[1780000000] zmem-hook status=silent reason=hub-refused "
            "ids=[] all=[] snapshot_refresh=refused")
        self.assertEqual(
            len(rows), 1,
            "the exact literal decision line must parse (not be skipped)")
        row = rows[0]
        self.assertEqual(row["ts"], 1780000000)
        self.assertEqual(row["status"], "silent")
        self.assertEqual(row["reason"], "hub-refused")
        self.assertEqual(row["ids"], [])
        self.assertEqual(row["all"], [])
        self.assertEqual(row.get("snapshot_refresh"), "refused")

    def test_writer_emitted_shape_matches_reader(self):
        # Emit a REAL line through the new writer (env pinned to tmp), then
        # feed the writer-emitted line itself to the shipped reader.
        _log_refusal_decision(self.env, "hub-refused")
        lines = self._decision_lines()
        self.assertEqual(
            len(lines), 1,
            "the guarded writer must append exactly one decision line")
        emitted = lines[-1]
        self._assert_decision_line(emitted, "hub-refused")
        rows = self._parse_lines(emitted)
        self.assertEqual(
            len(rows), 1,
            "the exact writer-emitted line shape must parse (not be "
            "skipped) -- got %d rows for %r" % (len(rows), emitted))
        row = rows[0]
        self.assertEqual(row["status"], "silent")
        self.assertEqual(row["reason"], "hub-refused")
        self.assertEqual(row["ids"], [])
        self.assertEqual(row["all"], [])
        self.assertEqual(row.get("snapshot_refresh"), "refused")

    def test_regex_gained_terminal_group(self):
        # Backward compatibility: a pre-#449 line (no snapshot_refresh
        # field) must still parse, with the new field absent/None -- the
        # terminal group is additive only.
        rows = self._parse_lines(
            "[1780000000] zmem-hook status=silent reason=empty-pool "
            "ids=[] all=[]")
        self.assertEqual(
            len(rows), 1,
            "a line WITHOUT the new field must still parse (the terminal "
            "group must stay optional)")
        self.assertIsNone(
            rows[0].get("snapshot_refresh"),
            "the snapshot_refresh field must be absent/None on pre-#449 "
            "line shapes")


if __name__ == "__main__":
    unittest.main()
