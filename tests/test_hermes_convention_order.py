"""Issue #446 supplementary (unfrozen) tests — plan-critic r2 finding 5
+ r2 finding 6:

PruneBeforeWriteOrderCappedTest — an in-process call-order spy proving the
inline prune observes the live session's row as LEGACY '5' BEFORE the
writer's increment (the count==6 pin alone cannot distinguish pre-write
from post-write pruning; a post-write implementation upgrades the row in
the writer first and the pruner never sees a legacy current row).

TriggerNegativeLegTest — PRR-023: the trigger's early return (below the
cap AND at the effective boundary, existing and new key shapes) skips the
classification pass; the equality-boundary legs kill the `<=` -> `<`
mutant (delta-review r1, mutation-proven).

CapBoundaryNewSessionTest — PRR-019: at an exactly-cap population, a NEW
session's first write counts the pending row (effective = cap - 1) and
the post-write total stays at the cap.

CapClampTest — ZMEM_CONVENTION_ROW_CAP parsing/clamp discipline: invalid,
negative, and unparsable values fall back to the default 64; 0 is a
valid aggressive setting; positive integers pass through.

Co-run citizenship (PRR-022, delta-r2): storelib is imported LAZILY per
class (setUpClass), never at module level, so this file's import does
not freeze storelib.schema.STORE_PATH for sibling files collected later
by a whole-suite pytest run; tearDownModule restores the pre-file
sys.modules and environment.

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

_PIN_KEYS = ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODEL_AUTODOWNLOAD",
             "PYTHONIOENCODING", "ZMEM_HOME", "CLAUDE_PLUGIN_DATA",
             "ZCODE_PLUGIN_DATA", "ZCODE_PROJECT_DIR",
             "CLAUDE_PROJECT_DIR", "ZMEM_SESSION", "ZMEM_SESSION_ID",
             "ZMEM_BACKUP_DIR", "ZMEM_CONVENTION_ROW_CAP")
_SAVED_ENV = {k: os.environ.get(k) for k in _PIN_KEYS}
# PRR-022 (delta-r2): snapshot every storelib module present BEFORE this
# file loads any (defensive: a pytest co-run may have loaded them under
# another file's env); restored in tearDownModule after the classes' lazy
# imports purge/reload under this file's pins.
_SAVED_MODULES = {key: value for key, value in list(sys.modules.items())
                  if key == "storelib" or key.startswith("storelib.")}

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


def _restore_ambient() -> None:
    for key in [key for key in list(sys.modules)
                if key == "storelib" or key.startswith("storelib.")]:
        del sys.modules[key]
    sys.modules.update(_SAVED_MODULES)
    for key, value in _SAVED_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


atexit.register(_restore_ambient)


def _import_storelib():
    """Lazy storelib load under THIS file's pins (PRR-022 delta-r2):
    purges any co-run's frozen modules and re-imports fresh so
    storelib.schema.STORE_PATH resolves to this file's scratch. Called
    from each class's setUpClass — never at module level, so pytest
    collection of this file freezes nothing for sibling files."""
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
    for key in [key for key in list(sys.modules)
                if key == "storelib" or key.startswith("storelib.")]:
        del sys.modules[key]
    import storelib.cli as cli_mod
    from storelib.backup import convention_row_cap
    return cli_mod, convention_row_cap


def tearDownModule():  # noqa: N802 - unittest hook
    """Co-run citizenship (PRR-022 delta-r2): when this module's tests
    complete, purge this file's storelib modules and restore whatever
    was present before them — a sibling collected/run after this one
    (alphabetical order: hermes < meta) must see ITS OWN frozen
    STORE_PATH, not this file's scratch."""
    _restore_ambient()


