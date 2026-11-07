"""Checks C4+C5 — issue #128: the per-session convention counters in the
meta table are BOUNDED, their values carry a machine-readable heartbeat, and
the verify-hook reader parses the new shape. Contracts pinned per the fix
plan's REVISION 3 deltas (.agents/issue-traces/443-128-store-hygiene-
collated/05-fix-plan.md).

C4 (MetaSweepTest — DISCRIMINATING at base): the Hermes compatibility ring
writes one `hermes_convention_count_<session>` (and, on legacy stores, a
bare `convention_count_<session>`) meta row PER SESSION and nothing ever
removed them. Law, asserted over one seeded census after
`store.py sweep --max-age-days 7`:
  1. convention_count_<LEGACY-NONCURRENT>='7'            -> REMOVED
  2. convention_count_sess_<LEGACY-NONCURRENT>='3'       -> REMOVED
  3. hermes_convention_count_<STALE-JSON> (last_seen
     2026-01-01, far older than the 7-day TTL)           -> REMOVED
  4. hermes_convention_count_<FRESH-JSON> (last_seen now) -> RETAINED
     byte-identical
  5. hermes_convention_count_<LEGACY-INT>='12'
     (non-current session)                               -> REMOVED
  6. hermes_convention_count_<MALFORMED>='not-json{'     -> RETAINED
     byte-identical (unparseable values are NEVER deleted)
  7. hermes_convention_count_<CURRENT-SESSION>='9' with
     ZMEM_SESSION naming the session in the sweep child  -> RETAINED and
     UPGRADED to canonical JSON {"count":9,"last_seen":<ISO-Z ~now>} with
     count UNCHANGED (REVISION 3 / #128 AC2) — NOT byte-identical '9'.
     In --dry-run it stays byte-identical '9'.
  8. schema_version / last_backup / unrelated_note       -> byte-identical
  9. near-miss keys (convention_countX_abc,
     hermes_convention_count without the trailing underscore,
     xconvention_count_abc)                              -> byte-identical,
     never matched by the true-prefix family predicates
Census summary counts (the family is exactly the rows above, of which the
retained are: fresh-json, malformed, current-session-legacy):
`meta convention rows removed=4 retained=3 unparseable=1` on stderr — the
real run and the dry-run carry the SAME numbers, the dry-run additionally
announces `would prune` and deletes/rewrites nothing.

DirectPrunerTest drives the planned seam
`storelib.backup.prune_convention_meta(store_path, cutoff_epoch=X,
current_session="", dry_run=False)` in-process: at the pre-fix tree the
function does not exist — the readable failure IS the discriminating leg.
Pins exact-cutoff retention (last_seen == iso(cutoff) survives, strict <)
and atomic rollback (a BEFORE-DELETE trigger aborts mid-prune; zero partial
deletes may survive).

CadenceSummaryTest drives `store.py session-cadence` (no --session-id argv
exists; the session is threaded via ZMEM_SESSION) and pins the summary
fragment `meta convention rows removed=N retained=N unparseable=N` plus the
current-session upgrade. At base the fragment does not exist.

C5 (CounterValueTest — DISCRIMINATING at base): the ring counter increment
(storelib/cli.py:cmd_hermes_convention, the write at cli.py:2139) upserts a
bare INTEGER string. Law: after an increment the value must parse as JSON
`{"count": N, "last_seen": "<ISO-8601 Z>"}` — a legacy '3' comes back as
count==4; a fresh counter goes 1 then 2 back-to-back (last_seen merely
valid ISO-Z; NO wall-clock-advance assertion); six CONCURRENT increments
land count==6 exactly (no lost update); a malformed value and a wrong-typed
JSON value are both treated as 0 (next increment reads count 1).

VerifyHookParityTest: the REAL reader
hermes-plugin/hooks/zmem-hermes-verify.py parses the counter with
`str(value).isdigit()` — a JSON value collapses to 0 and the verify nudge
silently dies. Parity law: a JSON counter with count>0 must take the
NONZERO path (emit the `{"action": "continue", ...}` nudge), a legacy bare
int keeps nudging (PRESERVING), and a MALFORMED value is treated as 0
(empty `{}` emission) in both worlds. The lockstep matrix compares
`schema_meta.parse_convention_count` against the hook's local twin parser —
the implementer MUST name the hook's twin `_parse_convention_count`
(zmem-hermes-verify.py) so this matrix can bind it; both are absent at the
pre-fix tree → readable discriminating failures.

Frozen C5 argv: `python -X utf8 tests/test_meta_table_bound.py
CounterValueTest VerifyHookParityTest` (both class names are load-bearing).

REVISION 3 round-3 additions: the pruner seam gains an injected-clock keyword
`now_epoch` (DirectPrunerTest.test_injected_now_upgrades_retained_legacy_
deterministically — last_seen EXACTLY iso(now_epoch), '9' byte-identical in
--dry-run); and the skip-preservation matrix around the sweep: a LOCKED store
(EXCLUSIVE transaction held by another process), a NEWER-schema store
(schema_version='9999'), and an UNREADABLE garbage store must each skip the
meta prune fail-open while the FILE sweep still proceeds (PRESERVING at base
— the base sweep is file-only — and the post-fix locked/refusal pins); the
cadence ordering leg (sweep BEFORE connect, sentinel reaped under a lock);
the writer's atomic rollback on an aborted upsert (PRESERVING); and the
no-storelib hook-copy leg (degraded path must not crash and must still nudge
on a JSON counter — DISCRIMINATING).

Stores are throwaway scratch stores materialized by a real `store.py add`;
meta rows are seeded by direct sqlite INSERT. Run:
python -X utf8 tests/test_meta_table_bound.py
"""

from __future__ import annotations

# -- env pinned BEFORE the issue-#244 isolation guard loads (the guard reads
# the ambient env at import time; an unpinned import aborts the whole file
# on a box with a real default store). Same ordering as
# tests/test_import_ns_control.py.
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
VERIFY_HOOK = REPO_ROOT / "hermes-plugin" / "hooks" / "zmem-hermes-verify.py"
PYTHON = sys.executable

# Ambient env snapshot taken BEFORE any mutation (review round
# PRR-008): tearDownModule restores THESE values, not the scratch
# pins written below.
_PINNED_KEYS = ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODEL_AUTODOWNLOAD",
                "PYTHONIOENCODING", "ZMEM_BACKUP_DIR", "ZMEM_HOME",
                "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
                "ZCODE_PROJECT_DIR", "CLAUDE_PROJECT_DIR",
                "ZMEM_SESSION", "ZMEM_SESSION_ID")
_SAVED_ENV = {key: os.environ.get(key) for key in _PINNED_KEYS}

_TMP = tempfile.mkdtemp(prefix="zmem-128-")
os.environ.update({
    "ZMEM_STORE": os.path.join(_TMP, "store.sqlite"),
    "ZMEM_DATA": _TMP,
    "ZMEM_MODEL_AUTODOWNLOAD": "0",
    "PYTHONIOENCODING": "utf-8",
})
for _k in ("ZMEM_BACKUP_DIR", "ZMEM_HOME", "CLAUDE_PLUGIN_DATA",
           "ZCODE_PLUGIN_DATA", "ZCODE_PROJECT_DIR", "CLAUDE_PROJECT_DIR",
           "ZMEM_SESSION", "ZMEM_SESSION_ID"):
    os.environ.pop(_k, None)
import _store_guard  # noqa: E402,F401  (issue #244 isolation guard)

# storelib modules present before THIS file imported anything (defensive:
# a pytest co-run may have loaded it under another file's env); restored in
# tearDownModule after the in-process direct-function legs purge/reload.
_SAVED_MODULES = {key: value for key, value in list(sys.modules.items())
                  if key == "storelib" or key.startswith("storelib.")}


def tearDownModule():  # noqa: N802 - unittest hook
    """Restore the ambient env / module cache (pytest co-run citizenship)."""
    for key in [key for key in list(sys.modules)
                if key == "storelib" or key.startswith("storelib.")]:
        del sys.modules[key]
    sys.modules.update(_SAVED_MODULES)
    for key, value in _SAVED_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    shutil.rmtree(_TMP, ignore_errors=True)


ISO_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
UPGRADE_TOLERANCE_S = 120  # last_seen may be +/- 120s from the test clock


