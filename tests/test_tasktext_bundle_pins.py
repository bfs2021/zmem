"""Issue-tracer bundle 329-340 acceptance checks (red checkpoint, base
a988469): selector-level and park-path pins for the delegation-recall
family. NEW files because both frozen hosts are at/near the 500-line cap
(tests/test_subagent_tasktext.py is 491 lines) and because
tests/test_p05_subagent_task_bridge.py must stay WHOLE-FILE GREEN at base
(the B18/B20 PRESERVING floor) — a base-red check appended there would
break that floor.

Checks hosted here (one AC = one check; canonical argv form
``python -X utf8 tests/test_tasktext_bundle_pins.py <Class>.<method>``):

- C1 / B1 (#329 AC1)  OrphanShiftTest — an AGED orphaned head must not
  shift a later child onto the orphan's query (backdated ``ts`` via the
  ``now`` park parameter; never a wall-clock sleep).
- C2 / B5 (#331 AC1)  SiblingDedupPinTest — two bridged calls in one
  session both receive the shared row (pins the 0.98.0 ``delivered_ids``
  carve-out at inject.py:1213-1214).
- C3 / B8 (#333 AC1)  ParkedPrecedenceTest — a subagent-moment call WITH
  an event query uses the event query and consumes nothing.
- C9 / B15 (#337 AC1) BridgedFallbackTest — a bridged zero-hit child must
  fall back to the recent lane (RED at base), CONSUME the park on the way,
  and render the RECENT header (never the query-keyed header).
- C6 / B11 (#334 AC1) TaskTextRedactionBoundaryTest — a token straddling
  the 800-char TASK_TEXT_MAX boundary persists no cleartext fragment
  (redact-first-then-cap, fixed in 0.98.0, pinned here).
- C7 / B12 (#334 AC2) TaskTextRedactionBoundaryTest — a redaction failure
  parks NOTHING (RED at base: the park path fails open and lands the raw
  text, delivery_ledger.py:1694-1698).
- C13 (supplementary #333 claim 3) TaskTextCapOrderPinTest — the parked
  cap applies AFTER redaction (1200-char prompt persists exactly
  TASK_TEXT_MAX redacted chars).
- FallbackCarveoutComposeTest (#331 x #337 compose, NEW-SURFACE) — two
  sequential zero-hit bridged children must EACH receive the shared row.
- PageOnlyPreservationTest (#337 fix boundary, NEW-SURFACE) — a bridged
  child whose ONLY hit is a curated PAGE must receive the page; the
  zero-hit recent fallback must not fire (recent canary NOT delivered).

Every legitimate-child selector call passes ``consume_tasktext=True``
(the #339 SubagentStart consume grant — set ONLY by the real subagent
hook lane; foreign prefetch-shaped calls stay without it). The kwarg does
not exist at base, so every legitimate-child leg here is RED at base with
the honest pre-fix ``TypeError: unexpected keyword argument`` signature
(plan-critic rounds 1-2, CHECK_WRONG wave).

All stores are throwaway temp dirs; env is pinned BEFORE the first
storelib import (store-path freeze). Runs standalone:
python -X utf8 tests/test_tasktext_bundle_pins.py
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# Store-isolation env MUST be pinned before any storelib import anywhere in
# this process (hard assignment — the tests/test_362_inject_wiring.py
# convention). ZMEM_INJECT_FLOOR_LEX=0: the subject is the bridge MECHANICS,
# not relevance-gate calibration (tests/test_p05 convention).
_MODULE_SCRATCH = tempfile.mkdtemp(prefix="zmem-329340-pins-")
os.environ["ZMEM_STORE"] = os.path.join(_MODULE_SCRATCH, "store.sqlite")
os.environ["ZMEM_DATA"] = _MODULE_SCRATCH
os.environ["ZMEM_MODELS_DIR"] = os.path.join(_MODULE_SCRATCH, "missing-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
os.environ["ZMEM_INJECT_FLOOR_LEX"] = "0"
for _k in ("ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                       / "skills" / "memory" / "scripts"))

from storelib import delivery_ledger as dl  # noqa: E402
from storelib import inject as inj  # noqa: E402
from storelib import schema as schema_mod  # noqa: E402


def _seed_rows(rows) -> None:
    """Direct-SQL seed (test_362 pattern; FTS triggers stay in sync).
    rows: (id, ns, content, ts)."""
    conn = schema_mod.connect()
    try:
        schema_mod.init_db(conn)
        conn.executemany(
            "INSERT OR REPLACE INTO memory (id, namespace, type, content, "
            "confidence, signal, source_ref, ingestion_ts) VALUES "
            "(?, ?, 'fact', ?, 0.9, 'test', 'session:pin', ?)", rows)
        conn.commit()
    finally:
        conn.close()


def _stash_entries(data_dir: str, sid: str) -> list:
    path = Path(dl.tasktext_path(data_dir, sid))
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw.get("entries", []) if isinstance(raw, dict) else []
    except (OSError, ValueError):
        return []


def _result_ids(envelope: dict) -> list:
    rows = envelope.get("results") if isinstance(envelope, dict) else None
    if not isinstance(rows, list):
        return []
    return [r.get("id") for r in rows if isinstance(r, dict)]


def _subagent_pull(data_dir: str, ns: str, sid: str, query: str = "") -> dict:
    """One LEGITIMATE SubagentStart-child selector call against the seeded
    store. ``consume_tasktext=True`` is the SubagentStart consume grant
    (the #339 fix surface): only the real subagent hook lane sets it —
    foreign prefetch-shaped calls stay WITHOUT it (ForeignDrainTest in
    tests/test_tasktext_consume_guards.py). The kwarg does not exist at
    base, so every legitimate-child leg is RED at base with the honest
    pre-fix TypeError signature."""
    conn = schema_mod.connect()
    try:
        schema_mod.init_db(conn)
        return inj.select_and_budget_for_injection(
            conn, query=query, namespace=ns, moment="subagent",
            session_id=sid, lane="zcode", limit=5, global_limit=3,
            budget_tokens=1500, data_dir=data_dir,
            consume_tasktext=True)
    finally:
        conn.close()


class OrphanShiftTest(unittest.TestCase):
    """C1 / B1 (#329 AC1): an aged orphaned head must not shift a later
    child. Orphan parked with ``now`` backdated past the 6h window —
    deterministic, no wall-clock sleeps."""

    NS = "project:pin329orphan"
    SID = "pin-329-orphan"
    # Token-disjoint contents so the bridged query can lexically match ONLY
    # its own row under either AND or OR FTS semantics.
    ORPHAN_TEXT = "quixotic barrista fulcrum zebra " + "zmem329orphanOLDqq"
    D1_TEXT = "tulip harbor meridian cascade " + "zmem329orphanD1qq"

    def setUp(self):
        self.tmp = str(Path(tempfile.mkdtemp(prefix="zmem-329-orphan-")).resolve())
        _seed_rows([
            ("orphan-row", self.NS, self.ORPHAN_TEXT,
             "2026-01-01T00:00:01Z"),
            ("d1-row", self.NS, self.D1_TEXT, "2026-01-01T00:00:02Z"),
        ])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_orphan_does_not_shift_later_child(self):
        dl.park_task_text(self.tmp, self.SID, self.ORPHAN_TEXT,
                          now=time.time() - 600)  # PRR-003: inside the 6h window, outside the 300s band — the aging rule must skip it
        dl.park_task_text(self.tmp, self.SID, self.D1_TEXT)
        env = _subagent_pull(self.tmp, self.NS, self.SID)
        ids = _result_ids(env)
        self.assertIn(
            "d1-row", ids,
            "the next prompt-less child must receive D1's text (aged orphan "
            "pruned); got rows %r" % ids)
        self.assertNotIn(
            "orphan-row", ids,
            "the aged orphan must not shift the later child onto the "
            "orphan's query; got rows %r" % ids)


class SiblingDedupPinTest(unittest.TestCase):
    """C2 / B5 (#331 AC1): two bridged calls in one session BOTH receive
    the shared row — pins the 0.98.0 carve-out (``if bridged_query:
    delivered_ids = []``); a sibling-dedup regression starves child two."""

    NS = "project:pin331sib"
    SID = "pin-331-sib"
    SHARED_TEXT = "cobalt veranda lithograph paddock " + "zmem331sharedqq"

    def setUp(self):
        self.tmp = str(Path(tempfile.mkdtemp(prefix="zmem-331-sib-")).resolve())
        _seed_rows([("shared-row", self.NS, self.SHARED_TEXT,
                     "2026-01-02T00:00:01Z")])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_shared_row_reaches_both_bridged_siblings(self):
        for _call in (1, 2):
            dl.park_task_text(self.tmp, self.SID, self.SHARED_TEXT)
            env = _subagent_pull(self.tmp, self.NS, self.SID)
            self.assertIn(
                "shared-row", _result_ids(env),
                "bridged subagent call %d must receive the shared row both "
                "siblings match (no sibling dedup on bridged pulls); rows %r"
                % (_call, _result_ids(env)))


class ParkedPrecedenceTest(unittest.TestCase):
    """C3 / B8 (#333 AC1): an event-supplied prompt wins over a parked
    delegation AND the entry is not consumed (inject.py:1061 gate half)."""

    NS = "project:pin333prec"
    SID = "pin-333-prec"
    PARKED_TEXT = "anchovy lighthouse trellis paprika " + "zmem333parkedqq"
    EVENT_TEXT = "obsidian kettle carousel driftwood " + "zmem333eventqq"

    def setUp(self):
        self.tmp = str(Path(tempfile.mkdtemp(prefix="zmem-333-prec-")).resolve())
        _seed_rows([
            ("parked-row", self.NS, self.PARKED_TEXT, "2026-01-03T00:00:01Z"),
            ("event-row", self.NS, self.EVENT_TEXT, "2026-01-03T00:00:02Z"),
        ])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_event_prompt_overrides_parked_prompt(self):
        dl.park_task_text(self.tmp, self.SID, self.PARKED_TEXT)
        before = len(_stash_entries(self.tmp, self.SID))
        env = _subagent_pull(self.tmp, self.NS, self.SID,
                             query=self.EVENT_TEXT)
        ids = _result_ids(env)
        self.assertIn("event-row", ids,
                      "the event query must drive the pull; rows %r" % ids)
        self.assertNotIn("parked-row", ids,
                         "the parked prompt must NOT override the event "
                         "query; rows %r" % ids)
        after = len(_stash_entries(self.tmp, self.SID))
        self.assertEqual(
            after, before,
            "a subagent-moment call WITH an event query must NOT consume "
            "the parked entry (%d -> %d)" % (before, after))


class BridgedFallbackTest(unittest.TestCase):
    """C9 / B15 (#337 AC1): a bridged child whose task text matches nothing
    must fall back to the recent lane (consume + RECENT header). RED at
    base: the bridged query replaces the recent lane with no fallback."""

    NS = "project:pin337fb"
    SID = "pin-337-fb"
    NOHIT_TEXT = "zxqj wvib kfrm pyxw " + "zmem337nohitqq"

    def setUp(self):
        self.tmp = str(Path(tempfile.mkdtemp(prefix="zmem-337-fb-")).resolve())
        _seed_rows([
            ("fb-r1", self.NS, "recent lane row one pin337 " + "zmem337r1qq",
             "2026-02-01T00:00:01Z"),
            ("fb-r2", self.NS, "recent lane row two pin337 " + "zmem337r2qq",
             "2026-02-01T00:00:02Z"),
            ("fb-r3", self.NS, "recent lane row three pin337 " + "zmem337r3qq",
             "2026-02-01T00:00:03Z"),
        ])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_zero_hit_bridged_child_falls_back_to_recent(self):
        dl.park_task_text(self.tmp, self.SID, self.NOHIT_TEXT)
        before = len(_stash_entries(self.tmp, self.SID))
        env = _subagent_pull(self.tmp, self.NS, self.SID)
        self.assertTrue(
            env.get("results"),
            "a bridged zero-hit child must fall back to the recent lane, "
            "not an empty context (got %d rows, reason=%r)"
            % (len(env.get("results") or []), env.get("reason")))
        after = len(_stash_entries(self.tmp, self.SID))
        self.assertEqual(
            after, before - 1,
            "the fallback must fire only for a GENUINELY bridged child — "
            "the parked entry must be consumed on the way to the recent "
            "lane (%d -> %d; an ordinary query-less recent pull passes "
            "through WITHOUT consuming)" % (before, after))
        rendered = env.get("rendered") or ""
        self.assertIn(
            "Recent memories", rendered,
            "the zero-hit fallback must RESET the effective query so the "
            "recent header renders (keyed on effective_query.strip())")
        self.assertNotIn(
            "Relevant memories", rendered,
            "a query-keyed header must not render for a fallen-back child")


class FallbackCarveoutComposeTest(unittest.TestCase):
    """NEW-SURFACE (#331 x #337 compose): two sequential zero-hit bridged
    children (both granted, disjoint parked prompts) must EACH receive the
    shared seeded recent row — the first child's fallback delivery must not
    suppress the second child's (fallback + carve-out compose)."""

    NS = "project:pin337x331"
    SID = "pin-337x331"
    SHARED = "marigold semaphore gazelle cobble " + "zmem337x331sharedqq"
    MISS_A = "qzwj xkvb frmp " + "zmem337x331missaqq"
    MISS_B = "plwk nzjv tdsg " + "zmem337x331missbqq"

    def setUp(self):
        self.tmp = str(Path(tempfile.mkdtemp(prefix="zmem-337x331-")).resolve())
        _seed_rows([("shared-recent", self.NS, self.SHARED,
                     "2026-03-01T00:00:01Z")])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_two_zero_hit_siblings_both_receive_shared_recent_row(self):
        for call, prompt in ((1, self.MISS_A), (2, self.MISS_B)):
            dl.park_task_text(self.tmp, self.SID, prompt)
            env = _subagent_pull(self.tmp, self.NS, self.SID)
            self.assertIn(
                "shared-recent", _result_ids(env),
                "zero-hit bridged child %d must receive the shared recent "
                "row (fallback + carve-out compose); rows %r"
                % (call, _result_ids(env)))


class PageOnlyPreservationTest(unittest.TestCase):
    """NEW-SURFACE (#337 fix boundary): a bridged child whose ONLY hit is a
    curated PAGE (bullet prose matches the prompt; NO ordinary row matches)
    must receive the PAGE — the zero-hit recent fallback must not fire.
    Hand-committed v2 page (pages.py on-disk contract) + a recent canary a
    wrong fallback would deliver."""

    NS = "project:pin337page"
    SID = "pin-337-page"
    PROMPT = "zephyr tundra compass pin337pagehit"
    SOURCE = "operational baseline row anchoring the curated page fixture"
    CANARY = "papaya hammock lantern unrelated recent fallback canary pin337canaryqq"

    def setUp(self):
        self.tmp = str(Path(tempfile.mkdtemp(prefix="zmem-337-page-")).resolve())
        _seed_rows([
            ("page-src-1", self.NS, self.SOURCE, "2026-04-01T00:00:01Z"),
            ("recent-canary", self.NS, self.CANARY, "2026-04-01T00:00:02Z"),
        ])
        conn = schema_mod.connect()
        try:
            schema_mod.migrate(conn)  # evidence tables are a v14 migration
            conn.execute(
                "INSERT INTO evidence (id, session_id, lane, moment, kind, "
                "ts, hash, excerpt, ref_path, ref_offset) VALUES "
                "('page-ev-1', 'pin337page', 'zcode', 'subagent', "
                "'delegation', '2026-04-01T00:00:00Z', ?, "
                "'fixture evidence pin337 page', 'fixture://pin337', 0)",
                (hashlib.sha256(b"pin337-evidence").hexdigest(),))
            conn.execute("INSERT INTO memory_evidence VALUES "
                         "('page-src-1', 'page-ev-1')")
            conn.commit()
        finally:
            conn.close()
        self._commit_page()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _commit_page(self):
        # Hand-commit the v2 page directly — a refresh-runtime fixture
        # build would drag in the whole adapter machinery for one pin.
        from storelib import pages as pages_mod
        pairs = {"source_ids": ["page-src-1"], "evidence_ids": ["page-ev-1"]}
        digest = hashlib.sha256(pages_mod._json_bytes(pairs)).hexdigest()[:16]
        content = ("# Pin337 Page\n\n<!-- bullet:%s -->\n"
                   "- zephyr tundra compass curated guidance pin337pagehit\n"
                   "  source_ids: page-src-1\n  evidence_ids: page-ev-1\n"
                   "<!-- end-bullet:%s -->\n" % (digest, digest))
        meta = {"version_id": "v000001",
                "freshness_watermark": "2026-04-01T00:00:00Z",
                "source_ids": ["page-src-1"], "evidence_ids": ["page-ev-1"],
                "retracted_source_ids": [],
                "page_checksum": hashlib.sha256(
                    content.encode("utf-8")).hexdigest()}
        version = dict(meta, content=content, format_version=2,
                       source_content_hashes={"page-src-1": hashlib.sha256(
                           self.SOURCE.encode("utf-8")).hexdigest()},
                       bullet_sources={digest: pairs})
        page_dir = Path(self.tmp) / "pages" / "pin337page"
        (page_dir / "versions").mkdir(parents=True)
        (page_dir / "definition.json").write_text(json.dumps({
            "namespace": self.NS, "query": "", "tags": ["pin337page"],
            "generator_revision": 1, "creation_policy": "manual"}),
            encoding="utf-8")
        (page_dir / "current.json").write_text(json.dumps(meta),
                                               encoding="utf-8")
        (page_dir / "versions" / "v000001.json").write_text(
            json.dumps(version), encoding="utf-8")

    def test_page_only_hit_not_replaced_by_recent(self):
        from storelib import pages as pages_mod
        conn = schema_mod.connect()
        try:
            found = pages_mod._page_candidates_for_selector(
                conn, data_dir=self.tmp, query=self.PROMPT,
                namespace=self.NS, moment="subagent", lane="zcode")
        finally:
            conn.close()
        self.assertEqual([c.get("id") for c in found],
                         ["page:pin337page:v000001"],
                         "fixture sanity: the page must be selector-eligible")
        dl.park_task_text(self.tmp, self.SID, self.PROMPT)
        env = _subagent_pull(self.tmp, self.NS, self.SID)
        ids = _result_ids(env)
        self.assertTrue(
            any(i and i.startswith("page:pin337page:") for i in ids),
            "a page-only bridged hit must deliver the PAGE (ids %r)" % ids)
        self.assertNotIn(
            "recent-canary", ids,
            "the recent fallback must NOT fire when a page hit exists "
            "(canary delivered: %r)" % ids)


class TaskTextRedactionBoundaryTest(unittest.TestCase):
    """C6 / B11 (#334 AC1) and C7 / B12 (#334 AC2): no cleartext secret
    across the 800-char boundary; a redaction FAILURE parks nothing."""

    def setUp(self):
        self.tmp = str(Path(tempfile.mkdtemp(prefix="zmem-334-bound-")).resolve())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_straddling_token_not_persisted(self):
        # ghp_ + 40 alnum = 44 chars (the redaction pattern needs {36,}
        # after the prefix); a space-delimited pad of exactly 784 chars puts
        # the token at [784, 828) so it SPANS position 800 (a trailing-space
        # pad keeps the \b word boundary before "ghp_" intact).
        token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0"
        prompt = "filler " * 112 + token + " tail words"
        self.assertGreater(len(prompt), dl.TASK_TEXT_MAX)
        dl.park_task_text(self.tmp, "pin-334-straddle", prompt)
        entries = _stash_entries(self.tmp, "pin-334-straddle")
        self.assertEqual(len(entries), 1, "the park must land one entry")
        parked = entries[0].get("text", "")
        self.assertNotIn(token[:20], parked,
                         "no cleartext PREFIX of a straddling token may "
                         "persist past the 800-char cap")
        self.assertNotIn(token[-20:], parked,
                         "no cleartext SUFFIX of a straddling token may "
                         "persist past the 800-char cap")
        # The 800-char cap may slice the 17-char marker itself; what the AC
        # pins is that the token was redacted WHOLE first (a marker prefix
        # survives the cut, no cleartext fragment does).
        self.assertIn("[REDACTED", parked,
                      "the straddling token must be redacted whole before "
                      "the cap (a marker prefix must survive the 800 cut)")

    def test_redaction_failure_parks_nothing(self):
        # The redactor is imported INSIDE park_task_text
        # (``from storelib.write import redact_text``) — patch the module
        # attribute so the call-time import observes the failure.
        from storelib import write as write_mod
        raw = "raw probe text token=ghp_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4"
        with mock.patch.object(write_mod, "redact_text",
                               side_effect=RuntimeError(
                                   "redactor probe failure")):
            dl.park_task_text(self.tmp, "pin-334-failclosed", raw)
        landed = _stash_entries(self.tmp, "pin-334-failclosed")
        self.assertFalse(
            landed,
            "a redaction failure must park NOTHING (fail-closed); the raw "
            "text landed instead: %r"
            % [e.get("text") for e in landed])


class TaskTextCapOrderPinTest(unittest.TestCase):
    """C13 (supplementary, #333 claim 3): the parked-text cap applies AFTER
    redaction — a 1200-char prompt with a secret fully inside the first 800
    chars persists exactly TASK_TEXT_MAX redacted characters."""

    def setUp(self):
        self.tmp = str(Path(tempfile.mkdtemp(prefix="zmem-333-cap-")).resolve())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cap_applies_after_redaction(self):
        token = "ghp_" + "B1c2D3e4F5g6H7i8J9k0L1m2N3o4P5q6R7s8T9u0"
        head = "lead words for the delegation prompt " + token + " tail"
        prompt = head + (" trailing pad prose " * 80)
        self.assertGreater(len(prompt), 1200)
        dl.park_task_text(self.tmp, "pin-333-cap", prompt)
        entries = _stash_entries(self.tmp, "pin-333-cap")
        self.assertEqual(len(entries), 1, "the park must land one entry")
        parked = entries[0].get("text", "")
        self.assertEqual(
            len(parked), dl.TASK_TEXT_MAX,
            "the store must cap at TASK_TEXT_MAX AFTER redaction (got %d)"
            % len(parked))
        self.assertNotIn(token, parked, "no cleartext secret may persist")
        self.assertIn("[REDACTED_SECRET]", parked,
                      "redaction must run before the cap")


if __name__ == "__main__":
    unittest.main(verbosity=2)
