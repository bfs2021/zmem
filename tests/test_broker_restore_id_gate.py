"""Frozen acceptance checks C1/C2 (issue #429): the broker-restore path
(``fleet.snapshot._refresh`` -> ``_build_from_payload``) must REFUSE a
hub-served snapshot row whose ``memory.id`` is not UUID-shaped
(``_INGEST_ID_RE`` fullmatch semantics), naming the offending id, BEFORE any
destination write — and a refusal must leave the previously-served snapshot
pair byte-identical.

BrokerRestoreIdGateTest drives the real restore path with a fake hub
transport whose payload carries one hostile id (SQL-metachar carry, and the
36-hex + trailing-newline escape class from pre-#426). At base the defect
accepts both, so the checks fail with the frozen anchors:

    AssertionError: broker-restore accepted the malformed id at base

DestinationUntouchedTest primes a GOOD served pair first, then runs the
hostile refresh and requires the served ``snapshot.sqlite`` sha256 to be
unchanged. At base the hostile refresh replaces the pair:

    AssertionError: destination pair was replaced at base (...)

Payload rows are built schema-derived (``PRAGMA table_info`` on a live
init_db+migrate probe database — the probe_p1 lesson: no hand-rolled column
guesses; ``taint`` must be ``trusted_internal`` (CHECK), ``source_ref`` and
``valid_from`` are NOT NULL).

Run: python tests/test_broker_restore_id_gate.py [TestClass ...]
"""

from __future__ import annotations

import atexit
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

# Hermetic store pin BEFORE the isolation guard (and before any storelib
# import): storelib freezes STORE_PATH at first import, and _store_guard
# refuses an ambient run on operator boxes whose real default store exists.
# The per-test broker scratch is separate (passed via the env dict), but the
# guard and any transitively imported storelib module must never see the
# operator's real store.
_MODULE_SCRATCH = tempfile.mkdtemp(prefix="zmem-broker-id-gate-")
os.environ["ZMEM_STORE"] = os.path.join(_MODULE_SCRATCH, "store.sqlite")
os.environ["ZMEM_DATA"] = os.path.join(_MODULE_SCRATCH, "data")
os.makedirs(os.environ["ZMEM_DATA"], exist_ok=True)
atexit.register(lambda: shutil.rmtree(_MODULE_SCRATCH, ignore_errors=True))

import _store_guard  # noqa: F401,E402  (issue #244 isolation guard)
from pathlib import Path  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from fleet import snapshot as fsnap  # noqa: E402

GOOD_ID = "0d8e7a5b-1234-4c5d-9e8f-a1b2c3d4e5f7"
# Hostile shapes: not UUID-shaped (SQL-metachar carry), and a FULL 36-char
# in-class id + trailing newline — the pre-#426 .match() escape class. The
# id is deliberately 36 in-class chars so LENGTH cannot refuse it: a
# \Z->$ anchor regression in _INGEST_ID_RE would let .match() callers
# accept it (review PRR-002; the regex leg below kills that mutant
# directly).
HOSTILE_ID = "x'-- DROP TABLE memory; --"
HOSTILE_ID_NL = GOOD_ID + "\n"

_COLS = [c.strip() for c in fsnap._SNAPSHOT_COLUMNS.split(",")]


def _schema_row(mid: str, scratch: Path) -> list:
    """Build a 25-column payload row satisfying every NOT NULL / CHECK
    constraint, deriving the constraint set from the live schema."""
    from storelib.schema import init_db, migrate

    probe_db = scratch / "rowprobe.sqlite"
    if probe_db.exists():
        probe_db.unlink()
    conn = sqlite3.connect(str(probe_db))
    try:
        init_db(conn)
        migrate(conn)
        info = {r[1]: (r[3], r[2]) for r in conn.execute("PRAGMA table_info(memory)")}
    finally:
        conn.close()
        probe_db.unlink(missing_ok=True)
    overrides = {
        "id": mid, "namespace": "user:global", "type": "note",
        "content": "acceptance-check row content", "tags": "[]",
        "source_ref": "ac429",  # NOT NULL
        "confidence": 0.5, "signal": "explicit",
        "ingestion_ts": "2026-11-03T00:00:00Z",
        "embedding": None,
        "taint": "trusted_internal",  # CHECK-constrained vocabulary
    }
    row = []
    for name in _COLS:
        if name in overrides:
            row.append(overrides[name])
            continue
        notnull, pytype = info.get(name, (0, "TEXT"))
        if not notnull:
            row.append(None)
        elif pytype and pytype.upper().startswith(
                ("INT", "REAL", "NUM", "FLOA", "DOUB")):
            row.append(0)
        else:
            row.append("")
    return row


class _FakeHttp:
    """Fake hub transport serving one payload row (probe_p1 shape)."""

    def __init__(self, mid: str, scratch: Path):
        self._mid = mid
        self._scratch = scratch

    def snapshot(self, deadline_ms=None):
        return {
            "ok": True,
            "rows": [_schema_row(self._mid, self._scratch)],
            "payload_bytes": 512,
            "principal": "ac-principal",
            "snapshot_revision": 7,
        }