def _now_iso_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch_of_iso_z(value: str) -> float:
    return datetime.strptime(
        value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def _iso_z_of_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(
        epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


LEGACY_PLAIN = "convention_count_11111111-2222-4333-8444-555566667777"
LEGACY_SESS = "convention_count_sess_11111111-2222-4333-8444-55556666778a"
HERMES_STALE = "hermes_convention_count_11111111-2222-4333-8444-55556666778b"
HERMES_FRESH = "hermes_convention_count_11111111-2222-4333-8444-55556666778c"
HERMES_LEGACY_INT = "hermes_convention_count_11111111-2222-4333-8444-55556666778e"
HERMES_MALFORMED = "hermes_convention_count_11111111-2222-4333-8444-55556666778f"
CURRENT_SESSION = "11111111-2222-4333-8444-5555666677f0"
LAUNCHER_CURRENT_LEGACY = f"convention_count_sess_{CURRENT_SESSION}"
MALFORMED_VALUE = "not-json{"
WRONG_TYPED_JSON = '{"count":"x","last_seen":5}'
NEAR_MISS_KEYS = (
    "convention_countX_abc",            # X where the prefix wants _
    "hermes_convention_count",          # no trailing underscore
    "xconvention_count_abc",            # leading noise
)
UNRELATED_KEYS = ("schema_version", "last_backup", "unrelated_note")

REMOVED_KEYS = (LEGACY_PLAIN, LEGACY_SESS, HERMES_STALE, HERMES_LEGACY_INT)
RETAINED_KEYS = (HERMES_FRESH, HERMES_MALFORMED, LAUNCHER_CURRENT_LEGACY)


def _is_convention_family(key: str) -> bool:
    """True-prefix family membership (REVISION 3 F4: true-prefix predicates,
    never a bare LIKE/substring — the near-miss keys must not match)."""
    return (key.startswith("convention_count_")
            or key.startswith("hermes_convention_count_"))


# Exact census numbers: 4 rows removed (groups 1,2,3,5), 3 retained
# (fresh-json + malformed + current-session-legacy), 1 of the retained
# unparseable. The same numbers appear in the real run, the dry-run, and the
# session-cadence summary.
EXPECTED_REMOVED_N = 4
EXPECTED_RETAINED_N = 3
EXPECTED_UNPARSEABLE_N = 1
EXPECTED_SUMMARY_FRAGMENT = (
    f"meta convention rows removed={EXPECTED_REMOVED_N} "
    f"retained={EXPECTED_RETAINED_N} unparseable={EXPECTED_UNPARSEABLE_N}")

LEGACY_SESSION = "11111111-2222-4333-8444-5555666677c5"
FRESH_SESSION = "11111111-2222-4333-8444-5555666677e0"
CONCURRENT_SESSION = "11111111-2222-4333-8444-555566667703"
MALFORMED_WRITE_SESSION = "11111111-2222-4333-8444-555566667704"
WRONG_TYPED_SESSION = "11111111-2222-4333-8444-555566667705"
HOOK_JSON_SESSION = "11111111-2222-4333-8444-555566667701"
HOOK_MALFORMED_SESSION = "11111111-2222-4333-8444-555566667702"
HOOK_LEGACY_INT_SESSION = "11111111-2222-4333-8444-555566667706"
# Round-3 additions (plan-critic REVISION 3 deltas).
INJECTED_NOW_SESSION = "11111111-2222-4333-8444-555566667710"
ROLLBACK_SESSION = "11111111-2222-4333-8444-555566667707"
HOOK_NOSTORELIB_SESSION = "11111111-2222-4333-8444-555566667708"
# Fixed injected-clock epoch (gmtime(1800000000) == 2027-01-15T08:00:00Z):
# deterministic AND far from the real wall clock, so a pruner that silently
# falls back to time.time() renders a DIFFERENT last_seen and the
# exact-equality assertion catches it.
INJECTED_NOW_EPOCH = 1800000000

# Ambient zmem/project markers are stripped from every child process so the
# subprocess resolves exactly the per-test scratch store (storelib freezes
# STORE_PATH at first import — an unpinned child could touch the shared
# operator store). Mirrors tests/test_add_namespace_hostless.py's pattern.
_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
    "ZMEM_INJECT_FLOOR_LEX", "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR",
    "ZMEM_SESSION", "ZMEM_SESSION_ID", "ZMEM_REMOTE_URL", "ZMEM_AUTO_REKEY",
    "ZMEM_HERMES_CONVENTION_EXISTING_ONLY", "ZMEM_CONVENTION_INTERVAL",
    "ZMEM_CONVENTION_ROW_CAP",
    "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID", "ZMEM_BACKUP_DIR",
    "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA", "CLAUDE_PLUGIN_ROOT",
    "ZCODE_PLUGIN_ROOT", "PLUGIN_ROOT", "PLUGIN_DATA",
    "ZCODE_PROJECT_DIR", "CLAUDE_PROJECT_DIR",
)


def _excerpt(proc: subprocess.CompletedProcess, tail: int = 900) -> str:
    return (f"rc={proc.returncode}\n--- stdout (tail) ---\n"
            f"{proc.stdout[-tail:]!r}\n--- stderr (tail) ---\n"
            f"{proc.stderr[-tail:]!r}")


class MetaTableBoundBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-128-leg-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = self.tmp / "store.sqlite"
        self.env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
        self.env.update({
            "ZMEM_STORE": str(self.store),
            "ZMEM_DATA": str(self.tmp),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "PYTHONUTF8": "1",
        })
        # Materialize a real schema-complete store via the writer.
        r = self._run("add", "--namespace", "user:global", "--type", "fact",
                      "--content", "meta-bound materialization row",
                      "--confidence", "0.9")
        self.assertEqual(r.returncode, 0,
                         f"materializing add failed:\n{_excerpt(r)}")

    def _run(self, *args, timeout=120, stdin=None,
             env=None) -> subprocess.CompletedProcess:
        """Run store.py with the given subcommand args."""
        return self._run_argv(
            [PYTHON, "-X", "utf8", str(STORE_PY), *args],
            timeout=timeout, stdin=stdin, env=env)

    def _run_argv(self, argv, *, timeout=120, stdin=None,
                  env=None, cwd=None) -> subprocess.CompletedProcess:
        return subprocess.run(
            argv,
            env=env or self.env, capture_output=True, text=True,
            timeout=timeout, input=stdin, cwd=cwd,
        )

    def _meta_values(self) -> dict:
        conn = sqlite3.connect(str(self.store))
        try:
            return {k: v for k, v in
                    conn.execute("SELECT key, value FROM meta").fetchall()}
        finally:
            conn.close()

    def _set_meta(self, key: str, value: str) -> None:
        conn = sqlite3.connect(str(self.store))
        try:
            conn.execute("INSERT OR REPLACE INTO meta(key, value)"
                         " VALUES (?, ?)", (key, value))
            conn.commit()
        finally:
            conn.close()

    def _seed_meta(self) -> dict:
        """The issue-#128 meta census (groups 1-9). Returns the seeded
        key->value map for byte-identity comparisons."""
        seeds = {
            # 1/2: legacy bare counters, no age evidence, no writer.
            LEGACY_PLAIN: "7",
            LEGACY_SESS: "3",
            # 3: stale JSON heartbeat (far older than the 7-day TTL).
            HERMES_STALE: json.dumps(
                {"count": 5, "last_seen": "2026-01-01T00:00:00Z"}),
            # 4: fresh JSON heartbeat (~now) — retained byte-identical.
            HERMES_FRESH: json.dumps({"count": 2, "last_seen": _now_iso_z()}),
            # 5: legacy integer under the hermes_ prefix, non-current.
            HERMES_LEGACY_INT: "12",
            # 6: unparseable — NEVER deleted, NEVER rewritten.
            HERMES_MALFORMED: MALFORMED_VALUE,
            # 7: legacy integer for the CURRENT session (named by ZMEM_SESSION
            # in the sweep subprocess) — retained AND upgraded in the real
            # run; byte-identical '9' in --dry-run. CHECK_WRONG amend (issue
            # #446 AC6 / plan-critic r1 finding 1): the pairing is now
            # PRODUCTION-SHAPED for the sweep surface — the retired hook-era
            # writer keyed convention_count_sess_<ZMEM_SESSION>, exactly this
            # shape; the hermes-family pairing (hermes payload session id vs
            # hermes_convention_count_<sid>) is production-covered end-to-end
            # by HermesProductionBoundTest below through the CLI entry.
            LAUNCHER_CURRENT_LEGACY: "9",
            # 8: unrelated meta rows — byte-identical after.
            "schema_version": "19",
            "last_backup": "x",
            "unrelated_note": "x",
            # 9: near-miss keys — byte-identical, never family-matched.
            "convention_countX_abc": "1",
            "hermes_convention_count": "7",
            "xconvention_count_abc": "1",
        }
        for key, value in seeds.items():
            self._set_meta(key, value)
        return seeds

    def _assert_summary_counts(self, text: str, context: str) -> None:
        """The census summary carries removed=4 retained=3 unparseable=1
        (exact numbers for THIS seed: 4 removed, 3 retained — fresh-json +
        malformed + current-session-legacy — of which 1 unparseable)."""
        self.assertIn(
            "meta convention rows removed=", text,
            f"C4 ({context}): the meta-convention summary fragment is "
            f"absent. Observed text:\n{text!r}")
        for field, expected in (("removed", EXPECTED_REMOVED_N),
                                ("retained", EXPECTED_RETAINED_N),
                                ("unparseable", EXPECTED_UNPARSEABLE_N)):
            m = re.search(rf"{field}=(\d+)", text)
            self.assertIsNotNone(
                m, f"C4 ({context}): the summary must carry {field}=<N>; "
                   f"observed text:\n{text!r}")
            self.assertEqual(
                int(m.group(1)), expected,
                f"C4 ({context}): the summary must report {field}={expected} "
                f"for this census; observed {field}={m.group(1)} in "
                f"text:\n{text!r}")

    def _stale_sentinel(self, name: str = ".convention-commit-prompted-r3"
                        ) -> Path:
        """One STALE sentinel file in ZMEM_DATA (mtime 30 days ago), from the
        SENTINEL_PREFIXES family. Naming/aging mirrors tests/test_sweep.py's
        _mk: write the file, then os.utime it back 30 days."""
        p = self.tmp / name
        p.write_text("1", encoding="utf-8")
        ts = time.time() - 30 * 86400
        os.utime(p, (ts, ts))
        return p

    def _assert_meta_byte_identical(self, before: dict, context: str) -> None:
        """Every meta row present before must still exist with the EXACT same
        value, and no row may be added — the byte-identity pin behind the
        locked-store / newer-schema / unreadable-store skip legs."""
        after = self._meta_values()
        missing = sorted(k for k in before if k not in after)
        added = sorted(k for k in after if k not in before)
        changed = sorted(k for k in before
                         if k in after and after[k] != before[k])
        self.assertEqual(
            (missing, added, changed), ([], [], []),
            f"C4 ({context}): every meta row must be byte-identical (no "
            f"deletes, no adds, no rewrites); observed missing={missing!r} "
            f"added={added!r} changed={changed!r}. Meta after: "
            f"{sorted(after.items())!r}")


class MetaSweepTest(MetaTableBoundBase):
    """C4 — sweep prunes stale/legacy convention counters from the meta
    table, upgrades the current-session legacy row, and preserves everything
    else byte-for-byte."""

    def _sweep(self, dry_run=False):
        env = dict(self.env)
        env["ZMEM_SESSION"] = CURRENT_SESSION  # group 7: current-session rule
        args = ["sweep", "--max-age-days", "7"]
        if dry_run:
            args.append("--dry-run")
        return self._run(*args, env=env)

    def test_corrupt_but_shaped_timestamp_retained_as_unparseable(self):
        """Impl-review round 2 finding 5: a RECENT row whose last_seen is
        shape-valid but unresolvable by the epoch parser (strptime accepts
        second=60; _parse_iso_to_epoch returns its 0.0 failure sentinel for
        it) must be RETAINED and counted unparseable — never deleted as
        'ancient' by the sentinel epoch. This is the data-loss shape the
        round-2 reviewer's probe caught."""
        corrupt_recent = json.dumps(
            {"count": 4,
             "last_seen": time.strftime("%Y-%m-%dT%H:%M:", time.gmtime())
             + "60Z"})
        self._set_meta("hermes_convention_count_leap-legacy-aa", corrupt_recent)
        r = self._sweep()
        self.assertEqual(r.returncode, 0, _excerpt(r))
        meta = self._meta_values()
        key = "hermes_convention_count_leap-legacy-aa"
        self.assertIn(key, meta, "the corrupt-but-shaped RECENT row was "
                     f"DELETED (data loss); observed meta: {sorted(meta)}")
        self.assertEqual(meta[key], corrupt_recent,
                         "the corrupt-but-shaped row must stay byte-identical")
        m = re.search(r"unparseable=(\d+)", r.stderr)
        self.assertIsNotNone(m, f"summary missing; stderr: {r.stderr!r}")
        self.assertGreaterEqual(int(m.group(1)), 1,
                                "the corrupt-but-shaped row must be counted "
                                f"unparseable; stderr: {r.stderr!r}")

    # NOTE (APPLIED, CHECK_WRONG amend — issue #446 AC6 / plan-critic r1
    # finding 1): the group-7 pairing in this leg (and its --dry-run twin
    # below) was SYNTHETIC at first freeze (a hermes-prefixed key paired
    # with ZMEM_SESSION — a pairing production never produced). It is now
    # PRODUCTION-SHAPED for the sweep surface: the retired hook-era writer
    # keyed convention_count_sess_<ZMEM_SESSION>, exactly the seeded shape.
    # The hermes-family production pairing (payload session id vs
    # hermes_convention_count_<sid>) is pinned end-to-end by
    # HermesProductionBoundTest below through the real CLI entry.
    def test_sweep_prunes_and_upgrades_convention_meta(self):
        """DISCRIMINATING — at base every convention row survives, nothing
        is upgraded, and no summary line exists."""
        seeds = self._seed_meta()
        r = self._sweep()
        self.assertEqual(r.returncode, 0,
                         f"sweep must exit 0 (fail-open reaper); observed:\n"
                         f"{_excerpt(r)}")
        meta = self._meta_values()
        # (a) exact survivor set over the TRUE-PREFIX family: exactly the
        # three retained keys; the near-miss keys are not family members.
        surviving_convention = sorted(
            k for k in meta if _is_convention_family(k))
        self.assertEqual(
            surviving_convention, sorted(RETAINED_KEYS),
            f"C4: the convention-family survivor set must be exactly "
            f"{sorted(RETAINED_KEYS)} (removed: {sorted(REMOVED_KEYS)}; the "
            f"near-miss keys are NOT family members). Observed survivors: "
            f"{surviving_convention!r}\nfull meta keys: {sorted(meta)!r}\n"
            f"sweep output:\n{_excerpt(r)}")
        for key in REMOVED_KEYS:
            self.assertNotIn(
                key, meta,
                f"C4: convention meta row {key!r} must be pruned by sweep "
                f"--max-age-days 7. Observed meta keys: {sorted(meta)!r}\n"
                f"sweep output:\n{_excerpt(r)}")
        # Fresh JSON, malformed, near-miss, unrelated rows: byte-identical
        # (only the CURRENT-SESSION legacy row is rewritten).
        for key in (HERMES_FRESH, HERMES_MALFORMED) + NEAR_MISS_KEYS \
                + UNRELATED_KEYS:
            self.assertEqual(
                meta.get(key), seeds[key],
                f"C4: meta row {key!r} must be byte-identical after sweep; "
                f"seeded {seeds[key]!r}, observed {meta.get(key)!r}.\n"
                f"sweep output:\n{_excerpt(r)}")
        # The CURRENT-SESSION legacy row: RETAINED and UPGRADED to canonical
        # JSON with count UNCHANGED (9) and last_seen a fresh ISO-Z stamp
        # (#128 AC2 / REVISION 3) — it must NOT stay byte-identical '9'.
        raw = meta.get(LAUNCHER_CURRENT_LEGACY)
        self.assertNotEqual(
            raw, "9",
            f"C4: the current-session legacy row must be UPGRADED to "
            f"canonical JSON in the real sweep, not left as the bare "
            f"integer '9'. Observed raw value: {raw!r}.\n"
            f"sweep output:\n{_excerpt(r)}")
        try:
            upgraded = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            self.fail(
                f"C4: the upgraded current-session row must parse as JSON; "
                f"observed raw {raw!r}.\nsweep output:\n{_excerpt(r)}")
        self.assertIsInstance(
            upgraded, dict,
            f"C4: the upgraded current-session row must be a JSON object; "
            f"observed {raw!r}.")
        self.assertEqual(
            upgraded.get("count"), 9,
            f"C4: the upgrade must keep count UNCHANGED (9); observed "
            f"{upgraded!r} from raw {raw!r}.")
        last_seen = upgraded.get("last_seen")
        self.assertIsInstance(
            last_seen, str,
            f"C4: upgraded last_seen must be a string; observed "
            f"{last_seen!r} in {upgraded!r}.")
        self.assertRegex(
            str(last_seen), ISO_Z_RE,
            f"C4: upgraded last_seen must be ISO-8601 Z; observed "
            f"{last_seen!r} in {upgraded!r}.")
        drift = abs(_epoch_of_iso_z(last_seen)
                    - datetime.now(timezone.utc).timestamp())
        self.assertLessEqual(
            drift, UPGRADE_TOLERANCE_S,
            f"C4: upgraded last_seen must be close to now (tolerance "
            f"+/-{UPGRADE_TOLERANCE_S}s); observed {last_seen!r} "
            f"({drift:.0f}s off).")
        # (b) the stderr summary with the census's exact counts.
        self._assert_summary_counts(r.stderr, "real sweep")

    def test_sweep_dry_run_deletes_nothing(self):
        """--dry-run must never delete or rewrite (the current-session
        legacy row stays byte-identical '9', the malformed row stays
        byte-identical) and must ANNOUNCE what it would prune with the SAME
        classification counts as the real run. Row presence is
        preserving-at-base; the announcement and counts are discriminating
        (no such stderr text exists at base)."""
        seeds = self._seed_meta()
        r = self._sweep(dry_run=True)
        self.assertEqual(r.returncode, 0,
                         f"sweep --dry-run must exit 0; observed:\n"
                         f"{_excerpt(r)}")
        meta = self._meta_values()
        for key, seeded in seeds.items():
            self.assertIn(
                key, meta,
                f"C4 dry-run: {key!r} must still be present after "
                f"sweep --dry-run (a dry run deletes NOTHING). Observed "
                f"meta keys: {sorted(meta)!r}\nsweep output:\n{_excerpt(r)}")
            self.assertEqual(
                meta.get(key), seeded,
                f"C4 dry-run: {key!r} must be byte-identical after "
                f"--dry-run (no upgrades, no rewrites); seeded {seeded!r}, "
                f"observed {meta.get(key)!r}.")
        self.assertIn(
            "would prune", r.stderr,
            f"C4 dry-run: sweep's stderr must announce what it WOULD prune "
            f"('would prune'); observed stderr:\n{r.stderr!r}\n"
            f"stdout:\n{r.stdout!r}")
        self._assert_summary_counts(r.stderr, "dry-run sweep")

    def test_absent_store_skip_non_creating(self):
        """PRESERVING — a missing store file is skipped fail-open (rc 0) and
        the sweep NEVER materializes the store (non-creating open)."""
        empty_dir = self.tmp / "emptydir"
        empty_dir.mkdir()
        missing = empty_dir / "store.sqlite"
        env = dict(self.env)
        env["ZMEM_STORE"] = str(missing)
        r = self._run("sweep", "--max-age-days", "7", env=env)
        self.assertEqual(r.returncode, 0,
                         f"sweep over an absent store must exit 0 "
                         f"(fail-open skip); observed:\n{_excerpt(r)}")
        self.assertFalse(
            missing.exists(),
            f"C4: sweep must not CREATE the store file when it is absent "
            f"(non-creating open). Observed {missing} exists after the "
            f"run.\n{_excerpt(r)}")

    def test_locked_store_skips_meta_prune_but_file_sweep_proceeds(self):
        """PRESERVING at base (the base sweep is file-only: it never opens the
        store, so rc 0 + sentinel removal + untouched meta rows hold trivially)
        — kept as the post-fix locked-skip pin: when another process holds an
        EXCLUSIVE transaction on the store, the sweep must STILL exit 0, the
        FILE sweep must still reap stale sentinels, and the meta prune must be
        SKIPPED (every meta row byte-identical, the current-session legacy '9'
        included — no upgrade, no delete)."""
        self._seed_meta()
        before = self._meta_values()
        sentinel = self._stale_sentinel()
        lock_conn = sqlite3.connect(str(self.store))
        lock_conn.isolation_level = None
        try:
            lock_conn.execute("BEGIN EXCLUSIVE")  # held across the child
            r = self._sweep()
        finally:
            lock_conn.close()
        self.assertEqual(r.returncode, 0,
                         f"C4 (locked store): sweep must exit 0 (fail-open "
                         f"skip of the locked store's meta prune); "
                         f"observed:\n{_excerpt(r)}")
        self.assertFalse(
            sentinel.exists(),
            f"C4 (locked store): the FILE sweep must proceed while the store "
            f"is locked — the stale sentinel {sentinel.name!r} under ZMEM_DATA "
            f"was NOT removed.\nsweep output:\n{_excerpt(r)}")
        self._assert_meta_byte_identical(
            before, "locked store: meta prune must be skipped")

    def test_newer_schema_store_skips_meta_prune(self):
        """PRESERVING at base (the base sweep never touches meta) — kept as
        the post-fix pin: a store whose schema_version ('9999') is NEWER than
        the supported one must NEVER be pruned (a newer-than-supported store
        could carry unknown row semantics); every meta row — the stale legacy
        counters included — stays byte-identical."""
        self._seed_meta()
        self._set_meta("schema_version", "9999")
        before = self._meta_values()
        r = self._sweep()
        self.assertEqual(r.returncode, 0,
                         f"C4 (newer schema): sweep must exit 0 (skip the "
                         f"meta prune, never crash); observed:\n"
                         f"{_excerpt(r)}")
        self._assert_meta_byte_identical(
            before, "newer schema: the pruner must refuse the store")

    def test_unreadable_store_skips_meta_prune_file_sweep_proceeds(self):
        """PRESERVING at base (the base sweep is file-only) — kept as the
        post-fix pin: ZMEM_STORE pointing at a NON-sqlite garbage file must be
        skipped fail-open (rc 0), the file sweep still reaps the stale
        sentinel, and the garbage file is NEVER rewritten or created over
        (byte-identical)."""
        sentinel = self._stale_sentinel(".convention-commit-prompted-r3b")
        garbage = self.tmp / "garbage-store.sqlite"
        garbage.write_bytes(b"this is not a sqlite database \x00\x01\xfe" * 4)
        garbage_before = garbage.read_bytes()
        env = dict(self.env)
        env["ZMEM_STORE"] = str(garbage)
        r = self._run("sweep", "--max-age-days", "7", env=env)
        self.assertEqual(r.returncode, 0,
                         f"C4 (unreadable store): sweep must exit 0 "
                         f"(fail-open skip); observed:\n{_excerpt(r)}")
        self.assertFalse(
            sentinel.exists(),
            f"C4 (unreadable store): the FILE sweep must proceed despite the "
            f"garbage store — the stale sentinel {sentinel.name!r} was NOT "
            f"removed.\nsweep output:\n{_excerpt(r)}")
        self.assertEqual(
            garbage.read_bytes(), garbage_before,
            f"C4 (unreadable store): the garbage store file must be "
            f"byte-identical (never rewritten, never created over).")


class DirectPrunerTest(MetaTableBoundBase):
    """Direct-function legs against the planned F4 seam
    `storelib.backup.prune_convention_meta(store_path, cutoff_epoch=...,
    current_session=..., dry_run=...)`. At the pre-fix tree the function
    does not exist; each leg's readable failure IS its discriminating
    verdict."""

    def _prune(self):
        """Import storelib.backup fresh (pinned module env) and return
        prune_convention_meta, failing READABLY when the seam is absent."""
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        for key in [key for key in list(sys.modules)
                    if key == "storelib" or key.startswith("storelib.")]:
            del sys.modules[key]
        try:
            from storelib import backup as zbackup  # noqa: PLC0415
        except Exception as exc:  # pragma: no cover - broken tree only
            self.fail(
                f"C4 direct: storelib.backup is unimportable in-process "
                f"({type(exc).__name__}: {exc}).")
        prune = getattr(zbackup, "prune_convention_meta", None)
        if prune is None:
            self.fail(
                "C4 direct: storelib.backup.prune_convention_meta does not "
                "exist at the pre-fix tree (the planned F4 seam — "
                "prune_convention_meta(store_path, cutoff_epoch=..., "
                "current_session=..., dry_run=...)). Discriminating "
                "failure: the function is absent.")
        return prune

    def test_exact_cutoff_retention_strict_less(self):
        """A hermes JSON row whose last_seen is EXACTLY the cutoff instant
        survives (strict <); one second older is removed."""
        now_epoch = int(datetime.now(timezone.utc).timestamp())
        at_cutoff = f"hermes_convention_count_11111111-2222-4333-8444-55556666771a"
        one_before = f"hermes_convention_count_11111111-2222-4333-8444-55556666771b"
        self._set_meta(at_cutoff, json.dumps(
            {"count": 4, "last_seen": _iso_z_of_epoch(now_epoch)}))
        self._set_meta(one_before, json.dumps(
            {"count": 6, "last_seen": _iso_z_of_epoch(now_epoch - 1)}))
        prune = self._prune()
        prune(str(self.store), cutoff_epoch=now_epoch,
              current_session="", dry_run=False)
        meta = self._meta_values()
        self.assertIn(
            at_cutoff, meta,
            f"C4 direct: a last_seen EXACTLY at the cutoff instant must be "
            f"RETAINED (strict <). Observed meta keys: {sorted(meta)!r} — "
            f"the at-cutoff row vanished.")
        self.assertNotIn(
            one_before, meta,
            f"C4 direct: a last_seen one second before the cutoff must be "
            f"REMOVED. Observed meta keys: {sorted(meta)!r}.")

    def test_rollback_atomic_no_partial_deletes(self):
        """A BEFORE-DELETE trigger aborting mid-prune must leave EVERY
        seeded row present (atomic rollback: zero partial deletes)."""
        seeds = {
            LEGACY_PLAIN: "7",
            LEGACY_SESS: "3",
            HERMES_LEGACY_INT: "12",
            HERMES_MALFORMED: MALFORMED_VALUE,
            "unrelated_note": "x",
        }
        for key, value in seeds.items():
            self._set_meta(key, value)
        conn = sqlite3.connect(str(self.store))
        try:
            conn.execute(
                "CREATE TRIGGER block_meta_delete BEFORE DELETE ON meta "
                "BEGIN SELECT RAISE(ABORT, 'blocked'); END;")
            conn.commit()
        finally:
            conn.close()
        prune = self._prune()
        raised: Exception | None = None
        try:
            prune(str(self.store),
                  cutoff_epoch=datetime.now(timezone.utc).timestamp(),
                  current_session="", dry_run=False)
        except Exception as exc:  # returns OR raises cleanly — either way
            raised = exc  # the store must be unchanged
        meta = self._meta_values()
        missing = sorted(k for k in seeds if k not in meta)
        changed = sorted(k for k in seeds if meta.get(k) != seeds[k])
        self.assertEqual(
            (missing, changed), ([], []),
            f"C4 direct: an aborted prune must roll back ATOMICALLY — "
            f"observed missing={missing!r} changed={changed!r} (the trigger "
            f"raised {raised!r}; zero partial deletes may survive). "
            f"Observed meta: {sorted(meta)!r}")
        # The trigger itself must survive too (the pruner never "cleans up"
        # schema objects to escape the abort).
        conn = sqlite3.connect(str(self.store))
        try:
            trig = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
                "AND name='block_meta_delete'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(trig, 1, "the blocking trigger must still exist")

    def test_injected_now_upgrades_retained_legacy_deterministically(self):
        """DISCRIMINATING — the post-fix signature carries an INJECTED CLOCK
        keyword named exactly `now_epoch`; the current-session upgrade's
        last_seen must be EXACTLY iso(now_epoch) (deterministic, not ~now).
        At the pre-fix tree prune_convention_meta does not exist, so
        self._prune() fails READABLY — that absence is this leg's verdict."""
        # The injected-clock keyword is `now_epoch` (post-fix signature:
        # prune_convention_meta(store_path, cutoff_epoch=..., current_session=...,
        # dry_run=..., now_epoch=<epoch>)).
        key = f"hermes_convention_count_{INJECTED_NOW_SESSION}"
        self._set_meta(key, "9")
        expected_last_seen = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(INJECTED_NOW_EPOCH))
        prune = self._prune()
        # --dry-run FIRST with the SAME args: the legacy row stays the raw
        # byte-identical '9' (a dry run never upgrades, never rewrites).
        prune(str(self.store), cutoff_epoch=INJECTED_NOW_EPOCH - 3600,
              current_session=INJECTED_NOW_SESSION, dry_run=True,
              now_epoch=INJECTED_NOW_EPOCH)
        self.assertEqual(
            self._meta_values().get(key), "9",
            f"C4 direct: --dry-run with the injected clock must leave the "
            f"current-session legacy row byte-identical '9'; observed "
            f"{self._meta_values().get(key)!r}.")
        # Real run: RETAINED and UPGRADED to canonical JSON whose last_seen is
        # EXACTLY the ISO-8601 Z rendering of now_epoch (count unchanged, 9).
        prune(str(self.store), cutoff_epoch=INJECTED_NOW_EPOCH - 3600,
              current_session=INJECTED_NOW_SESSION, dry_run=False,
              now_epoch=INJECTED_NOW_EPOCH)
        raw = self._meta_values().get(key)
        try:
            upgraded = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            self.fail(
                f"C4 direct: the current-session legacy row must upgrade to "
                f"JSON under the injected clock; observed raw {raw!r}.")
        self.assertIsInstance(
            upgraded, dict,
            f"C4 direct: upgraded row must be a JSON object; observed "
            f"{raw!r}.")
        self.assertEqual(
            upgraded.get("count"), 9,
            f"C4 direct: the injected-clock upgrade keeps count UNCHANGED "
            f"(9); observed {upgraded!r} from raw {raw!r}.")
        self.assertEqual(
            upgraded.get("last_seen"), expected_last_seen,
            f"C4 direct: last_seen must be EXACTLY the ISO-8601 Z rendering "
            f"of the injected now_epoch={INJECTED_NOW_EPOCH} "
            f"({expected_last_seen!r}) — deterministically, never ~now. "
            f"Observed {upgraded.get('last_seen')!r} in {upgraded!r}.")