class _OrderBase(unittest.TestCase):
    """Shared lazy-import bootstrap."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cli_mod, cls.convention_row_cap = _import_storelib()

    @classmethod
    def tearDownClass(cls) -> None:
        pass


class PruneBeforeWriteOrderCappedTest(_OrderBase):
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
                rc = self.cli_mod.cmd_hermes_convention(
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


class TriggerNegativeLegTest(_OrderBase):
    """PRR-023: below AND AT the cap with an EXISTING key, the inline
    prune must NOT run — the early return is the hot-path bound, and
    delete-the-gate or `<=` -> `<` mutants previously passed every leg
    (the at-cap leg is the equality boundary that kills the `<`
    mutant; delta-review r1)."""

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
                rc = self.cli_mod.cmd_hermes_convention(
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

    def test_at_cap_existing_key_skips_classification(self):
        """PRR-023 equality boundary (delta-r1): count == effective with
        an EXISTING key must SKIP — a `<=` -> `<` mutant fires here
        (delta-reviewer mutation probe), and only this fixture
        distinguishes the two operators."""
        store = Path(_TMP) / "neg-atcap-store.sqlite"
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

        os.environ["ZMEM_CONVENTION_ROW_CAP"] = "1"
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
                rc = self.cli_mod.cmd_hermes_convention(
                    live_conn,
                    payload={"payload": {"session_id": "order-live"}})
            finally:
                live_conn.close()
        finally:
            backup_mod._prune_convention_meta_conn = real_conn_core
        self.assertEqual(rc, 0, "hermes-convention must exit 0")
        self.assertEqual(
            fired["n"], 0,
            "PRR-023: with count == cap (existing key, in-place "
            "increment), the classification pass must NOT run — a "
            "`<=` -> `<` mutant fires here; the prune core fired "
            f"{fired['n']} time(s)")
        final = sqlite3.connect(store).execute(
            "SELECT value FROM meta WHERE key = "
            "'hermes_convention_count_order-live'").fetchone()[0]
        self.assertIn('"count":6', final,
                      "the writer still increments (5 -> 6) with the "
                      "prune skipped")

    def test_at_effective_new_key_skips_classification(self):
        """PRR-023 equality boundary, NEW-key side (delta-r1): count ==
        effective (cap - 1 for a NEW session key) must SKIP — the
        pending row lands the post-write total exactly at cap. A `<=`
        -> `<` mutant on the effective comparison FIRES here (count ==
        effective fails `<`), so this leg kills it."""
        store = Path(_TMP) / "neg-newkey-store.sqlite"
        if store.exists():
            store.unlink()
        conn = sqlite3.connect(store)
        conn.executescript(
            "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "INSERT INTO meta VALUES ('schema_version', '19');"
            "INSERT INTO meta VALUES "
            "('hermes_convention_count_seed-a', '1');")
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
                rc = self.cli_mod.cmd_hermes_convention(
                    live_conn,
                    payload={"payload": {"session_id": "order-new"}})
            finally:
                live_conn.close()
        finally:
            backup_mod._prune_convention_meta_conn = real_conn_core
        self.assertEqual(rc, 0, "hermes-convention must exit 0")
        self.assertEqual(
            fired["n"], 0,
            "PRR-023: count 1 == effective (cap-1) for a NEW key must "
            "SKIP (post-write total = 2 == cap; the pending row is "
            "counted by the trigger's effective shift); a `<=` -> `<` "
            f"mutant fires here; the prune core fired {fired['n']} "
            f"time(s)")
        rows = sqlite3.connect(store).execute(
            "SELECT key FROM meta WHERE key = "
            "'hermes_convention_count_order-new'").fetchone()
        self.assertIsNotNone(rows,
                             "the new session's row must still be written")


class CapBoundaryNewSessionTest(_OrderBase):
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
        rc = self.cli_mod.cmd_hermes_convention(
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


class CapClampTest(_OrderBase):
    """ZMEM_CONVENTION_ROW_CAP parsing/clamp (r2 finding 6)."""

    def _with(self, raw):
        old = os.environ.get("ZMEM_CONVENTION_ROW_CAP")
        os.environ["ZMEM_CONVENTION_ROW_CAP"] = raw
        try:
            return type(self).convention_row_cap()
        finally:
            if old is None:
                os.environ.pop("ZMEM_CONVENTION_ROW_CAP", None)
            else:
                os.environ["ZMEM_CONVENTION_ROW_CAP"] = old

    def test_default_when_unset(self):
        os.environ.pop("ZMEM_CONVENTION_ROW_CAP", None)
        self.assertEqual(type(self).convention_row_cap(), 64)

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
