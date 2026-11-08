"""Issue #446 supplementary (unfrozen) tests — plan-critic r2 finding 5
+ r2 finding 6:

PruneBeforeWriteOrderTest — an in-process call-order spy proving the
inline prune observes the live session's row as LEGACY '5' BEFORE the
writer's increment (the count==6 pin alone cannot distinguish pre-write
from post-write pruning; a post-write implementation upgrades the row in
the writer first and the pruner never sees a legacy current row).

CapClampTest — ZMEM_CONVENTION_ROW_CAP parsing/clamp discipline: invalid,
negative, and unparsable values fall back to the default 64; 0 is a
valid aggressive setting; positive integers pass through.

Run: python -X utf8 tests/test_hermes_convention_order.py
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"

_TMP = tempfile.mkdtemp(prefix="zmem-446-order-")
atexit.register(shutil.rmtree, _TMP, True)  # PRR-021-class cleanup
# PRR-022: snapshot EVERY ambient key this module mutates so a
# whole-suite pytest run inherits the operator's environment back
# (storelib freezes STORE_PATH from ZMEM_STORE at the import below, so
# the restore must run at interpreter exit, after any atexit consumers).
_SAVED_ENV = {k: os.environ.get(k) for k in
              ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODEL_AUTODOWNLOAD",
               "PYTHONIOENCODING", "ZMEM_HOME", "CLAUDE_PLUGIN_DATA",
               "ZCODE_PLUGIN_DATA", "ZCODE_PROJECT_DIR",
               "CLAUDE_PROJECT_DIR", "ZMEM_SESSION", "ZMEM_SESSION_ID",
               "ZMEM_BACKUP_DIR", "ZMEM_CONVENTION_ROW_CAP")}


def _restore_env() -> None:
    for _k, _v in _SAVED_ENV.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v


atexit.register(_restore_env)

os.environ.update({
    "ZMEM_STORE": os.path.join(_TMP, "store.sqlite"),
    "ZMEM_DATA": _TMP,
    "ZMEM_MODEL_AUTODOWNLOAD": "0",
    "PYTHONIOENCODING": "utf-8",
})
for _k in ("ZMEM_HOME", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
           "ZCODE_PROJECT_DIR", "CLAUDE_PROJECT_DIR",
           "ZMEM_SESSION", "ZMEM_SESSION_ID", "ZMEM_BACKUP_DIR",
           "ZMEM_CONVENTION_ROW_CAP"):
    os.environ.pop(_k, None)
sys.path.insert(0, str(REPO_ROOT / "tests"))
import _store_guard  # noqa: E402,F401  (issue #244 isolation guard)

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "storelib"))

import storelib.cli as cli_mod  # noqa: E402
from storelib.backup import convention_row_cap  # noqa: E402


class PruneBeforeWriteOrderCappedTest(unittest.TestCase):
    """Same ordering pin with ZMEM_CONVENTION_ROW_CAP=1 so the trigger
    actually fires on the two-row fixture."""

    def test_prune_sees_legacy_live_row_before_increment(self):
        store = Path(_TMP) / "order-store.sqlite"
        if store.exists():
            store.unlink()
        conn = sqlite3.connect(store)
        conn.executescript(
            "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "INSERT INTO meta VALUES ('schema_version', '19');"
            "INSERT INTO meta VALUES "
            "('hermes_convention_count_order-live', '5');"
            "INSERT INTO meta VALUES "
            "('hermes_convention_count_order-stale', '7');")
        conn.commit()
        conn.close()

        os.environ["ZMEM_CONVENTION_ROW_CAP"] = "1"
        self.addCleanup(os.environ.pop, "ZMEM_CONVENTION_ROW_CAP", None)

        observed: dict[str, str] = {}

        import storelib.backup as backup_mod
        real_conn_core = backup_mod._prune_convention_meta_conn

        def _spy_core(conn, **kwargs):
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?",
                ("hermes_convention_count_order-live",)).fetchone()
            observed["live_at_prune"] = row[0] if row else None
            return real_conn_core(conn, **kwargs)

        backup_mod._prune_convention_meta_conn = _spy_core
        try:
            live_conn = sqlite3.connect(store)
            try:
                rc = cli_mod.cmd_hermes_convention(
                    live_conn,
                    payload={"payload": {"session_id": "order-live"}})
            finally:
                live_conn.close()
        finally:
            backup_mod._prune_convention_meta_conn = real_conn_core
        self.assertEqual(rc, 0, "hermes-convention must exit 0")
        self.assertIn(
            "live_at_prune", observed,
            "fixture sanity: the inline prune must have RUN (cap 1 with "
            "2 family rows); the spy never fired")
        self.assertEqual(
            observed["live_at_prune"], "5",
            f"#446 ordering: the pruner must observe the live row as the "
            f"LEGACY bare integer '5' BEFORE the writer's increment (a "
            f"post-write order shows canonical JSON count 5/6); observed "
            f"{observed.get('live_at_prune')!r}")
        final = sqlite3.connect(store).execute(
            "SELECT value FROM meta WHERE key = "
            "'hermes_convention_count_order-live'").fetchone()[0]
        self.assertIn('"count":6', final,
                      f"post-write sanity: the live row ends at canonical "
                      f"count 6; observed {final!r}")


class TriggerNegativeLegTest(unittest.TestCase):
    """PRR-023: below/at the cap with an EXISTING key, the inline prune
    must NOT run — the early return is the hot-path bound, and
    delete-the-gate or `<=` -> `<` mutants previously passed every leg."""

    def test_below_cap_existing_key_skips_classification(self):
        store = Path(_TMP) / "neg-store.sqlite"
        if store.exists():
            store.unlink()
        conn = sqlite3.connect(store)
        conn.executescript(
            "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "INSERT INTO meta VALUES ('schema_version', '19');"
            "INSERT INTO meta VALUES "
            "('hermes_convention_count_order-live', '5');")
        conn.commit()
        conn.close()

        os.environ["ZMEM_CONVENTION_ROW_CAP"] = "2"
        self.addCleanup(os.environ.pop, "ZMEM_CONVENTION_ROW_CAP", None)
        fired: "dict[str, int]" = {"n": 0}

        import storelib.backup as backup_mod
        real_conn_core = backup_mod._prune_convention_meta_conn

        def _spy_core(conn, **kwargs):
            fired["n"] += 1
            return real_conn_core(conn, **kwargs)

        backup_mod._prune_convention_meta_conn = _spy_core
        try:
            live_conn = sqlite3.connect(store)
            try:
                rc = cli_mod.cmd_hermes_convention(
                    live_conn,
                    payload={"payload": {"session_id": "order-live"}})
            finally:
                live_conn.close()
        finally:
            backup_mod._prune_convention_meta_conn = real_conn_core
        self.assertEqual(rc, 0, "hermes-convention must exit 0")
        self.assertEqual(
            fired["n"], 0,
            "PRR-023: with 1 family row <= cap 2 (existing key), the "
            "classification pass must NOT run (the early return bounds "
            "the hot path); the prune core fired "
            f"{fired['n']} time(s)")
        final = sqlite3.connect(store).execute(
            "SELECT value FROM meta WHERE key = "
            "'hermes_convention_count_order-live'").fetchone()[0]
        self.assertIn('"count":6', final,
                      "the writer still increments (5 -> 6) with the "
                      "prune skipped")


class CapBoundaryNewSessionTest(unittest.TestCase):
    """PRR-019: at an exactly-cap population, a NEW session's first
    write must leave the post-write total <= cap (the pending row is
    counted by the trigger and the cap shrinks by one for the prune)."""

    def test_exact_cap_plus_new_session_stays_bounded(self):
        store = Path(_TMP) / "boundary-store.sqlite"
        if store.exists():
            store.unlink()
        conn = sqlite3.connect(store)
        conn.executescript(
            "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "INSERT INTO meta VALUES ('schema_version', '19');")
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        for i in range(64):
            k = f"hermes_convention_count_seed{i:04d}"
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                (k, json.dumps({"count": 1, "last_seen": (
                    now - timedelta(hours=2 * (64 - i))).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")})))
        conn.commit()
        conn.close()

        os.environ.pop("ZMEM_CONVENTION_ROW_CAP", None)  # default cap 64
        rc = cli_mod.cmd_hermes_convention(
            sqlite3.connect(store),
            payload={"payload": {"session_id": "boundary-new"}})
        conn = sqlite3.connect(store)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM meta WHERE substr(key, 1, 22) = "
                "'hermes_convention_coun'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(rc, 0, "hermes-convention must exit 0")
        self.assertLessEqual(
            n, 64,
            f"PRR-019: at exactly-cap 64, a NEW session's first write "
            f"must leave the post-write family total <= 64 (the pending "
            f"row is counted); observed {n} rows")


class CapClampTest(unittest.TestCase):
    """ZMEM_CONVENTION_ROW_CAP parsing/clamp (r2 finding 6)."""

    def _with(self, raw):
        old = os.environ.get("ZMEM_CONVENTION_ROW_CAP")
        os.environ["ZMEM_CONVENTION_ROW_CAP"] = raw
        try:
            return convention_row_cap()
        finally:
            if old is None:
                os.environ.pop("ZMEM_CONVENTION_ROW_CAP", None)
            else:
                os.environ["ZMEM_CONVENTION_ROW_CAP"] = old

    def test_default_when_unset(self):
        os.environ.pop("ZMEM_CONVENTION_ROW_CAP", None)
        self.assertEqual(convention_row_cap(), 64)

    def test_invalid_values_fall_back_to_default(self):
        for raw in ("not-a-number", "-3", "2.5", "", "  "):
            self.assertEqual(self._with(raw), 64, f"raw={raw!r}")

    def test_zero_is_a_valid_aggressive_setting(self):
        self.assertEqual(self._with("0"), 0)

    def test_positive_passthrough(self):
        self.assertEqual(self._with("128"), 128)
        self.assertEqual(self._with("1"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
