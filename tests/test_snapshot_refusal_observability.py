"""Issue #437: snapshot-refresh refusal observability + failure backoff.

At base every failure branch of ``fleet/snapshot.py``'s ``_refresh`` returns
False SILENTLY -- no background/decision line names the refusal class or the
offending legacy id, and ``_maybe_refresh`` re-arms the same full attempt on
every live moment. The frozen post-fix contract (trace design calls):

- each refusal class writes ONE bounded stderr line naming the class --
  ``hub-unreachable`` (the snapshot call raised), ``hub-refused`` (non-dict
  payload or ok is not True), ``payload-invalid`` (non-int payload_bytes,
  oversize, or build ValueError/OSError/sqlite) -- while ``_refresh`` still
  returns False (best-effort, never a raise);
- the id-gate ``payload-invalid`` line names the offending ROW INDEX and the
  id value, and the refuse-whole-payload gate itself is unchanged (the
  previously-served good pair survives byte-identical);
- consecutive failing live moments back off to at most one re-fetch attempt
  per REFRESH_INTERVAL_S window while the stamp stays armed.

Drives the real ``fleet.snapshot`` module with stub http objects and per-test
temp data dirs (mirrors tests/test_broker_restore_id_gate.py isolation; env
pinned; never touches a real store).

Run: python tests/test_snapshot_refusal_observability.py
"""

from __future__ import annotations

import atexit
import hashlib
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

# Hermetic store pin BEFORE the isolation guard (and before any storelib
# import): storelib freezes STORE_PATH at first import, and _store_guard
# refuses an ambient run on operator boxes whose real default store exists.
_MODULE_SCRATCH = tempfile.mkdtemp(prefix="zmem-ac437-obs-")
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

GOOD_ID = "0d8e7a5b-1234-4c5d-9e8f-a1b2c3d4e5f7"
HOSTILE_ID = "x'-- DROP TABLE memory; --"
MAX_LINE_CHARS = 400  # one bounded line (broker.py PRR-009 shape)

_COLS = [c.strip() for c in fsnap._SNAPSHOT_COLUMNS.split(",")]


def _schema_row(mid: str, scratch: Path) -> list:
    """Build a payload row satisfying every NOT NULL / CHECK constraint,
    deriving the constraint set from the live schema (probe_p1 lesson:
    no hand-rolled column guesses)."""
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
        "content": "ac437 observability row content", "tags": "[]",
        "source_ref": "ac437",  # NOT NULL
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


class _RaisingHttp:
    """Hub transport whose snapshot call raises (hub-unreachable)."""

    def snapshot(self, deadline_ms=None):
        raise RuntimeError("hub socket refused")


class _StaticPayloadHttp:
    """Fake hub transport serving a pre-built payload dict verbatim."""

    def __init__(self, payload):
        self._payload = payload

    def snapshot(self, deadline_ms=None):
        return self._payload


def _ok_payload(rows: list, **overrides) -> dict:
    payload = {
        "ok": True, "rows": rows, "payload_bytes": 512,
        "principal": "ac437-principal", "snapshot_revision": 7,
    }
    payload.update(overrides)
    return payload


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-ac437-env-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.env = {
            "ZMEM_STORE": os.path.join(self._tmp, "store.sqlite"),
            "ZMEM_DATA": os.path.join(self._tmp, "data"),
        }

    def _refresh_capturing(self, http):
        """Run _refresh with stderr captured; return (accepted, stderr)."""
        buf = io.StringIO()
        with redirect_stderr(buf):
            accepted = fsnap._refresh(http, self.env, None)
        return accepted, buf.getvalue()


