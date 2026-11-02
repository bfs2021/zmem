"""Issue-tracer bundle 329-340 acceptance checks (red checkpoint, base
a988469): the two DISCRIMINATING store-side guards. Both are RED at base
by design — they turn GREEN only under the #339 consume-lane fix and the
#340 no-fsync park fix. NEW file (both frozen hosts are at/near the
500-line cap; a base-red check appended to
tests/test_p05_subagent_task_bridge.py would break its whole-file
PRESERVING floor).

Checks hosted here (one AC = one check; canonical argv form
``python -X utf8 tests/test_tasktext_consume_guards.py <Class>.<method>``):

- C10 / B17 (#339 AC1) ForeignDrainTest — a prefetch-shaped NON-SubagentStart
  call (moment=subagent, empty query, hermes-compat lane) must NOT consume
  a parked delegation. The consume gate keys ONLY on
  (data_dir, session_id) (inject.py:1061-1068, delivery_ledger.py
  tasktext_path with no namespace), so at base the foreign caller drains
  the stash 3 -> 2 (executed at the anchor head; reproduced by R2 in
  repro/repro_probe_base.py). Canonical data_dir: the selector consumes at
  ``_resolve_store_path().parent`` when data_dir is None.
- C12 / B19 (#340 AC1) ParkFsyncTest — the park write must perform NO
  fsync on the .tasktext sidecar (the budget-kill mechanism pin: the park
  runs FIRST inside the parent's store subprocess and its unconditional
  os.fsync at delivery_ledger.py:908 shares the 8s recall budget). The
  count is deterministic — no timing assertion.
- C15 (#329 supplementary, D6, DISCRIMINATING) OrphanAgingBandTest — the
  #329 fix is an aged-head skip: on consume, leading entries older than
  the 300s band are skipped ONLY while a strictly newer (finite ts) entry
  exists; an exact agent_id match still wins before aging; a sole entry
  is never skipped however old. RED at base (no aging rule: an orphan
  backdated 600s — INSIDE the 6h load-prune window, OUTSIDE the band —
  sits at the FIFO head and the child gets the ORPHAN's text). Edge
  guards in the same class (plan-critic round 1): GREEN at base —
  test_sole_aged_entry_still_served, test_exact_agent_id_wins_over_aging,
  test_equal_timestamps_never_aged, test_malformed_ts_never_aged; RED at
  base with the same band member — test_consecutive_aged_heads_each_
  need_newer (the chained [aged1, aged2, fresh] shape cannot be green
  without the aging rule; honest color reported, no weakened assertion).

Env pinned BEFORE the first storelib import (store-path freeze). Runs
standalone: python -X utf8 tests/test_tasktext_consume_guards.py
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_MODULE_SCRATCH = tempfile.mkdtemp(prefix="zmem-329340-drain-")
os.environ["ZMEM_STORE"] = os.path.join(_MODULE_SCRATCH, "store.sqlite")
os.environ["ZMEM_DATA"] = _MODULE_SCRATCH
os.environ["ZMEM_MODELS_DIR"] = os.path.join(_MODULE_SCRATCH, "missing-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
for _k in ("ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                       / "skills" / "memory" / "scripts"))

from storelib import delivery_ledger as dl  # noqa: E402
from storelib import inject as inj  # noqa: E402
from storelib import schema as schema_mod  # noqa: E402
from storelib.schema import _resolve_store_path  # noqa: E402


def _stash_entries(data_dir: str, sid: str) -> list:
    path = Path(dl.tasktext_path(data_dir, sid))
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw.get("entries", []) if isinstance(raw, dict) else []
    except (OSError, ValueError):
        return []


class ForeignDrainTest(unittest.TestCase):
    """C10 / B17 (#339 AC1): a prefetch-shaped call must NOT drain the
    stash. RED at base (3 -> 2 drain executed end-to-end at the anchor
    head and reproduced at base by R2)."""

    SID = "pin-339-drain"

    def setUp(self):
        # Canonical keying, per R2: with data_dir=None the selector resolves
        # the sidecar directory as the PARENT of the resolved store path.
        self.canonical = str(Path(_resolve_store_path()).parent)
        for text in ("T1-pin339", "T2-pin339", "T3-pin339"):
            dl.park_task_text(self.canonical, self.SID, text)

    def tearDown(self):
        path = dl.tasktext_path(self.canonical, self.SID)
        if path and os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass

    def test_prefetch_shaped_subagent_call_does_not_drain(self):
        before = len(_stash_entries(self.canonical, self.SID))
        self.assertEqual(before, 3, "fixture: three parked entries")
        conn = schema_mod.connect()
        try:
            schema_mod.init_db(conn)
            envelope = inj.select_and_budget_for_injection(
                conn,
                query="",                       # the MCP prefetch strip
                namespace="project:pin339foreign",
                moment="subagent",               # the gate's only key
                session_id=self.SID,
                lane="hermes-compat",            # NOT the SubagentStart lane
                limit=5,
                global_limit=3,
                budget_tokens=1500,
                data_dir=None,                   # canonical resolution
            )
        finally:
            conn.close()
        self.assertIsInstance(envelope, dict,
                              "the foreign call must still get an envelope")
        after = len(_stash_entries(self.canonical, self.SID))
        self.assertEqual(
            after, before,
            "a prefetch-shaped (non-SubagentStart) --moment subagent call "
            "with an empty query must NOT consume the parked delegation "
            "(drained %d -> %d)" % (before, after))


class ParkFsyncTest(unittest.TestCase):
    """C12 / B19 (#340 AC1): the park write must perform NO fsync on the
    .tasktext sidecar — and must still write the file. RED at base
    (_atomic_write_json fsyncs unconditionally, delivery_ledger.py:908).
    Count-based: deterministic, no timing."""

    def setUp(self):
        self.tmp = str(Path(tempfile.mkdtemp(prefix="zmem-340-fsync-")).resolve())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_park_write_performs_no_fsync(self):
        sid = "pin-340-fsync"
        counts = {"n": 0}
        real_fsync = os.fsync

        def counting_fsync(fd):
            counts["n"] += 1
            return real_fsync(fd)

        # delivery_ledger resolves os.fsync at call time through the shared
        # os module — patch it there for the duration of the park call.
        with mock.patch.object(dl.os, "fsync", counting_fsync):
            dl.park_task_text(self.tmp, sid, "fsync budget pin text")
        entries = _stash_entries(self.tmp, sid)
        self.assertEqual(
            [e.get("text") for e in entries], ["fsync budget pin text"],
            "the park must still WRITE the sidecar (a fix that parks "
            "nothing would pass the fsync count vacuously)")
        self.assertEqual(
            counts["n"], 0,
            "the .tasktext park write must perform NO fsync (the park runs "
            "FIRST inside the parent's budgeted store subprocess; got %d "
            "fsync call(s))" % counts["n"])


class OrphanAgingBandTest(unittest.TestCase):
    """C15 (#329 supplementary, D6, DISCRIMINATING): the aged-head skip
    band. On consume, leading entries older than 300s are skipped ONLY
    while a strictly newer (finite ts) entry exists; an exact agent_id
    match still wins before aging; a sole entry is never skipped however
    old. The orphan is backdated 600s via the ``now`` park parameter —
    INSIDE the 6h load-prune window (so the prune cannot remove it) and
    OUTSIDE the 300s band. Deterministic: no wall-clock sleeps."""

    def setUp(self):
        self.tmp = str(Path(tempfile.mkdtemp(prefix="zmem-329-band-")).resolve())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_band_aged_orphan_skipped_for_newer_child(self):
        sid = "pin-329-band"
        orphan = "aged orphan prompt inside window outside band"
        d1 = "fresh delegation prompt for the next child"
        dl.park_task_text(self.tmp, sid, orphan, now=time.time() - 600)
        dl.park_task_text(self.tmp, sid, d1)
        served = dl.consume_task_text(self.tmp, sid)
        self.assertEqual(
            served, d1,
            "a leading orphan older than the 300s aging band must be "
            "skipped once a strictly newer entry exists — the child got "
            "the ORPHAN's text (FIFO head served, no aging rule)")

    def test_sole_aged_entry_still_served(self):
        # PRESERVING-in-file guard (a): aging may skip a leading entry only
        # while a strictly newer entry exists — a sole entry is always
        # served, however old. GREEN at base (FIFO) and after the fix.
        sid = "pin-329-sole"
        sole = "sole aged entry must still be served"
        dl.park_task_text(self.tmp, sid, sole, now=time.time() - 600)
        self.assertEqual(
            dl.consume_task_text(self.tmp, sid), sole,
            "a SOLE entry is never aged out however old — skipping is "
            "armed only by the existence of a strictly newer entry")

    def test_exact_agent_id_wins_over_aging(self):
        # PRESERVING-in-file guard (b): the exact agent_id match outranks
        # BOTH the aging rule and FIFO. The tagged entry is aged AND a
        # newer entry exists, so aging alone would skip it — the matching
        # child must still receive it. GREEN at base (the agent_id branch
        # at delivery_ledger.py:1757-1761 wins before any head logic).
        sid = "pin-329-exact"
        plain = "plain aged head entry without an agent id"
        tagged = "tagged aged entry the exact match must still win"
        fresh = "fresh untagged entry a query-less child would take"
        dl.park_task_text(self.tmp, sid, plain, now=time.time() - 600)
        dl.park_task_text(self.tmp, sid, tagged, agent_id="ag-x",
                          now=time.time() - 600)
        dl.park_task_text(self.tmp, sid, fresh)
        self.assertEqual(
            dl.consume_task_text(self.tmp, sid, agent_id="ag-x"), tagged,
            "an exact agent_id match wins BEFORE the aging rule — the "
            "tagged entry is aged and a newer entry exists, but its "
            "matching child must still be served")

    def test_equal_timestamps_never_aged(self):
        # PRESERVING-in-file guard (c): aging arms only on a STRICTLY newer
        # entry. Two entries share one ts 600s old — no strictly newer ts
        # exists, so the aged-looking FIFO head must still be served.
        # GREEN at base (plain FIFO).
        sid = "pin-329-eqts"
        head = "equal ts head entry never aged out"
        second = "equal ts second entry same age"
        aged = time.time() - 600
        dl.park_task_text(self.tmp, sid, head, now=aged)
        dl.park_task_text(self.tmp, sid, second, now=aged)
        self.assertEqual(
            dl.consume_task_text(self.tmp, sid), head,
            "equal timestamps arm no STRICTLY newer entry — the "
            "aged-looking head must still be served (aging requires a "
            "strictly newer ts)")

    def test_malformed_ts_never_aged(self):
        # PRESERVING-in-file guard (d): an entry whose ts is not a number
        # must NEVER be aged out (conservative serve), even with a fresh
        # strictly-newer entry behind it. The stash is written DIRECTLY —
        # park_task_text cannot produce a malformed ts. GREEN at base: the
        # load prune's except-branch keeps a non-float ts and FIFO serves
        # the head (delivery_ledger.py:1644-1650).
        sid = "pin-329-badts"
        head = "malformed ts head entry served conservatively"
        fresh = "fresh entry after the malformed head"
        path = Path(dl.tasktext_path(self.tmp, sid))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"entries": [
            {"agent_id": "", "text": head, "ts": "not-a-number"},
            {"agent_id": "", "text": fresh, "ts": time.time()},
        ]}), encoding="utf-8")
        self.assertEqual(
            dl.consume_task_text(self.tmp, sid), head,
            "an entry with an invalid ts must NEVER be aged out — serve "
            "the malformed head conservatively even when a strictly newer "
            "entry exists")

    def test_oversized_int_ts_never_wedges(self):
        # PRR-002 (0.138.0 review): an entry whose ts is a JSON integer too
        # large for float() must not raise OverflowError out of
        # consume_task_text — _finite_ts treats it as malformed (live,
        # never aged), so the head is served and the consume path never
        # wedges the session. GREEN both ends (the fix ships in the same
        # commit as the pin; the pre-fix behavior was an OverflowError).
        sid = "pin-329-hugets"
        head = "oversized int ts head served conservatively"
        fresh = "fresh entry after the oversized head"
        path = Path(dl.tasktext_path(self.tmp, sid))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"entries": [
            {"agent_id": "", "text": head, "ts": 10 ** 400},
            {"agent_id": "", "text": fresh, "ts": time.time()},
        ]}), encoding="utf-8")
        self.assertEqual(
            dl.consume_task_text(self.tmp, sid), head,
            "an oversized numeric ts must be treated as malformed (never "
            "aged, never raised) — serve the head conservatively instead "
            "of wedging the session's consume path")

    def test_consecutive_aged_heads_each_need_newer(self):
        # Guard (e), the two-head chain: entries [aged1, aged2, fresh] —
        # EACH aged head is skipped only because the strictly newer fresh
        # entry exists; the child must receive fresh's text and the stash
        # must afterwards contain NEITHER aged head. At base this is RED
        # alongside C15 (no aging rule: FIFO serves aged1 and leaves aged2
        # on disk) — it is the chained form of the same band member.
        sid = "pin-329-chain"
        aged1 = "first aged head entry skipped for the fresh entry"
        aged2 = "second aged head entry skipped for the fresh entry"
        fresh = "fresh tail entry the child must receive"
        aged = time.time() - 600
        dl.park_task_text(self.tmp, sid, aged1, now=aged)
        dl.park_task_text(self.tmp, sid, aged2, now=aged)
        dl.park_task_text(self.tmp, sid, fresh)
        served = dl.consume_task_text(self.tmp, sid)
        self.assertEqual(
            served, fresh,
            "each consecutive aged head is skipped ONLY because a "
            "strictly newer entry exists — the child must receive the "
            "fresh entry (got the aged FIFO head instead)")
        leftover = [e.get("text") for e in _stash_entries(self.tmp, sid)]
        self.assertNotIn(
            aged1, leftover,
            "a skipped aged head must leave the stash (aged1 survived: %r)"
            % leftover)
        self.assertNotIn(
            aged2, leftover,
            "a skipped aged head must leave the stash (aged2 survived: %r)"
            % leftover)


if __name__ == "__main__":
    unittest.main(verbosity=2)