class _StaticPayloadHttp:
    """Fake hub transport serving a pre-built payload dict verbatim."""

    def __init__(self, payload: dict):
        self._payload = payload

    def snapshot(self, deadline_ms=None):
        return self._payload


class BrokerRestoreIdGateTest(unittest.TestCase):
    """C1: a malformed id in the hub payload must be REFUSED with a
    ValueError naming the id (observed through _refresh as False)."""

    _tmpdir: str | None = None
    _env: dict | None = None

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp(prefix="zmem-ac429-gate-")
        cls._env = {
            "ZMEM_STORE": str(Path(cls._tmpdir) / "store.sqlite"),
            "ZMEM_DATA": str(Path(cls._tmpdir) / "data"),
        }

    @classmethod
    def tearDownClass(cls):
        if cls._tmpdir is not None:
            shutil.rmtree(cls._tmpdir, ignore_errors=True)
            cls._tmpdir = None

    def test_sql_metachar_id_is_refused(self):
        http = _FakeHttp(HOSTILE_ID, Path(self._tmpdir))
        accepted = fsnap._refresh(http, self._env, None)
        self.assertFalse(
            accepted, "broker-restore accepted the malformed id at base")
        with self.assertRaises(ValueError) as raised:
            fsnap._build_from_payload(http.snapshot(), self._env)
        self.assertIn(
            repr(HOSTILE_ID), str(raised.exception),
            "the refusal must name the offending id (repr form — the "
            "gate's message embeds {id!r} so hostile bytes stay visible "
            "and single-line)")

    def test_trailing_newline_id_is_refused(self):
        http = _FakeHttp(HOSTILE_ID_NL, Path(self._tmpdir))
        accepted = fsnap._refresh(http, self._env, None)
        self.assertFalse(
            accepted, "broker-restore accepted the malformed id at base")
        with self.assertRaises(ValueError) as raised:
            fsnap._build_from_payload(http.snapshot(), self._env)
        self.assertIn(
            repr(HOSTILE_ID_NL), str(raised.exception),
            "the refusal must name the offending id (repr form — a literal "
            "newline in the id must appear escaped, keeping the ValueError "
            "single-line)")

    def test_ingest_id_regex_anchor_rejects_trailing_newline(self):
        # Review PRR-002: the refusal gate uses .fullmatch, which cannot
        # see a \Z->$ anchor regression at all — the anchor contract lives
        # in the .match() call sites (the sync.py ingest validators). The
        # fixture is a full 36-char in-class id + newline, so only the
        # anchor keeps it out; this leg kills that mutant directly.
        from storelib.sync import _INGEST_ID_RE  # noqa: PLC0415 - lazy

        self.assertIsNone(
            _INGEST_ID_RE.match(HOSTILE_ID_NL),
            "a 36-char in-class id + trailing newline must NOT match via "
            ".match() — a \\Z->$ anchor regression re-opens the pre-#426 "
            "newline-escape class")

    def test_non_sequence_row_degrades_not_crashes(self):
        # Plan-critic round 1 (#1) + round 2 (#1): a hostile payload whose
        # ROWS CONTAINER is not a list/tuple, or whose row is not a sequence
        # (or an empty one), must degrade through ValueError — the ONLY
        # shape _refresh's designed degrade catches at :510-511. An
        # IndexError/TypeError/KeyError would escape _refresh and crash the
        # broker moment (fail-open crash class).
        for bad_rows, bad_row in (
                (None, None), (42, None), ("rows", None),
                ([None], None), ([42], 42), (["row"], "row"),
                ([[]], []), ([()], ())):
            payload = {
                "ok": True, "rows": bad_rows, "payload_bytes": 8,
                "principal": "ac-principal", "snapshot_revision": 7,
            }
            with self.subTest(bad_rows=bad_rows):
                accepted = fsnap._refresh(
                    _StaticPayloadHttp(payload), self._env, None)
                self.assertFalse(
                    accepted,
                    f"broker-restore accepted a malformed rows/row "
                    f"{bad_rows!r} at base")
                with self.assertRaises(ValueError):
                    fsnap._build_from_payload(payload, self._env)

    def test_non_string_id_in_valid_row_is_refused(self):
        # Round 2 (#1): the id TYPE gate (isinstance(row[0], str)) must be
        # pinned separately — a row of the right arity whose id is None or
        # an int must be refused with ValueError (never a TypeError out of
        # fullmatch).
        for bad_id in (None, 42):
            row = _schema_row("0d8e7a5b-1234-4c5d-9e8f-a1b2c3d4e5f7",
                              Path(self._tmpdir))
            row[0] = bad_id
            payload = {
                "ok": True, "rows": [row], "payload_bytes": 512,
                "principal": "ac-principal", "snapshot_revision": 7,
            }
            with self.subTest(bad_id=bad_id):
                accepted = fsnap._refresh(
                    _StaticPayloadHttp(payload), self._env, None)
                self.assertFalse(
                    accepted,
                    f"broker-restore accepted a non-string id {bad_id!r} "
                    f"at base")
                with self.assertRaises(ValueError):
                    fsnap._build_from_payload(payload, self._env)

    def test_oversized_integer_in_valid_row_degrades_not_crashes(self):
        # Round 3 (#1): a right-arity row with a VALID UUID id but an
        # out-of-range integer in an INTEGER column (e.g. applied_count =
        # 1 << 100) raises OverflowError at sqlite bind time — outside
        # _refresh's degrade at :510-511. The gate must translate it into
        # the ValueError degrade so the broker moment never crashes.
        row = _schema_row("0d8e7a5b-1234-4c5d-9e8f-a1b2c3d4e5f7",
                          Path(self._tmpdir))
        row[_COLS.index("applied_count")] = 1 << 100
        payload = {
            "ok": True, "rows": [row], "payload_bytes": 512,
            "principal": "ac-principal", "snapshot_revision": 7,
        }
        accepted = fsnap._refresh(_StaticPayloadHttp(payload), self._env, None)
        self.assertFalse(
            accepted,
            "broker-restore accepted an oversized-integer row at base "
            "(OverflowError escaped the designed degrade)")
        with self.assertRaises(ValueError):
            fsnap._build_from_payload(payload, self._env)

    def test_non_int_scalar_control_fields_degrade_not_crashes(self):
        # Implementation-review round 1: payload_bytes and
        # snapshot_revision are consumed as int() downstream (the latter
        # AFTER build_snapshot has already replaced the served sqlite), so
        # a JSON object in either field raised TypeError outside the
        # ValueError degrade and left a half-updated pair. Both must
        # refuse with ValueError BEFORE any destination write.
        good_row = _schema_row("0d8e7a5b-1234-4c5d-9e8f-a1b2c3d4e5f7",
                               Path(self._tmpdir))
        for field, bad in (("snapshot_revision", {"a": 1}),
                           ("payload_bytes", {}),
                           ("snapshot_revision", []),
                           ("payload_bytes", "12x"),
                           # JSON 1e400 decodes to float inf; int(inf)
                           # overflows - review round 2's escape probe,
                           # landed in round 3 after a failed patch pass.
                           ("snapshot_revision", float("inf")),
                           ("payload_bytes", float("inf"))):
            with self.subTest(field=field, bad=bad):
                payload = {
                    "ok": True, "rows": [good_row], "payload_bytes": 512,
                    "principal": "ac-principal", "snapshot_revision": 7,
                }
                payload[field] = bad
                accepted = fsnap._refresh(
                    _StaticPayloadHttp(payload), self._env, None)
                self.assertFalse(
                    accepted,
                    f"broker-restore accepted a non-int {field} at base "
                    f"(TypeError escaped the designed degrade)")
                with self.assertRaises(ValueError):
                    fsnap._build_from_payload(payload, self._env)