class RefreshRefusalObservabilityTest(_Base):
    """AC5: every refusal class writes one bounded stderr line naming the
    class while _refresh still returns False; AC6: the id-gate line names
    the offending row index AND id value, and the previous good pair
    survives the refused refresh."""

    def _assert_refusal_line(self, stderr: str, class_token: str):
        lines = [ln for ln in stderr.splitlines() if ln.strip()]
        self.assertTrue(
            lines, "the refused refresh wrote no stderr line at base "
            f"(expected the {class_token} refusal class)")
        naming = [ln for ln in lines if class_token in ln]
        self.assertEqual(
            len(naming), 1,
            f"expected exactly one bounded stderr line naming "
            f"{class_token}; got {len(naming)} of {len(lines)} line(s)")
        self.assertLessEqual(len(naming[0]), MAX_LINE_CHARS,
                             "the refusal line must stay bounded")

    def test_hub_unreachable_names_class(self):
        accepted, stderr = self._refresh_capturing(_RaisingHttp())
        self.assertFalse(accepted, "a raising hub must still refuse")
        self._assert_refusal_line(stderr, "hub-unreachable")

    def test_non_dict_payload_names_hub_refused(self):
        accepted, stderr = self._refresh_capturing(
            _StaticPayloadHttp([1, 2]))
        self.assertFalse(accepted)
        self._assert_refusal_line(stderr, "hub-refused")

    def test_non_ok_payload_names_hub_refused(self):
        accepted, stderr = self._refresh_capturing(
            _StaticPayloadHttp({"ok": False, "error": "snapshot cap"}))
        self.assertFalse(accepted)
        self._assert_refusal_line(stderr, "hub-refused")

    def test_non_int_payload_bytes_names_payload_invalid(self):
        accepted, stderr = self._refresh_capturing(
            _StaticPayloadHttp(_ok_payload([], payload_bytes="12x")))
        self.assertFalse(accepted)
        self._assert_refusal_line(stderr, "payload-invalid")

    def test_oversize_payload_names_payload_invalid(self):
        accepted, stderr = self._refresh_capturing(
            _StaticPayloadHttp(_ok_payload(
                [], payload_bytes=fsnap.MAX_SNAPSHOT_BYTES + 1)))
        self.assertFalse(accepted)
        self._assert_refusal_line(stderr, "payload-invalid")

    def test_build_value_error_names_payload_invalid(self):
        # A right-arity-defying row raises ValueError inside
        # _build_from_payload -- the designed degrade, classed
        # payload-invalid (never a crash out of _refresh).
        accepted, stderr = self._refresh_capturing(
            _StaticPayloadHttp(_ok_payload(["row"])))
        self.assertFalse(accepted)
        self._assert_refusal_line(stderr, "payload-invalid")

    def test_id_gate_line_names_row_index_and_id(self):
        # AC6: a 25-column row whose id is not UUID-shaped must produce a
        # payload-invalid line naming BOTH the offending row index and the
        # id value (the ValueError's {i} and {mid!r} disclosures).
        hostile_row = [HOSTILE_ID] + [None] * (len(_COLS) - 1)
        accepted, stderr = self._refresh_capturing(
            _StaticPayloadHttp(_ok_payload([hostile_row])))
        self.assertFalse(accepted, "the id-gate refusal must stand")
        self._assert_refusal_line(stderr, "payload-invalid")
        line = [ln for ln in stderr.splitlines()
                if "payload-invalid" in ln][0]
        self.assertIn("row 0", line,
                      "the refusal line must name the offending row index")
        self.assertIn(HOSTILE_ID, line,
                      "the refusal line must name the offending id value")

    def test_refused_refresh_keeps_previous_good_pair(self):
        # AC6, second half: the refuse-whole-payload gate is unchanged --
        # prime a GOOD served pair through the real restore path, then the
        # hostile-id refresh must leave snapshot.sqlite byte-identical.
        primed = fsnap._refresh(
            _StaticPayloadHttp(_ok_payload(
                [_schema_row(GOOD_ID, Path(self._tmp))])), self.env, None)
        self.assertTrue(primed, "harness could not prime a good pair")
        snap, _meta = fsnap._snapshot_paths(self.env)
        before = hashlib.sha256(snap.read_bytes()).hexdigest()
        hostile_row = [HOSTILE_ID] + [None] * (len(_COLS) - 1)
        accepted, stderr = self._refresh_capturing(
            _StaticPayloadHttp(_ok_payload([hostile_row])))
        self.assertFalse(accepted)
        after = hashlib.sha256(snap.read_bytes()).hexdigest()
        self.assertEqual(
            before, after,
            "a refused refresh must leave the served snapshot.sqlite "
            "byte-identical (the whole-payload gate is unchanged)")


class RefreshRefusalBackoffTest(_Base):
    """AC7: consecutive failing live moments trigger at most one re-fetch
    per REFRESH_INTERVAL_S window while the stamp stays armed."""

    class _CountingRaisingHttp:
        def __init__(self):
            self.calls = 0

        def snapshot(self, deadline_ms=None):
            self.calls += 1
            raise RuntimeError("hub still down")

    def test_consecutive_failures_back_off_within_interval(self):
        http = self._CountingRaisingHttp()
        fsnap.write_stamp(self.env)  # arm the reconnect-transition stamp
        live_moments = 3
        for _ in range(live_moments):
            fsnap._maybe_refresh(http, self.env, None)
        self.assertLessEqual(
            http.calls, 1,
            "consecutive failing live moments must trigger at most one "
            "re-fetch per REFRESH_INTERVAL_S window (%d attempts for %d "
            "live moments at base)" % (http.calls, live_moments))
        self.assertTrue(
            fsnap.stamp_present(self.env),
            "the stamp must stay armed while every refresh keeps failing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