class CadenceSummaryTest(MetaTableBoundBase):
    """C4 cadence — `store.py session-cadence` (no --session-id argv exists;
    the session is threaded via ZMEM_SESSION) must carry the exact meta
    summary fragment and upgrade the current-session legacy row."""

    def test_session_cadence_reports_meta_summary_and_upgrades(self):
        """DISCRIMINATING — at base the summary has no meta fragment and
        nothing is upgraded."""
        self._seed_meta()
        env = dict(self.env)
        env["ZMEM_SESSION"] = CURRENT_SESSION
        r = self._run("session-cadence", env=env, timeout=300)
        self.assertEqual(r.returncode, 0,
                         f"session-cadence must exit 0 when its ops succeed; "
                         f"observed:\n{_excerpt(r)}")
        combined = r.stdout + "\n--stderr--\n" + r.stderr
        self.assertIn(
            EXPECTED_SUMMARY_FRAGMENT, combined,
            f"C4 cadence: the session-cadence summary must carry exactly "
            f"'{EXPECTED_SUMMARY_FRAGMENT}'. Observed output:\n{combined!r}")
        # The current-session legacy row was upgraded by the cadence's sweep
        # step (session threaded from the cadence's session id).
        raw = self._meta_values().get(LAUNCHER_CURRENT_LEGACY)
        self.assertNotEqual(
            raw, "9",
            f"C4 cadence: the current-session legacy row must be upgraded "
            f"to canonical JSON; observed raw {raw!r}.")
        try:
            upgraded = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            self.fail(f"C4 cadence: upgraded row must parse as JSON; "
                      f"observed {raw!r}.")
        self.assertEqual(
            upgraded.get("count"), 9,
            f"C4 cadence: the upgrade keeps count UNCHANGED (9); observed "
            f"{upgraded!r}.")
        self.assertRegex(str(upgraded.get("last_seen") or ""), ISO_Z_RE,
                         f"C4 cadence: upgraded last_seen must be ISO-Z; "
                         f"observed {upgraded!r}.")

    def test_cadence_sweep_runs_before_connect_on_locked_store(self):
        """PRESERVING at base — the cadence already runs its sweep BEFORE
        connect()/_prepare_store (PRR-004), so this leg passes today and pins
        the ordering: with an EXCLUSIVE transaction held on the store and a
        stale sentinel present, the sentinel must be reaped REGARDLESS of the
        cadence's own exit code (the post-connect ops may fail or block on
        the lock — that is fine; the sweep must never wait behind them).

        Same argv as the discriminating cadence leg above: `session-cadence`
        with the session threaded via ZMEM_SESSION (no --session-id argv
        exists)."""
        sentinel = self._stale_sentinel(".convention-commit-prompted-r3c")
        env = dict(self.env)
        env["ZMEM_SESSION"] = CURRENT_SESSION
        lock_conn = sqlite3.connect(str(self.store))
        lock_conn.isolation_level = None
        try:
            lock_conn.execute("BEGIN EXCLUSIVE")  # held across the child
            try:
                r = self._run("session-cadence", env=env, timeout=300)
            except subprocess.TimeoutExpired:
                # The post-connect steps blocking on the lock is acceptable —
                # the sweep already ran before connect (the ordering this leg
                # pins); assert on the sentinel below either way.
                r = None
        finally:
            lock_conn.close()
        self.assertFalse(
            sentinel.exists(),
            f"C4 cadence ordering: the cadence's sweep step runs BEFORE its "
            f"store connect, so the stale sentinel {sentinel.name!r} must be "
            f"reaped even while the store is locked (cadence exit code is "
            f"immaterial here; observed rc="
            f"{r.returncode if r is not None else 'timeout'}).")


