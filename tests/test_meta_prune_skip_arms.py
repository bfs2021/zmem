"""Direct-seam skip-contract tests for prune_convention_meta (PR #444
feedback round, delta-r1: the documented "absent / locked / unreadable /
not a database / missing meta table" arms must return None — the skip
shape — in BOTH the dry-run and real-run arms, never a raise that the
caller's blanket handler would mask as a silent skip).

Run: python -X utf8 tests/test_meta_prune_skip_arms.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import atexit
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"

_TMP = tempfile.mkdtemp(prefix="zmem-pruneskip-")
atexit.register(shutil.rmtree, _TMP, True)  # PRR-021: clean the scratch
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
import _store_guard  # noqa: E402,F401  (issue #244 isolation guard)

sys.path.insert(0, str(SCRIPTS))

from storelib.backup import prune_convention_meta  # noqa: E402

CUTOFF = 1_000_000_000.0  # 2001 — prunes everything with last_seen before it
NOW = 1_800_000_000.0


def _seeded_store(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO meta VALUES ('schema_version', '19');"
        "INSERT INTO meta VALUES "
        "('hermes_convention_count_probe', "
        "'{\"count\":1,\"last_seen\":\"2026-01-01T00:00:00Z\"}');")
    conn.commit()
    conn.close()


class PruneSkipArmsTest(unittest.TestCase):
    """Every unopenable-store arm returns None (the skip shape)."""

    def _assert_none_both_arms(self, store, context):
        for dry in (False, True):
            with self.subTest(context=context, dry_run=dry):
                result = prune_convention_meta(
                    store, cutoff_epoch=CUTOFF, current_session="",
                    dry_run=dry, now_epoch=NOW)
                self.assertIsNone(
                    result,
                    f"{context} (dry_run={dry}) must return None (the skip "
                    f"shape), got {result!r}")

    def test_garbage_file_returns_none_both_arms(self):
        store = os.path.join(_TMP, "garbage.sqlite")
        with open(store, "wb") as fh:
            fh.write(b"this is not a sqlite database at all" * 40)
        self._assert_none_both_arms(store, "garbage file")

    def test_truncated_store_returns_none_both_arms(self):
        store = os.path.join(_TMP, "trunc.sqlite")
        good = os.path.join(_TMP, "good-tmp.sqlite")
        _seeded_store(good)
        blob = Path(good).read_bytes()
        Path(store).write_bytes(blob[: len(blob) // 3])
        self._assert_none_both_arms(store, "truncated store")

    def test_meta_less_store_returns_none_both_arms(self):
        store = os.path.join(_TMP, "nometa.sqlite")
        conn = sqlite3.connect(store)
        conn.execute("CREATE TABLE other(x TEXT)")
        conn.commit()
        conn.close()
        self._assert_none_both_arms(store, "meta-less store")

    def test_locked_store_returns_none_real_run(self):
        store = os.path.join(_TMP, "locked.sqlite")
        _seeded_store(store)
        holder = sqlite3.connect(store, timeout=0.5)
        holder.execute("BEGIN EXCLUSIVE")
        try:
            started = threading.Event()
            outcome: "dict[str, object]" = {}

            def _run():
                started.set()
                # PRR-025: the skip shape is "returns None, never a
                # raise" — capture the outcome so a raise-instead-of-
                # None mutant fails this leg (threading's excepthook
                # would otherwise swallow it and the leg would stay
                # green on thread completion alone).
                try:
                    outcome["result"] = prune_convention_meta(
                        store, cutoff_epoch=CUTOFF, current_session="",
                        dry_run=False, now_epoch=NOW)
                except BaseException as exc:  # noqa: BLE001 - the pin
                    outcome["error"] = exc

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            started.wait(5)
            t.join(timeout=15)
            self.assertFalse(t.is_alive(),
                             "the pruner must return (None skip) after the "
                             "busy timeout, not hang on the locked store")
            self.assertNotIn(
                "error", outcome,
                f"the locked-store skip must NOT raise (the documented "
                f"None-skip contract); observed error={outcome.get('error')!r}")
            self.assertIsNone(
                outcome.get("result"),
                f"the locked-store skip must return None; observed "
                f"{outcome.get('result')!r}")
        finally:
            holder.rollback()
            holder.close()

    def test_absent_store_returns_none_and_creates_nothing(self):
        store = os.path.join(_TMP, "absent", "store.sqlite")
        os.makedirs(os.path.dirname(store), exist_ok=True)
        self._assert_none_both_arms(store, "absent store")
        self.assertFalse(os.path.exists(store),
                         "the pruner must never create the store file")


# ============================================================================
# Issue filed #446 AC2 + AC3 (frozen checks C7 + C8) (trace contracts D and E). Frozen at base c78a2d5;
# both classes are runnable directly:
#   python -X utf8 tests/test_meta_prune_skip_arms.py HermesTriggerBoundTest
#   python -X utf8 tests/test_meta_prune_skip_arms.py ConcurrentSweepRestoreAdjudicationTest
# ============================================================================

_HERMES_TARGET_CAP = 64   # documented host-profile bound (contract D)
_HERMES_BOUND_SLACK = 8   # pinned slack: the assertion allows cap + slack
_HERMES_STALE_SESSIONS = 200
_HERMES_LIVE_SESSION = "aaaabbbb-cccc-dddd-eeee-ffff00001111"
_HERMES_LIVE_KEY = f"hermes_convention_count_{_HERMES_LIVE_SESSION}"
_ISO_Z = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"


def _meta_rows(store) -> dict:
    conn = sqlite3.connect(store)
    try:
        return dict(conn.execute("SELECT key, value FROM meta").fetchall())
    finally:
        conn.close()


def _hermes_convention_rows(store) -> dict:
    return {k: v for k, v in _meta_rows(store).items()
            if k.startswith("hermes_convention_count_")}


def _remove_quietly(path) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


class HermesTriggerBoundTest(unittest.TestCase):
    """filed #446 AC2 (contract D, NEW-SURFACE): a Hermes-only host bounds its
    ``hermes_convention_count_*`` rows through the PRODUCTION writer entry —
    the hidden CLI ``store.py hermes-convention`` (the exact stdin-payload
    argv the Hermes hook fires; storelib/cli.py cmd_hermes_convention),
    which additionally triggers a bounded meta prune (prune_convention_meta
    with current_session = the hermes payload session id) once the
    convention-row count exceeds the target cap (64 rows; see
    _HERMES_TARGET_CAP; pinned slack 8).

    The twin of tests/test_meta_table_bound.py::HermesProductionBoundTest by
    design (the filed-AC2 NEW-SURFACE class for THIS file). Frozen at base: the
    production entry never prunes, so the bound leg fails and the table
    keeps every seeded row.
    """

    def _scratch_store(self):
        import shutil
        import subprocess
        base = Path(tempfile.mkdtemp(prefix="zmem-446-hermes-", dir=_TMP))
        self.addCleanup(shutil.rmtree, base, True)
        store = base / "store.sqlite"
        env = dict(os.environ)
        env.update({
            "ZMEM_STORE": str(store),
            "ZMEM_DATA": str(base),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "PYTHONUTF8": "1",
        })
        for key in ("ZMEM_SESSION", "ZMEM_SESSION_ID", "ZMEM_HOME",
                    "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
                    "ZCODE_PROJECT_DIR", "CLAUDE_PROJECT_DIR",
                    "ZMEM_BACKUP_DIR", "ZMEM_REMOTE_URL",
                    "ZMEM_CONVENTION_INTERVAL",
                    "ZMEM_HERMES_CONVENTION_EXISTING_ONLY",
                    "ZMEM_CONVENTION_ROW_CAP"):
            env.pop(key, None)
        # Materialize a real schema-complete store via the writer (same
        # channel as tests/test_meta_table_bound.py's MetaTableBoundBase).
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(SCRIPTS / "store.py"),
             "add", "--namespace", "user:global", "--type", "fact",
             "--content", "446 hermes trigger materialization row",
             "--confidence", "0.9"],
            env=env, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120)
        self.assertEqual(r.returncode, 0,
                         f"materializing add failed: rc={r.returncode} "
                         f"stderr={r.stderr[-400:]!r}")
        return store, env

    def _seed_census(self, store) -> int:
        """_HERMES_STALE_SESSIONS non-current rows (alternating legacy bare
        ints and TTL-stale JSON heartbeats) + the LIVE session's legacy
        bare-int row '5', by direct sqlite INSERT."""
        import json
        pairs = []
        for i in range(_HERMES_STALE_SESSIONS):
            key = (f"hermes_convention_count_aaaabbbb-cccc-dddd-eeee-"
                   f"{1000000000 + i:010d}")
            pairs.append((key, "1") if i % 2 == 0 else (key, json.dumps(
                {"count": 2, "last_seen": "2026-01-01T00:00:00Z"})))
        pairs.append((_HERMES_LIVE_KEY, "5"))
        conn = sqlite3.connect(str(store))
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                pairs)
            conn.commit()
        finally:
            conn.close()
        return len(_hermes_convention_rows(str(store)))

    def _drive_production_entry(self, env):
        import json
        import subprocess
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(SCRIPTS / "store.py"),
             "hermes-convention"],
            input=json.dumps({"payload": {"session_id": _HERMES_LIVE_SESSION}}),
            env=env, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120)

    def test_production_hermes_entry_bounds_convention_rows(self):
        """Leg (1): many stale hermes sessions' rows, then ONE production
        entry run -> the table is bounded (<= cap 64 + slack 8 = 72)."""
        store, env = self._scratch_store()
        before = self._seed_census(store)
        self.assertGreaterEqual(
            before, _HERMES_STALE_SESSIONS,
            f"fixture sanity: {_HERMES_STALE_SESSIONS} rows must be seeded; "
            f"observed {before}")
        r = self._drive_production_entry(env)
        self.assertEqual(r.returncode, 0,
                         f"hermes-convention must exit 0 (a best-effort "
                         f"prune must never break the write); observed "
                         f"rc={r.returncode} stderr={r.stderr[-400:]!r}")
        after = len(_hermes_convention_rows(str(store)))
        bound = _HERMES_TARGET_CAP + _HERMES_BOUND_SLACK
        self.assertLessEqual(
            after, bound,
            f"filed #446 AC2 (frozen check C7): a Hermes-only host running the production "
            f"hermes-convention entry with {before} convention rows must "
            f"bound the table to <= cap {_HERMES_TARGET_CAP} + slack "
            f"{_HERMES_BOUND_SLACK} = {bound} rows without operator "
            f"intervention; observed {after} rows surviving (no hermes-side "
            f"sweep trigger ran).")

    def test_live_legacy_row_retained_and_upgraded(self):
        """Leg (2): the LIVE hermes session's LEGACY bare-int row ('5',
        seeded directly in sqlite) is RETAINED and UPGRADED to canonical
        JSON with the count preserved then incremented (count==6) — NOT
        deleted by the bounded prune (deleted-then-recreated reads 1)."""
        import json
        store, env = self._scratch_store()
        self._seed_census(store)
        r = self._drive_production_entry(env)
        self.assertEqual(r.returncode, 0,
                         f"hermes-convention must exit 0; observed "
                         f"rc={r.returncode} stderr={r.stderr[-400:]!r}")
        rows = _hermes_convention_rows(str(store))
        self.assertIn(
            _HERMES_LIVE_KEY, rows,
            "filed #446 AC1/AC2 (frozen checks C6 + C7): the LIVE session's legacy row must be RETAINED "
            "by the production entry's bounded prune (current_session = the "
            f"hermes payload session id); it was DELETED. Surviving rows: "
            f"{len(rows)}")
        try:
            value = json.loads(rows[_HERMES_LIVE_KEY])
        except ValueError as exc:
            self.fail(f"filed #446 AC1/AC2 (frozen checks C6 + C7): the retained legacy row must be "
                      f"UPGRADED to canonical JSON; observed unparseable "
                      f"{rows[_HERMES_LIVE_KEY]!r} ({exc}).")
        self.assertIsInstance(value, dict,
                              f"upgraded value must be a JSON object; "
                              f"observed {rows[_HERMES_LIVE_KEY]!r}")
        self.assertEqual(
            value.get("count"), 6,
            f"filed #446 AC1/AC2 (frozen checks C6 + C7): legacy '5' preserved across the prune and "
            f"incremented to count==6 (a deleted-then-recreated row reads "
            f"count==1 — the retention failure). Observed {value!r} (raw "
            f"{rows[_HERMES_LIVE_KEY]!r}).")
        self.assertRegex(str(value.get("last_seen") or ""), _ISO_Z,
                         f"upgraded last_seen must be ISO-8601 Z; observed "
                         f"{value!r}")

    def test_fresh_rows_above_cap_bounded_malformed_retained(self):
        """Leg (3) — CHECK_WRONG amend (plan-critic r1 finding 6): the cap
        is a NUMERIC bound on the DELETABLE population, not a TTL trigger.
        A census of FRESH TTL-immune non-current JSON rows beyond the cap
        is bounded oldest-first; malformed rows are NEVER deleted (#128
        operator-inspection contract). At base the production entry never
        prunes, so every row survives -> RED with the same >cap+slack
        signature."""
        import json
        from datetime import datetime, timezone
        fresh_n = 100
        malformed_n = 3
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        pairs = [
            (f"hermes_convention_count_ccccdddd-eeee-ffff-"
             f"{3000000000 + i:010d}",
             json.dumps({"count": 2, "last_seen": now_iso}))
            for i in range(fresh_n)
        ]
        malformed_keys = [
            f"hermes_convention_count_ccccdddd-eeee-ffff-malformed-{i}"
            for i in range(malformed_n)
        ]
        pairs += [(k, "not-json{") for k in malformed_keys]
        pairs.append((_HERMES_LIVE_KEY, "5"))
        store, env = self._scratch_store()
        conn = sqlite3.connect(str(store))
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                pairs)
            conn.commit()
        finally:
            conn.close()
        r = self._drive_production_entry(env)
        self.assertEqual(r.returncode, 0,
                         f"hermes-convention must exit 0 (best-effort prune "
                         f"never breaks the write); observed rc="
                         f"{r.returncode} stderr={r.stderr[-400:]!r}")
        rows = _hermes_convention_rows(str(store))
        bound = _HERMES_TARGET_CAP + _HERMES_BOUND_SLACK
        self.assertLessEqual(
            len(rows), bound,
            f"filed #446 AC2 (frozen check C7): a FRESH census of {fresh_n} TTL-immune non-current "
            f"rows must still be bounded to <= cap {_HERMES_TARGET_CAP} + "
            f"slack {_HERMES_BOUND_SLACK} = {bound} (numeric bound on the "
            f"deletable population); observed {len(rows)} surviving.")
        for key in malformed_keys:
            self.assertIn(
                key, rows,
                f"filed #446 AC2 (frozen check C7): malformed row {key!r} must be RETAINED (never "
                f"deleted, #128 contract); observed {len(rows)} survivors.")
        self.assertIn(_HERMES_LIVE_KEY, rows,
                      "the LIVE session's row must survive the cap prune")
        self.assertEqual(json.loads(rows[_HERMES_LIVE_KEY]).get("count"), 6,
                         "live legacy '5' retained-and-upgraded to count 6 "
                         "under cap pressure")

    def test_cap_prunes_oldest_first_deterministic(self):
        """Leg (4) — wave-2 amend (r2 finding 3), fixtures corrected
        wave-3 (r3 N3-1/N3-2): TTL-FRESH distinct last_seen rows with
        keys ANTI-CORRELATED with time -> deterministic oldest-first
        deletion, exact survivor set (the 3 NEWEST stamps' keys = the 3
        SMALLEST keys + live + malformed), malformed byte-identity. A
        key-only sorter keeps the wrong three and fails. At base nothing
        is pruned -> RED."""
        import json
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        stamps = [
            (now - timedelta(hours=2 * (10 - i))).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            for i in range(10)
        ]  # ascending, all inside the 7-day TTL
        # key index runs OPPOSITE to time: oldest stamp -> largest key
        keys = [
            f"hermes_convention_count_eeee1111-ffff-ffff-"
            f"{9100000000 + (9 - i):010d}" for i in range(10)
        ]
        malformed_keys = [
            f"hermes_convention_count_eeee1111-ffff-ffff-bad-{i}"
            for i in range(2)
        ]
        pairs = [(k, json.dumps({"count": 2, "last_seen": ts}))
                 for k, ts in zip(keys, stamps)]
        pairs += [(k, "not-json{") for k in malformed_keys]
        pairs.append((_HERMES_LIVE_KEY, "5"))
        store, env = self._scratch_store()
        env = dict(env)
        env["ZMEM_CONVENTION_ROW_CAP"] = "4"
        conn = sqlite3.connect(str(store))
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                pairs)
            conn.commit()
        finally:
            conn.close()
        r = self._drive_production_entry(env)
        self.assertEqual(r.returncode, 0,
                         f"hermes-convention must exit 0; stderr tail: "
                         f"{r.stderr[-300:]!r}")
        rows = _hermes_convention_rows(str(store))
        # wave-4 fix: the 3 NEWEST stamps carry the 3 SMALLEST key
        # values, which sit at list positions keys[-3:] (anti-
        # correlated) — a key-only sorter keeps the wrong three.
        expected = set(keys[-3:]) | {_HERMES_LIVE_KEY} | set(malformed_keys)
        self.assertEqual(
            set(rows), expected,
            f"filed #446 AC2 (check C7) wave-4: cap 4 with a live current row must retain "
            f"exactly the 3 NEWEST stamps' rows (the 3 SMALLEST keys, "
            f"anti-correlated) + live + malformed; observed "
            f"{sorted(rows)[:12]}... expected {sorted(expected)[:12]}...")
        for k in malformed_keys:
            self.assertEqual(rows.get(k), "not-json{",
                             f"malformed {k!r} must stay byte-identical; "
                             f"observed {rows.get(k)!r}")

    def test_cap_tie_broken_by_key(self):
        """Leg (5) — wave-2 amend (r2 finding 3), stamp corrected wave-3
        (r3 N3-1): identical TTL-FRESH last_seen rows tie-break by KEY
        lexicographic (smallest keys deleted)."""
        import json
        from datetime import datetime, timedelta, timezone
        stamp = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        stamps = [stamp] * 4
        keys = [
            f"hermes_convention_count_ffff2222-ffff-ffff-"
            f"{9200000000 + i:010d}" for i in range(4)
        ]
        pairs = [(k, json.dumps({"count": 2, "last_seen": ts}))
                 for k, ts in zip(keys, stamps)]
        pairs.append((_HERMES_LIVE_KEY, "5"))
        store, env = self._scratch_store()
        env = dict(env)
        env["ZMEM_CONVENTION_ROW_CAP"] = "3"
        conn = sqlite3.connect(str(store))
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                pairs)
            conn.commit()
        finally:
            conn.close()
        r = self._drive_production_entry(env)
        self.assertEqual(r.returncode, 0,
                         f"hermes-convention must exit 0; stderr tail: "
                         f"{r.stderr[-300:]!r}")
        rows = _hermes_convention_rows(str(store))
        expected = set(sorted(keys)[-2:]) | {_HERMES_LIVE_KEY}
        self.assertEqual(
            set(rows), expected,
            f"filed #446 AC2 (check C7) wave-3: same-timestamp tie must break by key "
            f"(largest keys survive); observed {sorted(rows)!r} expected "
            f"{sorted(expected)!r}")

    def test_second_call_steady_state_no_churn(self):
        """Leg (6) — wave-3 replace (r3 N3-3): steady state under a
        PERSISTENT above-cap retained population (70 malformed + 10
        TTL-fresh + live = 81 > cap 64) — the SECOND call genuinely
        re-enters the classification pass and must write nothing except
        the live row's own increment."""
        import json
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        stamps = [
            (now - timedelta(hours=2 * (10 - i))).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            for i in range(10)
        ]
        fresh_keys = [
            f"hermes_convention_count_eeee1111-ffff-ffff-steady-"
            f"{i:010d}" for i in range(10)
        ]
        pairs = [(k, json.dumps({"count": 2, "last_seen": ts}))
                 for k, ts in zip(fresh_keys, stamps)]
        pairs += [
            (f"hermes_convention_count_eeee1111-ffff-ffff-steadbad-{i}",
             "not-json{")
            for i in range(70)
        ]
        pairs.append((_HERMES_LIVE_KEY, "5"))
        store, env = self._scratch_store()
        conn = sqlite3.connect(str(store))
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                pairs)
            conn.commit()
        finally:
            conn.close()
        r1 = self._drive_production_entry(env)
        self.assertEqual(r1.returncode, 0,
                         f"first entry rc={r1.returncode} stderr="
                         f"{r1.stderr[-300:]!r}")
        conn = sqlite3.connect(str(store))
        try:
            snapshot = dict(conn.execute(
                "SELECT key, value FROM meta").fetchall())
        finally:
            conn.close()
        retained = len(_hermes_convention_rows(str(store)))
        self.assertGreater(
            retained, _HERMES_TARGET_CAP,
            f"fixture sanity: retained population ({retained}) must stay "
            f"above cap {_HERMES_TARGET_CAP} so the second call "
            f"re-enters the classification pass")
        r2 = self._drive_production_entry(env)
        self.assertEqual(r2.returncode, 0,
                         f"second entry rc={r2.returncode} stderr="
                         f"{r2.stderr[-300:]!r}")
        conn = sqlite3.connect(str(store))
        try:
            after = dict(conn.execute(
                "SELECT key, value FROM meta").fetchall())
        finally:
            conn.close()
        self.assertEqual(
            {k: v for k, v in after.items() if k != _HERMES_LIVE_KEY},
            {k: v for k, v in snapshot.items() if k != _HERMES_LIVE_KEY},
            "filed #446 AC2 (check C7) wave-3: steady state — the second call must leave "
            "every row byte-identical apart from the live row's own "
            "counter increment (no prune churn)")
        self.assertEqual(
            json.loads(after[_HERMES_LIVE_KEY]).get("count"), 7,
            "second-call sanity: live row 6->7 (writer contract)")