class DestinationUntouchedTest(unittest.TestCase):
    """C2: on a refused (malformed-id) refresh the served snapshot pair must
    stay byte-identical to the pre-refresh good pair."""

    _tmpdir: str | None = None
    _env: dict | None = None

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp(prefix="zmem-ac429-pair-")
        cls._env = {
            "ZMEM_STORE": str(Path(cls._tmpdir) / "store.sqlite"),
            "ZMEM_DATA": str(Path(cls._tmpdir) / "data"),
        }
        # Prime a GOOD served pair through the real restore path.
        primed = fsnap._refresh(
            _FakeHttp(GOOD_ID, Path(cls._tmpdir)), cls._env, None)
        if not primed:
            raise AssertionError(
                "harness could not prime a good served pair at base")

    @classmethod
    def tearDownClass(cls):
        if cls._tmpdir is not None:
            shutil.rmtree(cls._tmpdir, ignore_errors=True)
            cls._tmpdir = None

    @staticmethod
    def _pair_sha(env: dict) -> str:
        snap, _meta = fsnap._snapshot_paths(env)
        return hashlib.sha256(snap.read_bytes()).hexdigest()

    def test_scalar_control_refusal_keeps_served_pair_intact(self):
        # Review round 2: the scalar-control refusal must land BEFORE any
        # destination write — a hostile snapshot_revision leaves the served
        # pair byte-identical (the round-1 half-updated-pair probe, pinned).
        before = self._pair_sha(self._env)
        row = _schema_row(GOOD_ID, Path(self._tmpdir))
        payload = {
            "ok": True, "rows": [row], "payload_bytes": 512,
            "principal": "ac-principal", "snapshot_revision": {"a": 1},
        }
        self.assertFalse(
            fsnap._refresh(_StaticPayloadHttp(payload), self._env, None),
            "broker-restore accepted a hostile snapshot_revision")
        self.assertEqual(
            before, self._pair_sha(self._env),
            "a refused scalar-control refresh must leave the served "
            "snapshot.sqlite byte-identical")

    def test_destination_pair_byte_identical_on_refusal(self):
        before = self._pair_sha(self._env)
        fsnap._refresh(
            _FakeHttp(HOSTILE_ID, Path(self._tmpdir)), self._env, None)
        after = self._pair_sha(self._env)
        self.assertEqual(
            before, after,
            "destination pair was replaced at base (a refused restore must "
            "leave the served snapshot.sqlite byte-identical)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