class CounterValueTest(MetaTableBoundBase):
    """C5 — the hermes ring counter value is a JSON object with a count and
    a valid ISO-8601 Z last_seen heartbeat.

    Driven through the REAL hidden CLI `store.py hermes-convention`, the same
    argv/stdin the Hermes hook fires (storelib/cli.py:cmd_hermes_convention,
    the write at cli.py:2139)."""

    def _increment(self, session: str) -> subprocess.CompletedProcess:
        payload = json.dumps({"payload": {"session_id": session}})
        return self._run("hermes-convention", stdin=payload)

    def _counter_value(self, session: str) -> str:
        return self._meta_values().get(
            f"hermes_convention_count_{session}", "")

    def _parse_counter(self, session: str, context: str) -> dict:
        raw = self._counter_value(session)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            self.fail(
                f"C5 ({context}): hermes_convention_count_{session} must be "
                f"a JSON object after an increment; observed unparseable "
                f"value {raw!r} ({exc}).")
        self.assertIsInstance(
            value, dict,
            f"C5 ({context}): hermes_convention_count_{session} must be a "
            f"JSON OBJECT after an increment; observed {raw!r} "
            f"(parsed to {type(value).__name__}).")
        return value

    def test_legacy_integer_counter_upgraded_on_increment(self):
        """DISCRIMINATING — at base the value stays a bare integer ('4')."""
        self._set_meta(f"hermes_convention_count_{LEGACY_SESSION}", "3")
        r = self._increment(LEGACY_SESSION)
        self.assertEqual(r.returncode, 0,
                         f"hermes-convention must exit 0; observed:\n"
                         f"{_excerpt(r)}")
        value = self._parse_counter(LEGACY_SESSION,
                                    "legacy integer '3' + one increment")
        self.assertEqual(
            value.get("count"), 4,
            f"C5: a counter whose value was the legacy integer '3' must "
            f"read count==4 after one increment. Observed value: "
            f"{value!r} (raw {self._counter_value(LEGACY_SESSION)!r})")
        last_seen = value.get("last_seen")
        self.assertIsInstance(
            last_seen, str,
            f"C5: last_seen must be a string; observed {last_seen!r} in "
            f"{value!r}")
        self.assertRegex(
            str(last_seen), ISO_Z_RE,
            f"C5: last_seen must be an ISO-8601 Z timestamp "
            f"(YYYY-MM-DDTHH:MM:SSZ); observed {last_seen!r} in {value!r}")

    def test_fresh_counter_increments_back_to_back(self):
        """DISCRIMINATING — at base the values are bare integers ('1','2').

        Two increments back-to-back; count must go 1 -> 2 monotonically and
        each last_seen must be a valid ISO-8601 Z string. Deliberately NO
        assertion that last_seen strictly advances: the writer may land both
        increments inside the same wall-clock second, and over-fitting the
        timestamp's advance would be wrong."""
        r = self._increment(FRESH_SESSION)
        self.assertEqual(r.returncode, 0,
                         f"hermes-convention must exit 0; observed:\n"
                         f"{_excerpt(r)}")
        first = self._parse_counter(FRESH_SESSION, "first fresh increment")
        _count = first.get("count")
        self.assertIsInstance(_count, int)
        self.assertFalse(
            isinstance(_count, bool),
            "count must be a real int, not a bool (bool is an int subclass; "
            "review round PRR-010)")
        self.assertEqual(
            _count, 1,
            f"C5: a fresh counter's first increment must read count==1. "
            f"Observed: {first!r} (raw "
            f"{self._counter_value(FRESH_SESSION)!r})")
        self.assertRegex(str(first.get("last_seen") or ""), ISO_Z_RE,
                         f"C5: first increment's last_seen must be a valid "
                         f"ISO-8601 Z string; observed {first!r}")
        r = self._increment(FRESH_SESSION)
        self.assertEqual(r.returncode, 0,
                         f"hermes-convention must exit 0; observed:\n"
                         f"{_excerpt(r)}")
        second = self._parse_counter(FRESH_SESSION, "second fresh increment")
        self.assertEqual(
            second.get("count"), 2,
            f"C5: the back-to-back second increment must read count==2 "
            f"(monotonic 1 -> 2). Observed: {second!r} (raw "
            f"{self._counter_value(FRESH_SESSION)!r})")
        self.assertRegex(str(second.get("last_seen") or ""), ISO_Z_RE,
                         f"C5: second increment's last_seen must be a valid "
                         f"ISO-8601 Z string; observed {second!r}")

    def test_concurrent_increments_no_lost_update(self):
        """DISCRIMINATING — six concurrent increments must land count==6
        exactly (no lost update) as JSON. At base the value is a bare
        integer (observed value printed)."""
        n = 6
        results: list[subprocess.CompletedProcess] = [None] * n
        errors: list[Exception] = []

        def _worker(i: int) -> None:
            try:
                results[i] = self._increment(CONCURRENT_SESSION)
            except Exception as exc:  # pragma: no cover - harness guard
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(i,))
                   for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=180)
        self.assertFalse(errors, f"increment subprocesses errored: {errors!r}")
        for i, res in enumerate(results):
            self.assertIsNotNone(
                res, f"increment subprocess {i} never completed")
            self.assertEqual(
                res.returncode, 0,
                f"increment subprocess {i} exited {res.returncode}: "
                f"{res.stderr[-300:]!r}")
        value = self._parse_counter(CONCURRENT_SESSION,
                                    f"{n} concurrent increments")
        self.assertEqual(
            value.get("count"), n,
            f"C5: {n} concurrent increments must land count=={n} exactly "
            f"(one transactional upsert per increment — no lost update). "
            f"Observed: {value!r} (raw "
            f"{self._counter_value(CONCURRENT_SESSION)!r})")

    def test_malformed_value_increment_treated_as_zero(self):
        """DISCRIMINATING — a malformed value must be treated as 0, so the
        next increment reads JSON count==1. At base CAST('not-json{' AS
        INTEGER)+1 yields the bare integer '1'."""
        self._set_meta(f"hermes_convention_count_{MALFORMED_WRITE_SESSION}",
                       MALFORMED_VALUE)
        r = self._increment(MALFORMED_WRITE_SESSION)
        self.assertEqual(r.returncode, 0,
                         f"hermes-convention must exit 0; observed:\n"
                         f"{_excerpt(r)}")
        value = self._parse_counter(
            MALFORMED_WRITE_SESSION, "malformed value + one increment")
        _count = value.get("count")
        self.assertIsInstance(_count, int)
        self.assertFalse(
            isinstance(_count, bool),
            "count must be a real int, not a bool (bool is an int subclass; "
            "review round PRR-010)")
        self.assertEqual(
            _count, 1,
            f"C5: a malformed counter value must be treated as 0 — the "
            f"next increment reads JSON count==1. Observed: {value!r} (raw "
            f"{self._counter_value(MALFORMED_WRITE_SESSION)!r})")

    def test_wrong_typed_json_increment_treated_as_malformed(self):
        """DISCRIMINATING — a JSON object with wrong-typed fields
        (count str, last_seen int) is MALFORMED per the F3 parser contract;
        the next increment reads JSON count==1."""
        self._set_meta(f"hermes_convention_count_{WRONG_TYPED_SESSION}",
                       WRONG_TYPED_JSON)
        r = self._increment(WRONG_TYPED_SESSION)
        self.assertEqual(r.returncode, 0,
                         f"hermes-convention must exit 0; observed:\n"
                         f"{_excerpt(r)}")
        value = self._parse_counter(
            WRONG_TYPED_SESSION, "wrong-typed JSON + one increment")
        _count = value.get("count")
        self.assertIsInstance(_count, int)
        self.assertFalse(
            isinstance(_count, bool),
            "count must be a real int, not a bool (bool is an int subclass; "
            "review round PRR-010)")
        self.assertEqual(
            _count, 1,
            f"C5: a wrong-typed JSON counter must be treated as malformed "
            f"(0) — the next increment reads JSON count==1. Observed: "
            f"{value!r} (raw "
            f"{self._counter_value(WRONG_TYPED_SESSION)!r})")

    def test_writer_rollback_on_failed_upsert(self):
        """PRESERVING at base — the handler (storelib/cli.py
        cmd_hermes_convention) already wraps the upsert in `except Exception:
        conn.rollback()` and fail-opens to `{}`. This leg pins that contract
        against a blocked meta table: with a BEFORE INSERT trigger ABORTing
        the `hermes_convention_count_%` key prefix (plus the BEFORE UPDATE
        blocker for the conflict arm), the CLI must STILL exit 0 printing
        `{}` AND leave NOTHING partially committed for the session (neither
        the counter row nor the `hermes_pending_convention_*` follower)."""
        conn = sqlite3.connect(str(self.store))
        try:
            conn.execute(
                "CREATE TRIGGER block_meta_upd BEFORE UPDATE ON meta "
                "BEGIN SELECT RAISE(ABORT, 'blocked'); END;")
            conn.execute(
                "CREATE TRIGGER block_conv_ins BEFORE INSERT ON meta "
                "WHEN NEW.key LIKE 'hermes_convention_count_%' "
                "BEGIN SELECT RAISE(ABORT, 'blocked'); END;")
            conn.commit()
        finally:
            conn.close()
        r = self._increment(ROLLBACK_SESSION)
        self.assertEqual(
            r.returncode, 0,
            f"C5 rollback: hermes-convention must fail-open to exit 0 even "
            f"when the meta upsert is aborted; observed:\n{_excerpt(r)}")
        self.assertEqual(
            r.stdout.strip(), "{}",
            f"C5 rollback: the handler must print the empty object `{{}}` "
            f"(fail-open); observed stdout {r.stdout!r}.\n{_excerpt(r)}")
        meta = self._meta_values()
        leaked = sorted(k for k in meta if ROLLBACK_SESSION in k)
        self.assertEqual(
            leaked, [],
            f"C5 rollback: a failed upsert must roll back ATOMICALLY — no "
            f"meta row for session {ROLLBACK_SESSION} may survive (no "
            f"hermes_convention_count_*, no hermes_pending_convention_*); "
            f"observed leaked keys {leaked!r}.\nfull meta keys: "
            f"{sorted(meta)!r}")