class ConcurrentSweepRestoreAdjudicationTest(unittest.TestCase):
    """filed #446 AC3 (frozen check C8) (contract E): the sweep's meta phase serializes under the
    SAME maintenance-lock protocol restore uses
    (``_acquire_maintenance_lock_with_liveness`` / ``_release_named_lock``
    from storelib.schema — the backup.py cmd_restore ladder), best-effort:
    a LIVE maintenance-lock holder makes the meta phase SKIP (return None,
    no writes), exactly like the existing busy-timeout skip.

    Frozen at base: prune_convention_meta takes NO maintenance lock, so with
    a live holder it still runs and writes — leg 1 fails. Leg 2 reproduces
    the Windows store-replacement interaction the issue deferred
    (observation, printed). Leg 3 pins that the maintenance lock is a real
    mutex against a restore-shaped acquisition (the exclusion the
    serialization buys).
    """

    @classmethod
    def setUpClass(cls) -> None:
        # PRR-022 co-run citizenship (delta-r2): a pytest co-run with a
        # sibling file that freezes storelib under ITS scratch (e.g.
        # test_hermes_convention_order) leaves sys.modules pointing at the
        # sibling's STORE_PATH by the time this class's lazy imports run.
        # Re-apply THIS file's pins and re-import fresh so the
        # maintenance-lock fixtures land beside THIS file's scratch.
        os.environ.update({
            "ZMEM_STORE": os.path.join(_TMP, "store.sqlite"),
            "ZMEM_DATA": _TMP,
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "PYTHONIOENCODING": "utf-8",
        })
        for key in ("ZMEM_HOME", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
                    "ZCODE_PROJECT_DIR", "CLAUDE_PROJECT_DIR",
                    "ZMEM_SESSION", "ZMEM_SESSION_ID", "ZMEM_BACKUP_DIR"):
            os.environ.pop(key, None)
        import sys as _sys
        for key in [key for key in list(_sys.modules)
                    if key == "storelib" or key.startswith("storelib.")]:
            del _sys.modules[key]
        import importlib as _importlib
        import storelib.schema as _schema
        _importlib.reload(_schema)
        import storelib.backup as _backup
        _importlib.reload(_backup)

    def _frozen_path_store(self) -> str:
        """A store at THIS process's frozen storelib STORE_PATH (frozen at
        first storelib import under the header-pinned env) — the lock file
        ``.zmem-maintenance.lock`` lands beside exactly this path, as it
        would for a restore of this store. Carries one prunable stale
        legacy row and one current-session legacy row."""
        from storelib.schema import STORE_PATH
        store = str(STORE_PATH)
        self.assertTrue(
            store.startswith(_TMP),
            f"fixture sanity: the frozen store path must be the scratch "
            f"pin, got {store!r}")
        _remove_quietly(store)
        conn = sqlite3.connect(store)
        try:
            conn.executescript(
                "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                "INSERT INTO meta VALUES ('schema_version', '19');"
                "INSERT INTO meta VALUES "
                "('hermes_convention_count_adjudication-stale', '1');"
                "INSERT INTO meta VALUES "
                "('hermes_convention_count_adjudication-live', '5');")
            conn.commit()
        finally:
            conn.close()
        self.addCleanup(_remove_quietly, store)
        return store

    def test_meta_prune_skips_while_maintenance_lock_held(self):
        """Leg 1: a restore-shaped LIVE maintenance-lock holder makes the
        meta phase skip cleanly (None, zero writes); a released lock lets
        the same prune run (non-vacuous control)."""
        from storelib.schema import (_acquire_maintenance_lock_with_liveness,
                                     _release_named_lock)
        store = self._frozen_path_store()
        before = _meta_rows(store)
        token = _acquire_maintenance_lock_with_liveness()  # as restore does
        self.assertIsNotNone(
            token, "fixture sanity: the maintenance lock must be acquirable "
            "when no holder exists")
        try:
            result = prune_convention_meta(
                store, cutoff_epoch=CUTOFF,
                current_session="adjudication-live",
                dry_run=False, now_epoch=NOW)
            after = _meta_rows(store)
        finally:
            _release_named_lock("maintenance", token)
        self.assertIsNone(
            result,
            "filed #446 AC3 (frozen check C8): while a LIVE holder owns the maintenance lock (as "
            "restore does), the sweep meta phase must SKIP (return None, no "
            f"writes); observed {result!r} — the meta prune ran "
            "unserialized against restore")
        self.assertEqual(
            after, before,
            "filed #446 AC3 (frozen check C8): the skipped meta phase must leave the meta table "
            "byte-identical (no deletes, no upgrades)")

        # Non-vacuous control: lock released -> the same prune runs and
        # prunes (the skip above was the lock, nothing else).
        control = prune_convention_meta(
            store, cutoff_epoch=CUTOFF,
            current_session="adjudication-live",
            dry_run=False, now_epoch=NOW)
        self.assertIsNotNone(
            control, "control: with the maintenance lock released the meta "
            "prune must run (proves the skip above was lock-caused)")
        self.assertGreaterEqual(control[0], 1,
                                f"control: the stale legacy row must be "
                                f"pruned; observed counts {control!r}")

    def test_meta_prune_dry_run_skips_while_maintenance_lock_held(self):
        """Leg 1b — wave-2 amend (r2 finding 1, Critical): the DRY-RUN
        arm also opens a store handle (open_readonly), which on Windows
        blocks restore's os.replace exactly like the real-run handle —
        so it must take the SAME maintenance lock and skip (None, zero
        sidecars/residue) under a live holder. At base the dry-run arm
        takes no lock and reports counts — RED."""
        from storelib.schema import (_acquire_maintenance_lock_with_liveness,
                                     _release_named_lock)
        store = self._frozen_path_store()
        before = _meta_rows(store)
        token = _acquire_maintenance_lock_with_liveness()  # as restore does
        self.assertIsNotNone(token, "fixture sanity: lock acquirable")
        try:
            result = prune_convention_meta(
                store, cutoff_epoch=CUTOFF,
                current_session="adjudication-live",
                dry_run=True, now_epoch=NOW)
            after = _meta_rows(store)
        finally:
            _release_named_lock("maintenance", token)
        self.assertIsNone(
            result,
            "filed #446 AC3 (frozen check C8) wave-2: the DRY-RUN meta phase must ALSO skip under "
            "a live maintenance-lock holder (its open_readonly handle "
            f"blocks restore's os.replace on Windows); observed {result!r}")
        self.assertEqual(
            after, before,
            "filed #446 AC3 (frozen check C8) wave-2: the skipped dry-run must leave the meta "
            "table byte-identical")
        # Non-vacuous control: released -> the dry-run reports counts
        # (would-prune accounting) without deleting.
        control = prune_convention_meta(
            store, cutoff_epoch=CUTOFF,
            current_session="adjudication-live",
            dry_run=True, now_epoch=NOW)
        self.assertIsNotNone(
            control, "control: dry-run with the lock released must report "
            "counts (the skip above was lock-caused)")
        self.assertGreaterEqual(control[0], 1,
                                f"control: dry-run must count the stale "
                                f"legacy row as removable; got {control!r}")
        self.assertEqual(
            _meta_rows(store), before,
            "control: a dry-run NEVER deletes (byte-identical meta)")

    def test_windows_store_replacement_blocked_by_open_transaction(self):
        """Leg 2: the Windows store-replacement interaction the issue
        deferred. With an open sqlite connection holding an ACTIVE
        transaction on the store file, os.replace of that file raises
        PermissionError on Windows (why the meta phase must serialize
        against restore's store replacement). OBSERVED and printed; on
        non-Windows the replace succeeds (noted — the hazard is
        Windows-specific)."""
        store = os.path.join(_TMP, "replace-probe.sqlite")
        replacement = os.path.join(_TMP, "replace-probe.new.sqlite")
        for path in (store, replacement):
            _remove_quietly(path)
            self.addCleanup(_remove_quietly, path)
        conn = sqlite3.connect(store)
        try:
            conn.executescript(
                "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                "INSERT INTO meta VALUES ('schema_version', '19');")
            conn.commit()
        finally:
            conn.close()
        Path(replacement).write_bytes(Path(store).read_bytes())

        holder = sqlite3.connect(store)
        observed, detail = None, ""
        try:
            holder.execute("BEGIN IMMEDIATE")
            holder.execute("INSERT OR REPLACE INTO meta VALUES ('probe', '1')")
            try:
                os.replace(replacement, store)
                observed = "replaced"
            except PermissionError as exc:
                observed, detail = "PermissionError", str(exc)
            except OSError as exc:  # pragma: no cover - platform variance
                observed, detail = f"OSError:{type(exc).__name__}", str(exc)
        finally:
            try:
                holder.rollback()
            finally:
                holder.close()
        print(f"[AC8 leg 2] os.replace of a store under an open ACTIVE "
              f"transaction on os.name={os.name!r} sys.platform="
              f"{sys.platform!r}: observed={observed} {detail}")
        if os.name == "nt":
            self.assertEqual(
                observed, "PermissionError",
                "filed #446 AC3 (frozen check C8): on Windows, replacing a store file while a "
                "connection holds an active transaction must be REFUSED "
                "(PermissionError) — the reproduced interaction that makes "
                f"meta-phase/restore serialization required. Observed "
                f"{observed!r} ({detail}).")
        else:
            self.assertEqual(
                observed, "replaced",
                "filed #446 AC3 (frozen check C8): on non-Windows platforms the replace succeeds "
                "(the hazard is Windows-specific); observed "
                f"{observed!r} ({detail}).")

    def test_maintenance_lock_is_a_real_mutex_vs_restore_shaped_acquire(self):
        """Leg 3: acquire the maintenance lock the way the meta phase will;
        a second, restore-shaped acquisition is mutually excluded (None),
        and the lock is acquirable again after release."""
        from storelib.schema import (_acquire_maintenance_lock_with_liveness,
                                     _release_named_lock)
        token = _acquire_maintenance_lock_with_liveness()
        self.assertIsNotNone(
            token, "fixture sanity: the maintenance lock must be acquirable")
        try:
            second = _acquire_maintenance_lock_with_liveness()
            self.assertIsNone(
                second,
                "filed #446 AC3 (frozen check C8): while the meta phase holds the maintenance lock, "
                "a restore-shaped acquisition must be mutually excluded "
                "(None) — the lock is a real mutex")
        finally:
            _release_named_lock("maintenance", token)
        third = _acquire_maintenance_lock_with_liveness()
        self.assertIsNotNone(
            third, "after release the maintenance lock must be acquirable "
            "again (the exclusion above was the held lock, not a wedge)")
        _release_named_lock("maintenance", third)

    def test_wrapper_lock_ordering_source_pin(self):
        """PRR-024: the wrapper's Windows hazard is the ORDERING — the
        maintenance lock must be acquired BEFORE the store is opened and
        released only AFTER the handle is closed. The C8 legs prove the
        no-lock mutant dies but acquire-after-open / release-before-close
        mutants pass them; this source-order pin kills both. (Text-order
        scan over prune_convention_meta's body — the established
        schema-lock-supp pattern.)"""
        from pathlib import Path as _P
        src = (_P(__file__).resolve().parent.parent
               / "skills" / "memory" / "scripts"
               / "storelib" / "backup.py").read_text(encoding="utf-8")
        body = src[src.index("def prune_convention_meta("):
                   src.index("def cmd_sweep(")]
        acquire = body.find("_acquire_maintenance_lock_with_liveness()")
        ro_open = body.find("open_readonly(store_path)")
        rw_open = body.find("sqlite3.connect(uri, uri=True")
        close_handle = body.find("handle.close()")
        close_conn = body.find("conn.close()")
        release = body.find('_release_named_lock("maintenance", m_token)')
        for name, needle in (("acquire", acquire), ("readonly open", ro_open),
                             ("rw open", rw_open),
                             ("handle close", close_handle),
                             ("conn close", close_conn), ("release", release)):
            self.assertGreaterEqual(
                needle, 0,
                f"wrapper source pin: {name!r} site not found in "
                f"prune_convention_meta — the function was restructured; "
                f"re-derive this pin")
        self.assertLess(
            acquire, min(ro_open, rw_open),
            "PRR-024: the maintenance lock must be acquired BEFORE any "
            "store open (an idle open handle blocks restore's os.replace "
            "on Windows)")
        self.assertGreater(
            release, max(close_handle, close_conn),
            "PRR-024: the maintenance lock must be released only AFTER "
            "the handle/conn is closed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