class VerifyHookParityTest(MetaTableBoundBase):
    """C5 parity — the REAL verify-hook reader
    (hermes-plugin/hooks/zmem-hermes-verify.py) must parse the JSON counter
    shape. The hook reads the counter with `str(value).isdigit()`, so a JSON
    value collapses to 0 and the verify nudge silently dies — the parity gap
    this check pins.

    Counter values are written DIRECTLY via sqlite (simulating the post-fix
    writer state) so the hook's parsing is what's under test, isolated from
    the writer legs above."""

    def _run_hook(self, session: str) -> subprocess.CompletedProcess:
        return self._run_argv(
            [PYTHON, "-X", "utf8", str(VERIFY_HOOK)],
            stdin=json.dumps({"session_id": session}), timeout=60)

    def test_json_counter_treated_nonzero(self):
        """DISCRIMINATING — at base isdigit() on the JSON value is False,
        count parses as 0, and the hook takes the empty path (emits `{}`),
        killing the verify nudge."""
        session = HOOK_JSON_SESSION
        now = _now_iso_z()
        self._set_meta(f"hermes_convention_count_{session}",
                       json.dumps({"count": 3, "last_seen": now}))
        r = self._run_hook(session)
        self.assertEqual(r.returncode, 0,
                         f"verify hook must exit 0; observed:\n"
                         f"{_excerpt(r)}")
        self.assertIn(
            '"action": "continue"', r.stdout,
            f"C5 parity: the verify hook must take the NONZERO path for a "
            f"JSON counter with count>0 (seeded "
            f"{{'count': 3, 'last_seen': {now}}}) — no prior prompted "
            f"marker, no captured lesson — and emit the continue nudge. "
            f"Observed the empty path instead. stdout:\n{r.stdout!r}\n"
            f"stderr:\n{r.stderr!r}")

    def test_legacy_int_counter_still_nudges(self):
        """PRESERVING — a legacy bare integer '3' keeps nudging (count>0)
        in both the base and post-fix worlds."""
        session = HOOK_LEGACY_INT_SESSION
        self._set_meta(f"hermes_convention_count_{session}", "3")
        r = self._run_hook(session)
        self.assertEqual(r.returncode, 0,
                         f"verify hook must exit 0; observed:\n"
                         f"{_excerpt(r)}")
        self.assertIn(
            '"action": "continue"', r.stdout,
            f"C5 parity: a legacy bare-int counter '3' (count>0) must keep "
            f"taking the nudge path. Observed stdout:\n{r.stdout!r}\n"
            f"stderr:\n{r.stderr!r}")

    def test_malformed_counter_treated_as_zero(self):
        """PRESERVING — a malformed counter value is treated as 0 (empty
        emission) in both the base and post-fix worlds."""
        session = HOOK_MALFORMED_SESSION
        self._set_meta(f"hermes_convention_count_{session}",
                       MALFORMED_VALUE)
        r = self._run_hook(session)
        self.assertEqual(r.returncode, 0,
                         f"verify hook must exit 0; observed:\n"
                         f"{_excerpt(r)}")
        self.assertEqual(
            r.stdout.strip(), "{}",
            f"C5 parity: a malformed counter value ({MALFORMED_VALUE!r}) "
            f"must be treated as count 0 — the hook emits the empty object "
            f"and never nudges. Observed stdout:\n{r.stdout!r}\n"
            f"stderr:\n{r.stderr!r}")

    def test_parser_lockstep_matrix(self):
        """DISCRIMINATING — schema_meta.parse_convention_count (the F3
        parser) and the verify hook's LOCAL TWIN parser must agree on every
        input. The hook's twin MUST be named `_parse_convention_count` in
        zmem-hermes-verify.py (the degraded-twin pattern, REVISION 3 F3) so
        this matrix can bind it. At the pre-fix tree both are absent."""
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        try:
            import schema_meta  # noqa: PLC0415
        except ImportError as exc:
            self.fail(
                f"C5 lockstep: schema_meta.parse_convention_count does not "
                f"exist at the pre-fix tree (the planned F3 parser module "
                f"at skills/memory/scripts/schema_meta.py). Discriminating "
                f"failure — ImportError: {exc}.")
        parse = getattr(schema_meta, "parse_convention_count", None)
        self.assertIsNotNone(
            parse, "C5 lockstep: schema_meta exists but carries no "
                   "parse_convention_count.")
        # Load the hook module BY PATH and bind its local twin. The twin's
        # REQUIRED name is `_parse_convention_count` — the implementer names
        # it so (REVISION 3 F3 degraded-twin pattern).
        spec = importlib.util.spec_from_file_location(
            "zmem_hermes_verify_twin", VERIFY_HOOK)
        self.assertIsNotNone(spec, f"cannot load spec for {VERIFY_HOOK}")
        hook_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook_mod)
        twin = getattr(hook_mod, "_parse_convention_count", None)
        self.assertIsNotNone(
            twin,
            "C5 lockstep: the verify hook must carry its local twin parser "
            "named exactly `_parse_convention_count` (zmem-hermes-verify."
            "py); observed attribute absent at the pre-fix tree.")
        matrix = (
            "0", "3", "007", "-1", "",
            MALFORMED_VALUE,
            '{"count":5,"last_seen":"2026-01-01T00:00:00Z"}',
            '{"count":"x"}',
            '{"count":2}',
            "[]",
            "null",
            '{"count":-3,"last_seen":"x"}',
            # Impl-review round 2 finding 5: corrupt-but-shaped timestamps.
            # strptime ACCEPTS second=60 (POSIX leap seconds) and strftime
            # renders it back verbatim, so only the explicit range gates
            # reject it; the epoch parser would resolve it to its 0.0
            # failure sentinel. Both twins must treat these as malformed.
            '{"count":1,"last_seen":"2026-11-06T18:00:60Z"}',
            '{"count":1,"last_seen":"2026-02-30T00:00:00Z"}',
            '{"count":1,"last_seen":"2026-11-06T24:00:00Z"}',
            '{"count":1,"last_seen":""}',
            '{"count":1,"last_seen":5}',
        )
        for value in matrix:
            with self.subTest(value=value):
                expected = parse(value)
                observed = twin(value)
                self.assertEqual(
                    observed, expected,
                    f"C5 lockstep: the hook twin diverged from "
                    f"schema_meta.parse_convention_count on input "
                    f"{value!r}: schema_meta={expected!r} "
                    f"twin={observed!r}.")

    def test_hook_runs_without_storelib_available(self):
        """A copy of the verify hook in a scratch dir — its relative storelib
        resolution finds NOTHING (no skills/memory/scripts/host.py under the
        copy's parents[2], no ZMEM_HOME candidate, PYTHONPATH stripped), so
        the hook must run entirely on its inline degraded path (the resolver's
        env-var fallback). Law: it must NOT crash (rc 0, no Traceback — passes
        in both worlds) AND (post-fix) must still parse a JSON counter with
        count>0 and emit the continue nudge. DISCRIMINATING at base: the hook
        reads the counter with str(value).isdigit(), a JSON value collapses
        to 0, and it emits the empty `{}` instead of the nudge.

        `_zmem_hook_common.py` rides along — it is the hook's stdlib-only
        sibling import (NOT storelib); without the copy the hook dies at its
        own module import, which is not the law under test."""
        scratch = self.tmp / "hookcopy"
        scratch.mkdir()
        hook_copy = scratch / "zmem-hermes-verify.py"
        shutil.copy2(VERIFY_HOOK, hook_copy)
        shutil.copy2(VERIFY_HOOK.parent / "_zmem_hook_common.py",
                     scratch / "_zmem_hook_common.py")
        # Fixture sanity: the copy's resolver candidates genuinely lack
        # host.py/storelib (parents[2] of the COPY, and no ZMEM_HOME), AND
        # the subprocess cwd cannot rescue candidate 2 — the hook resolves
        # ZMEM_HOME-absent as Path("")/"skills/memory/scripts", which is
        # CWD-RELATIVE; without a cwd pin the repo checkout itself would
        # satisfy it and the inline degraded fallback would never run
        # (review round PRR-007: the un-pinned leg was sabotage-mutant
        # GREEN from the repo root).
        self.assertFalse(
            (hook_copy.parents[2] / "skills" / "memory" / "scripts"
             / "host.py").is_file(),
            f"fixture sanity: the scratch copy at {hook_copy} must NOT reach "
            f"a skills/memory/scripts tree via parents[2] "
            f"({hook_copy.parents[2]}).")
        hook_cwd = scratch / "cwd"
        hook_cwd.mkdir()
        self.assertFalse(
            (hook_cwd / "skills" / "memory" / "scripts" / "host.py").is_file(),
            "fixture sanity: the hook subprocess cwd must not resolve a "
            "skills/memory/scripts tree either.")
        session = HOOK_NOSTORELIB_SESSION
        self._set_meta(f"hermes_convention_count_{session}",
                       json.dumps({"count": 3, "last_seen": _now_iso_z()}))
        env = dict(self.env)
        env.pop("PYTHONPATH", None)
        env.pop("ZMEM_HOME", None)
        self.assertNotIn("ZMEM_HOME", env,
                         "fixture sanity: the hook copy's second resolver "
                         "candidate (ZMEM_HOME) must be absent.")
        r = self._run_argv([PYTHON, "-X", "utf8", str(hook_copy)],
                           stdin=json.dumps({"session_id": session}),
                           timeout=60, env=env, cwd=str(hook_cwd))
        # Passes in BOTH worlds: without storelib the hook must degrade, not
        # crash (rc 0, no Traceback — no ImportError leaking from a hard
        # storelib import).
        self.assertEqual(r.returncode, 0,
                         f"C5 no-storelib: the hook copy must exit 0 without "
                         f"storelib available; observed:\n{_excerpt(r)}")
        self.assertNotIn(
            "Traceback", r.stderr,
            f"C5 no-storelib: the hook copy must not crash (no ImportError/"
            f"Traceback) when its relative storelib resolution finds nothing. "
            f"Observed stderr:\n{r.stderr!r}")
        # DISCRIMINATING: count>0 must take the NONZERO nudge path even on
        # the degraded (no-storelib) path. At base isdigit() collapses the
        # JSON value to 0 and the hook emits the empty `{{}}`.
        self.assertIn(
            '"action": "continue"', r.stdout,
            f"C5 no-storelib: with a JSON counter count=3 seeded and no "
            f"storelib resolvable, the hook copy must STILL take the NONZERO "
            f"path and emit the continue nudge. Observed the empty path "
            f"instead. stdout:\n{r.stdout!r}\nstderr:\n{r.stderr!r}")


# ============================================================================
# Issue #446 AC6 / AC7 (contract D): production-shaped retention pairing and
# hermes-side bounding, both proven through the REAL production writer entry
# `store.py hermes-convention` (the same stdin-payload argv the Hermes hook
# fires — cmd_hermes_convention in storelib/cli.py). The implementer bounds
# the meta prune at a row-count CAP (target: 64 hermes convention rows per
# host, per the #446 AC2 "bound documented for that host profile" contract)
# and threads current_session = the hermes payload session id, so a LIVE
# hermes session's LEGACY bare-int row is RETAINED and UPGRADED (never
# deleted-and-recreated at count=1). Frozen at base c78a2d5: the production
# entry writes/updates the counter but never prunes, so the bound leg fails
# at base (row count stays at the seeded census size).
# ============================================================================


class HermesProductionBoundTest(MetaTableBoundBase):
    """#446 AC6 legs (1) and (2): the production hermes-convention entry
    BOUNDS hermes_convention_count_* rows and retain-and-upgrades the LIVE
    session's legacy row."""

    # The documented host-profile bound (issue #446 AC2 / trace contract D):
    # target cap 64 convention rows. The pinned assertion allows CAP + SLACK
    # (slack 8) so a newest-N or TTL implementation both satisfy the bound.
    CAP = 64
    SLACK = 8
    # Enough stale sessions to exceed any plausible cap AND to be TTL-stale
    # (legacy bare ints are prunable at their first post-upgrade sweep under
    # the prune_convention_meta law regardless of the cutoff).
    STALE_SESSIONS = 200
    LIVE_SESSION = "99998888-7777-6666-5555-4444333322f0"
    LIVE_KEY = f"hermes_convention_count_{LIVE_SESSION}"
    STALE_JSON_LAST_SEEN = "2026-01-01T00:00:00Z"  # far older than any TTL

    def _convention_rows(self) -> dict:
        return {k: v for k, v in self._meta_values().items()
                if k.startswith("hermes_convention_count_")}

    def _seed_census(self, live_value: str = "5") -> None:
        """STALE_SESSIONS non-current legacy/stale-JSON rows plus the LIVE
        session's LEGACY bare-int row, seeded by direct sqlite INSERT (the
        same seeding channel as the C4 census)."""
        stale_pairs = [
            (f"hermes_convention_count_99998888-7777-6666-5555-"
             f"{4333220000 + i:010d}", "1")
            if i % 2 == 0 else
            (f"hermes_convention_count_99998888-7777-6666-5555-"
             f"{4333220000 + i:010d}",
             json.dumps({"count": 2, "last_seen": self.STALE_JSON_LAST_SEEN}))
            for i in range(self.STALE_SESSIONS)
        ]
        conn = sqlite3.connect(str(self.store))
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                stale_pairs + [(self.LIVE_KEY, live_value)])
            conn.commit()
        finally:
            conn.close()

    def _drive_production_entry(self) -> subprocess.CompletedProcess:
        """Run the REAL hidden CLI the Hermes hook fires: one JSON payload on
        stdin carrying the live payload session id."""
        payload = json.dumps({"payload": {"session_id": self.LIVE_SESSION}})
        return self._run("hermes-convention", stdin=payload)

    def test_production_entry_bounds_convention_row_count(self):
        """AC6/AC7 leg (1): seeding many stale hermes sessions' rows then
        running the production entry BOUNDS the table. Frozen at base: the
        production entry never prunes, so every seeded row survives."""
        self._seed_census()
        before = len(self._convention_rows())
        self.assertGreaterEqual(
            before, self.STALE_SESSIONS,
            f"fixture sanity: {self.STALE_SESSIONS} convention rows must be "
            f"seeded before the drive; observed {before}")
        r = self._drive_production_entry()
        self.assertEqual(r.returncode, 0,
                         f"hermes-convention must exit 0 (a best-effort "
                         f"prune must never break the write); observed:\n"
                         f"{_excerpt(r)}")
        after = len(self._convention_rows())
        bound = self.CAP + self.SLACK
        self.assertLessEqual(
            after, bound,
            f"#446 AC7: running the production hermes-convention entry on a "
            f"host holding {before} hermes convention rows must bound the "
            f"table to <= cap {self.CAP} + slack {self.SLACK} = {bound} "
            f"rows; observed {after} rows surviving (no hermes-side bound "
            f"ran).")

    def test_live_legacy_row_retained_and_upgraded(self):
        """AC6 leg (2): a LIVE hermes session's LEGACY bare-int row
        (seeded directly in sqlite as '5') is RETAINED and UPGRADED to the
        canonical JSON shape with the count preserved (then incremented by
        the write) — NOT deleted. A wrong current_session threading deletes
        the legacy row (non-current legacy = stale) and the write recreates
        it at count=1, which the count==6 pin catches."""
        self._seed_census(live_value="5")
        r = self._drive_production_entry()
        self.assertEqual(r.returncode, 0,
                         f"hermes-convention must exit 0; observed:\n"
                         f"{_excerpt(r)}")
        rows = self._convention_rows()
        self.assertIn(
            self.LIVE_KEY, rows,
            f"#446 AC6: the LIVE session's legacy row must be RETAINED by "
            f"the production entry's prune (current_session = the hermes "
            f"payload session id); it was DELETED. Surviving rows: "
            f"{len(rows)}")
        try:
            value = json.loads(rows[self.LIVE_KEY])
        except json.JSONDecodeError as exc:
            self.fail(
                f"#446 AC6: the retained legacy row must be UPGRADED to the "
                f"canonical JSON shape; observed unparseable value "
                f"{rows[self.LIVE_KEY]!r} ({exc}).")
        self.assertIsInstance(value, dict,
                              f"upgraded value must be a JSON object; "
                              f"observed {rows[self.LIVE_KEY]!r}")
        self.assertEqual(
            value.get("count"), 6,
            f"#446 AC6: the live session's legacy '5' must be preserved "
            f"ACROSS the prune and incremented to count==6 by the write "
            f"(a deleted-then-recreated row reads count==1 — the retention "
            f"failure). Observed {value!r} (raw "
            f"{rows[self.LIVE_KEY]!r}).")
        self.assertRegex(str(value.get("last_seen") or ""), ISO_Z_RE,
                         f"upgraded last_seen must be ISO-8601 Z; observed "
                         f"{value!r}")

    def test_fresh_rows_above_cap_bounded_malformed_retained(self):
        """AC6 leg (3) — CHECK_WRONG amend (plan-critic r1 finding 6): CAP
        must be a NUMERIC bound on the DELETABLE population, not a TTL
        trigger. A census of FRESH non-current JSON rows (last_seen ~now,
        TTL-immune) exceeding the cap is bounded by row age (oldest
        pruned); malformed rows are NEVER deleted (#128 operator-inspection
        contract) and are excluded from the cap arithmetic. At base the
        production entry never prunes: every row survives -> RED."""
        fresh_n = 100
        malformed_n = 3
        now_iso = _now_iso_z()
        pairs = [
            (f"hermes_convention_count_bbbbcccc-dddd-eeee-ffff-"
             f"{2000000000 + i:010d}",
             json.dumps({"count": 2, "last_seen": now_iso}))
            for i in range(fresh_n)
        ]
        malformed_keys = [
            f"hermes_convention_count_bbbbcccc-dddd-eeee-ffff-"
            f"malformed-{i}" for i in range(malformed_n)
        ]
        pairs += [(k, MALFORMED_VALUE) for k in malformed_keys]
        pairs.append((self.LIVE_KEY, "5"))
        conn = sqlite3.connect(str(self.store))
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                pairs)
            conn.commit()
        finally:
            conn.close()
        r = self._drive_production_entry()
        self.assertEqual(r.returncode, 0,
                         f"hermes-convention must exit 0 (best-effort prune "
                         f"never breaks the write); observed:\n"
                         f"{_excerpt(r)}")
        rows = self._convention_rows()
        bound = self.CAP + self.SLACK
        self.assertLessEqual(
            len(rows), bound,
            f"#446 AC6: a FRESH census of {fresh_n} TTL-immune non-current "
            f"rows must still be bounded to <= cap {self.CAP} + slack "
            f"{self.SLACK} = {bound} (the cap is a numeric bound on the "
            f"deletable population, not a TTL trigger); observed "
            f"{len(rows)} rows surviving.")
        for key in malformed_keys:
            self.assertIn(
                key, rows,
                f"#446 AC6: malformed row {key!r} must be RETAINED (never "
                f"deleted, #128 operator-inspection contract); observed "
                f"survivors: {sorted(rows)[:10]}...")
        self.assertIn(self.LIVE_KEY, rows,
                      "the LIVE session's row must survive the cap prune")
        self.assertEqual(json.loads(rows[self.LIVE_KEY]).get("count"), 6,
                         "the live legacy '5' must be retained-and-upgraded "
                         "then incremented to 6 (same discriminator as "
                         "leg 2, under cap pressure)")

    # ---- wave-2 legs (plan-critic r2 findings 2+3+6); fixtures
    # corrected wave-3 (r3 findings N3-1/N3-2/N3-3): timestamps are
    # TTL-FRESH (relative to the test's UTC now — frozen 2026-01..04
    # stamps were beyond the 7-day TTL and would be deleted before cap
    # survivor selection, making the legs post-fix-impossible), and keys
    # are ANTI-CORRELATED with time (newest stamp <-> smallest key) so a
    # key-only sorter cannot pass. ----

    def _env_with_cap(self, cap: int) -> dict:
        env = dict(self.env)
        env["ZMEM_CONVENTION_ROW_CAP"] = str(cap)
        return env

    @staticmethod
    def _ttl_fresh_stamps(n: int, hours_back: int = 2) -> list:
        """n DISTINCT ISO-Z stamps ascending in time, all inside the
        sweep-default 7-day TTL (r3 N3-1): stamp i is now -
        (hours_back*(n-i)) hours."""
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        return [
            (now - timedelta(hours=hours_back * (n - i))).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            for i in range(n)
        ]

    def _seed_ordered_census(self, stamps, malformed_n=2):
        """Distinct TTL-fresh last_seen rows + live legacy '5' +
        malformed. Keys are ANTI-CORRELATED with time (r3 N3-2): the
        NEWEST stamp carries the SMALLEST key, so only a
        (bucket, last_seen, key) sorter picks the right survivors.
        Returns (keys_by_age_oldest_first, malformed_keys)."""
        n = len(stamps)
        # key index runs OPPOSITE to time: oldest stamp -> largest key
        keys = [
            f"hermes_convention_count_dddd0000-eeee-ffff-"
            f"{9000000000 + (n - 1 - i):010d}" for i in range(n)
        ]
        pairs = [
            (k, json.dumps({"count": 2, "last_seen": ts}))
            for k, ts in zip(keys, stamps)
        ]
        malformed_keys = [
            f"hermes_convention_count_dddd0000-eeee-ffff-bad-{i}"
            for i in range(malformed_n)
        ]
        pairs += [(k, MALFORMED_VALUE) for k in malformed_keys]
        pairs.append((self.LIVE_KEY, "5"))
        conn = sqlite3.connect(str(self.store))
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                pairs)
            conn.commit()
        finally:
            conn.close()
        return keys, malformed_keys

    def test_cap_prunes_oldest_first_deterministic(self):
        """Wave-2 leg (r2 finding 3): with a reduced cap, deletion is
        deterministic oldest-first — distinct last_seen rows are pruned
        oldest first, exactly the NEWEST (cap - 1) fresh rows survive
        alongside the live row (which consumes one slot, undeletable),
        and malformed rows stay byte-identical. At base nothing is
        pruned, so the exact survivor-set assertion fails."""
        stamps = self._ttl_fresh_stamps(10)  # ascending, all inside TTL
        keys, malformed_keys = self._seed_ordered_census(stamps)
        cap = 4
        r = self._run(
            "hermes-convention",
            stdin=json.dumps({"payload": {"session_id": self.LIVE_SESSION}}),
            env=self._env_with_cap(cap))
        self.assertEqual(r.returncode, 0,
                         f"hermes-convention must exit 0; stderr tail: "
                         f"{r.stderr[-300:]!r}")
        meta = self._meta_values()
        # Accounting law: live row consumes 1 of 4 slots (undeletable);
        # the 3 NEWEST stamps survive. Keys are anti-correlated with
        # time, so the survivors are the 3 SMALLEST key VALUES, which
        # sit at list positions keys[-3:] (the newest stamps' entries) —
        # a key-only sorter keeps the 3 largest keys and fails here.
        # (wave-4 fix: the wave-3 slice wrongly expected keys[:3].)
        expected_survivors = (
            set(keys[-3:]) | {self.LIVE_KEY} | set(malformed_keys))
        observed = {k for k in meta
                    if k.startswith("hermes_convention_count_")}
        self.assertEqual(
            observed, expected_survivors,
            f"#446 AC6 wave-2: cap {cap} with a live current row must "
            f"retain exactly the 3 NEWEST fresh rows + the live row + the "
            f"malformed rows (oldest-first deletion, key tie-break); "
            f"observed survivors {sorted(observed)!r}, expected "
            f"{sorted(expected_survivors)!r}")
        for k in malformed_keys:
            self.assertEqual(
                meta.get(k), MALFORMED_VALUE,
                f"malformed row {k!r} must stay BYTE-IDENTICAL (never "
                f"rewritten); observed {meta.get(k)!r}")

    def test_cap_tie_broken_by_key(self):
        """Wave-2 leg (r2 finding 3): same-timestamp rows tie-break by
        key lexicographic (oldest = smallest key deleted first) — the
        one-second timestamp resolution makes the tie-breaker
        load-bearing."""
        stamps = [self._ttl_fresh_stamps(4)[-1]] * 4  # one identical stamp
        keys, _ = self._seed_ordered_census(stamps, malformed_n=0)
        cap = 3  # live consumes 1; 2 same-stamp fresh survive
        r = self._run(
            "hermes-convention",
            stdin=json.dumps({"payload": {"session_id": self.LIVE_SESSION}}),
            env=self._env_with_cap(cap))
        self.assertEqual(r.returncode, 0,
                         f"hermes-convention must exit 0; stderr tail: "
                         f"{r.stderr[-300:]!r}")
        meta = self._meta_values()
        expected = set(sorted(keys)[-2:]) | {self.LIVE_KEY}
        observed = {k for k in meta
                    if k.startswith("hermes_convention_count_")}
        self.assertEqual(
            observed, expected,
            f"#446 AC6 wave-2: a same-timestamp tie must break by KEY "
            f"(largest keys survive); observed {sorted(observed)!r}, "
            f"expected {sorted(expected)!r}")

    def test_second_call_steady_state_byte_identical(self):
        """Wave-3 leg (r3 finding N3-3, replacing the wave-2 shape):
        steady state under a PERSISTENT above-cap retained population —
        70 malformed rows (never deletable) + 10 TTL-fresh parseable rows
        + the live row keep the retained total (81) above cap 64, so the
        SECOND production call genuinely re-enters the classification
        pass — and must write NOTHING except the live row's own counter
        increment (idempotence of the disclosed steady-state path)."""
        malformed_n = 70
        fresh_stamps = self._ttl_fresh_stamps(10)
        fresh_keys = [
            f"hermes_convention_count_dddd0000-eeee-ffff-"
            f"steady-{i:010d}" for i in range(10)
        ]
        pairs = [
            (k, json.dumps({"count": 2, "last_seen": ts}))
            for k, ts in zip(fresh_keys, fresh_stamps)
        ]
        pairs += [
            (f"hermes_convention_count_dddd0000-eeee-ffff-steadbad-{i}",
             MALFORMED_VALUE)
            for i in range(malformed_n)
        ]
        pairs.append((self.LIVE_KEY, "5"))
        conn = sqlite3.connect(str(self.store))
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                pairs)
            conn.commit()
        finally:
            conn.close()
        r1 = self._drive_production_entry()
        self.assertEqual(r1.returncode, 0,
                         f"first entry must exit 0; stderr tail: "
                         f"{r1.stderr[-300:]!r}")
        snapshot = self._meta_values()
        retained = len(self._convention_rows())
        self.assertGreater(
            retained, self.CAP,
            f"fixture sanity: the retained population ({retained}) must "
            f"stay ABOVE cap {self.CAP} after the first call so the "
            f"second call re-enters the classification pass")
        r2 = self._drive_production_entry()
        self.assertEqual(r2.returncode, 0,
                         f"second entry must exit 0; stderr tail: "
                         f"{r2.stderr[-300:]!r}")
        after = self._meta_values()
        self.assertEqual(
            {k: v for k, v in after.items() if k != self.LIVE_KEY},
            {k: v for k, v in snapshot.items() if k != self.LIVE_KEY},
            "#446 AC6 wave-3: steady state — the second production call "
            "must leave every meta row byte-identical apart from the live "
            "row's own counter increment (no prune churn, no upgrades, "
            "no rewrites)")
        rows = self._convention_rows()
        self.assertEqual(
            json.loads(rows[self.LIVE_KEY]).get("count"), 7,
            "second-call sanity: the live row's own counter advanced 6->7 "
            "(writer contract); everything else stayed byte-identical")


if __name__ == "__main__":
    unittest.main(verbosity=2)
