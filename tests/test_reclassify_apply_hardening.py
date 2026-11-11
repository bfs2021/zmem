"""Issue #367: reclassify-entities apply-path hardening and test pins.

Pins the apply-path contracts the #365 review round identified as gaps:

- ApplyEnvelopeSingleSnapshotTest (C4/AC4): the apply envelope describes ONE
  locked snapshot — scanned and before_other_link_share are computed under the
  write lock pre-UPDATE, not replayed from the stale pre-lock proposal.
- CommittedApplyStdoutFailureTest (C5/AC5): a stdout failure AFTER the commit
  reports success from the committed state (exit 0 + stderr note), never exit 2
  for a committed apply.
- UnmigratedStoreRefusalTest (C6/AC6): --apply refuses an unmigrated store up
  front with the rescan-secrets-style message (stamp != SUPPORTED).
- ApplyRollbackAndConcurrentMutationTest (C7/AC7): forced post-executemany
  failure rolls the whole apply back; a target mutated between scan and BEGIN
  fails closed with ValueError and zero changes.
- ApplyLeaseResidueTest (C8/AC8): a successful apply leaves no *.lease residue
  and SKILL.md documents the writer lease as advisory for this surface.
- DryRunNoSidecarTest (C11/AC11): dry-run leaves the main store byte-identical
  and creates no -wal/-shm sidecars.

Run: python tests/test_reclassify_apply_hardening.py  (no pytest; house convention)
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable


def _base_env(store_path: str) -> dict:
    env = dict(os.environ)
    env["ZMEM_STORE"] = store_path
    env["ZMEM_MODELS_DIR"] = str(Path(store_path).parent / "no-models")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env.pop("ZMEM_DATA", None)
    env.pop("ZMEM_BACKUP_DIR", None)
    env.pop("ZMEM_ENTITY_KIND_WEIGHTS", None)
    return env


class _HardeningStore(unittest.TestCase):
    """Subprocess CLI + direct sqlite seeding; in-process seams per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-hardening-")
        self.store = str(Path(self.tmp) / "store.sqlite")
        self.env = _base_env(self.store)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_store(self, *args, env=None, check=True, timeout=120):
        r = subprocess.run(
            [PYTHON, str(STORE_PY), *args],
            capture_output=True, text=True, env=env or self.env, timeout=timeout,
        )
        if check and r.returncode != 0:
            self.fail(f"store.py {' '.join(args)} exited {r.returncode}\n"
                      f"stdout: {r.stdout}\nstderr: {r.stderr}")
        return r

    def _seed(self, *entities):
        """Create the store with one memory, then insert the named other-kind
        entities (id, canonical_name). `README.md` re-kinds to file."""
        self.run_store(
            "add", "--namespace", "project:hard", "--type", "fact",
            "--content", "hardening seed", "--tags", "zzseed",
            "--source-ref", "hard-367", "--json")
        conn = sqlite3.connect(self.store)
        try:
            now = "2026-01-01T00:00:00Z"
            for eid, name in entities:
                conn.execute(
                    "INSERT INTO entity(id,kind,canonical_name,created_at,"
                    "updated_at) VALUES(?,?,?,?,?)", (eid, "other", name, now, now))
                conn.execute(
                    "INSERT INTO entity_alias(entity_id,alias_norm) VALUES(?,?)",
                    (eid, name))
            conn.commit()
        finally:
            conn.close()

    def _import_storelib_entity(self):
        """In-process storelib.entity import with env pinned and any prior
        storelib modules purged (the store-path-freeze pattern)."""
        os.environ["ZMEM_STORE"] = self.store
        os.environ["ZMEM_MODELS_DIR"] = str(Path(self.store).parent / "no-models")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        sys.path.insert(0, str(SCRIPTS_DIR))
        self.addCleanup(sys.path.remove, str(SCRIPTS_DIR))
        for mod in [m for m in list(sys.modules)
                    if m == "storelib" or m.startswith("storelib.")]:
            del sys.modules[mod]
        import storelib.entity as entity_mod  # noqa: E402
        return entity_mod


class ApplyEnvelopeSingleSnapshotTest(_HardeningStore):
    """C4 (AC4): the envelope reports the locked snapshot, not the proposal."""

    def test_foreign_write_between_scan_and_begin_is_reflected(self):
        self._seed(("e-file", "README.md"),      # proposal target -> file
                   ("e-stay", "zzstay"),          # stays other
                   ("e-foreign", "zzforeign"))    # re-kinded by a foreign writer
        # Link the foreign row so the share arithmetic is exact: the seeded
        # memory already links the project entity and the zzseed tag entity.
        link = sqlite3.connect(self.store)
        try:
            mid = link.execute(
                "SELECT id FROM memory WHERE source_ref='hard-367'").fetchone()[0]
            link.execute(
                "INSERT INTO memory_entity(memory_id,entity_id,role)"
                " VALUES(?,?,'mentions')", (mid, "e-foreign"))
            link.commit()
        finally:
            link.close()
        entity_mod = self._import_storelib_entity()
        real_scan = entity_mod._reclassify_scan
        state = {"calls": 0}

        def scan_wrapper(conn, *, apply_changes, proposed=None):
            result = real_scan(conn, apply_changes=apply_changes, proposed=proposed)
            if apply_changes is False and proposed is None and state["calls"] == 0:
                # A non-lease writer re-kinds a foreign row AFTER the proposal
                # scan returns but BEFORE BEGIN IMMEDIATE.
                state["calls"] += 1
                other = sqlite3.connect(self.store, timeout=5.0)
                try:
                    other.execute(
                        "UPDATE entity SET kind='host' WHERE id='e-foreign'")
                    other.commit()
                finally:
                    other.close()
            return result

        entity_mod._reclassify_scan = scan_wrapper
        try:
            conn = sqlite3.connect(self.store, timeout=5.0)
            conn.row_factory = sqlite3.Row
            conn.isolation_level = None  # caller-owned transactions
            try:
                envelope = entity_mod.reclassify_entities(conn, dry_run=False)
            finally:
                conn.close()
        finally:
            entity_mod._reclassify_scan = real_scan

        # Proposal scanned 4 other rows (e-file, e-stay, e-foreign, zzseed —
        # the seed add mints the zzseed tag entity). Under the lock
        # (pre-UPDATE) e-foreign is already host, so the locked count is 3.
        # The stale proposal replay at base reports scanned=4.
        self.assertEqual(envelope["scanned"], 3,
                         "scanned must describe the locked pre-UPDATE snapshot")
        # before-share from the LOCKED pre-UPDATE kinds: links are project,
        # zzseed (other), e-foreign (host under the lock) -> 1/3. The stale
        # proposal fold-back (e-foreign counted other) reports 2/3.
        self.assertAlmostEqual(envelope["before_other_link_share"], 1 / 3,
                               places=6,
                               msg="before-share must reflect the locked kinds")
        self.assertAlmostEqual(envelope["after_other_link_share"], 1 / 3,
                               places=6)
        self.assertEqual(envelope["changed"], 1)
        changes = [c["id"] for c in envelope["changes"]]
        self.assertEqual(changes, ["e-file"])


class CommittedApplyStdoutFailureTest(_HardeningStore):
    """C5 (AC5): committed apply + failed stdout => success, not failure."""

    def test_stdout_failure_after_commit_exits_zero_with_note(self):
        self._seed(("e-file", "README.md"))
        r_pipe, w_pipe = os.pipe()
        os.close(r_pipe)  # no reader: the child's stdout write fails
        try:
            r = subprocess.run(
                [PYTHON, str(STORE_PY), "reclassify-entities",
                 "--apply", "--json"],
                stdout=w_pipe, stderr=subprocess.PIPE, env=self.env,
                timeout=120, text=True,
            )
        finally:
            os.close(w_pipe)
        # The apply itself committed; the reporting failure must not turn the
        # process exit into a failure of the committed operation.
        self.assertEqual(
            r.returncode, 0,
            f"a committed apply must report success from the committed state; "
            f"stderr was: {r.stderr!r}")
        self.assertIn("committed", r.stderr.lower(),
                      "stderr must explain the envelope could not be printed")
        conn = sqlite3.connect(self.store)
        try:
            kind = conn.execute(
                "SELECT kind FROM entity WHERE id='e-file'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(kind, "file", "rows must be committed")

    def test_human_renderer_closed_stdout_after_commit_exits_zero(self):
        # The HUMAN renderer (no --json) behind the same committed-rendering
        # boundary: a closed stdout must not turn a committed apply into a
        # failure. (The CLI reconfigures streams to UTF-8/replace at startup,
        # so encoding failures cannot fire in-process; the closed-stdout leg
        # exercises the boundary deterministically on both arms.)
        self._seed(("e-file", "README.md"))
        r_pipe, w_pipe = os.pipe()
        os.close(r_pipe)
        try:
            r = subprocess.run(
                [PYTHON, str(STORE_PY), "reclassify-entities", "--apply"],
                stdout=w_pipe, stderr=subprocess.PIPE, env=self.env,
                timeout=120, text=True,
            )
        finally:
            os.close(w_pipe)
        self.assertEqual(
            r.returncode, 0,
            f"committed apply must report success; stderr={r.stderr!r}")
        self.assertIn("committed", r.stderr.lower())
        conn = sqlite3.connect(self.store)
        try:
            kind = conn.execute(
                "SELECT kind FROM entity WHERE id='e-file'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(kind, "file", "rows must be committed")


class UnmigratedStoreRefusalTest(_HardeningStore):
    """C6 (AC6): --apply refuses an unmigrated store up front."""

    def test_older_stamp_is_refused_with_rescan_style_message(self):
        self._seed(("e-file", "README.md"))
        sys.path.insert(0, str(SCRIPTS_DIR))
        try:
            from schema_meta import SUPPORTED_SCHEMA_VERSION
        finally:
            sys.path.remove(str(SCRIPTS_DIR))
        conn = sqlite3.connect(self.store)
        try:
            conn.execute(
                "UPDATE meta SET value=? WHERE key='schema_version'",
                (str(SUPPORTED_SCHEMA_VERSION - 1),))
            conn.commit()
        finally:
            conn.close()
        r = self.run_store("reclassify-entities", "--apply", check=False)
        self.assertEqual(
            r.returncode, 2,
            f"unmigrated store must be refused up front; "
            f"stdout={r.stdout!r} stderr={r.stderr!r}")
        self.assertRegex(
            r.stderr,
            r"store schema version \d+ is not the supported \d+; "
            r"run any zmem command once to migrate")
        conn = sqlite3.connect(self.store)
        try:
            kind = conn.execute(
                "SELECT kind FROM entity WHERE id='e-file'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(kind, "other", "refused apply must change nothing")

    def test_newer_stamp_routes_through_forward_compat_not_migrate_message(self):
        """Review F-07 (PR #454): a NEWER stamp must reach the compatibility
        gate (proper refuse message, ZMEM_ALLOW_NEWER_SCHEMA honored), not
        the migrate-then-retry refusal the older-stamp arm produces."""
        self._seed(("e-file", "README.md"))
        sys.path.insert(0, str(SCRIPTS_DIR))
        try:
            from schema_meta import SUPPORTED_SCHEMA_VERSION
        finally:
            sys.path.remove(str(SCRIPTS_DIR))
        conn = sqlite3.connect(self.store)
        try:
            conn.execute(
                "UPDATE meta SET value=? WHERE key='schema_version'",
                (str(SUPPORTED_SCHEMA_VERSION + 1),))
            conn.commit()
        finally:
            conn.close()
        # Without the override: the compat gate refuses (its message names
        # the forward-compat ceiling and the override variable).
        r = self.run_store("reclassify-entities", "--apply", check=False)
        self.assertEqual(r.returncode, 2,
                         f"stderr={r.stderr!r}")
        self.assertNotIn("run any zmem command once to migrate", r.stderr,
                         "newer store must NOT get the migrate refusal")
        self.assertIn("ZMEM_ALLOW_NEWER_SCHEMA", r.stderr)
        # With the override: the apply proceeds like any compat-mode command.
        env = dict(self.env)
        env["ZMEM_ALLOW_NEWER_SCHEMA"] = "1"
        r2 = self.run_store("reclassify-entities", "--apply", "--json",
                            env=env, check=False)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn("WARNING: ZMEM_ALLOW_NEWER_SCHEMA=1", r2.stderr)
        conn = sqlite3.connect(self.store)
        try:
            kind = conn.execute(
                "SELECT kind FROM entity WHERE id='e-file'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(kind, "file", "override apply must commit")


class ApplyRollbackAndConcurrentMutationTest(_HardeningStore):
    """C7 (AC7): rollback + fail-closed mutation guard, both pinned."""

    def test_failure_after_executemany_rolls_back_whole_apply(self):
        self._seed(("e-file", "README.md"), ("e-stay", "zzstay"))
        entity_mod = self._import_storelib_entity()
        real_scan = entity_mod._reclassify_scan

        def scan_wrapper(conn, *, apply_changes, proposed=None):
            if apply_changes and proposed is not None:
                raise RuntimeError("forced post-executemany failure")
            return real_scan(conn, apply_changes=apply_changes, proposed=proposed)

        before_kinds = None
        probe = sqlite3.connect(self.store)
        try:
            before_kinds = dict(probe.execute(
                "SELECT id, kind FROM entity").fetchall())
        finally:
            probe.close()
        entity_mod._reclassify_scan = scan_wrapper
        conn = sqlite3.connect(self.store, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        try:
            with self.assertRaises(RuntimeError):
                entity_mod.reclassify_entities(conn, dry_run=False)
            # DISCRIMINATING the explicit rollback: assert on the STILL-OPEN
            # connection. Closing an uncommitted transaction rolls it back
            # implicitly, so a mutant that swaps rollback for commit would be
            # masked by any post-close inspection.
            self.assertFalse(
                conn.in_transaction,
                "the failed apply must leave no open transaction")
            open_kinds = dict(conn.execute(
                "SELECT id, kind FROM entity").fetchall())
            self.assertEqual(open_kinds, before_kinds,
                             "explicit rollback must restore the exact "
                             "pre-apply kinds (not a close-time rollback)")
        finally:
            entity_mod._reclassify_scan = real_scan
            conn.close()
        check = sqlite3.connect(self.store)
        try:
            kinds = dict(check.execute("SELECT id, kind FROM entity").fetchall())
        finally:
            check.close()
        self.assertEqual(kinds, before_kinds,
                         "a fresh connection must see the rolled-back state")

    def test_target_mutated_between_scan_and_begin_fails_closed(self):
        self._seed(("e-file", "README.md"), ("e-stay", "zzstay"))
        entity_mod = self._import_storelib_entity()
        real_scan = entity_mod._reclassify_scan
        state = {"calls": 0}

        def scan_wrapper(conn, *, apply_changes, proposed=None):
            result = real_scan(conn, apply_changes=apply_changes, proposed=proposed)
            if apply_changes is False and proposed is None and state["calls"] == 0:
                state["calls"] += 1
                other = sqlite3.connect(self.store, timeout=5.0)
                try:
                    other.execute(
                        "UPDATE entity SET kind='file' WHERE id='e-file'")
                    other.commit()
                finally:
                    other.close()
            return result

        entity_mod._reclassify_scan = scan_wrapper
        conn = sqlite3.connect(self.store, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        try:
            with self.assertRaisesRegex(
                    ValueError, r"no longer.*kind 'other'"):
                entity_mod.reclassify_entities(conn, dry_run=False)
        finally:
            entity_mod._reclassify_scan = real_scan
            conn.close()
        check = sqlite3.connect(self.store)
        try:
            kinds = dict(check.execute("SELECT id, kind FROM entity").fetchall())
        finally:
            check.close()
        self.assertEqual(kinds["e-file"], "file",
                         "the foreign mutation stands; the apply added nothing")
        self.assertEqual(kinds["e-stay"], "other")


class ApplyLeaseResidueTest(_HardeningStore):
    """C8 (AC8): successful apply leaves no lease; docs say advisory."""

    def test_successful_apply_leaves_no_lease_file(self):
        self._seed(("e-file", "README.md"))
        self.run_store("reclassify-entities", "--apply", "--json")
        leases = list(Path(self.tmp).rglob("*.lease"))
        self.assertEqual(
            leases, [],
            "a successful apply must release its writer lease")
        conn = sqlite3.connect(self.store)
        try:
            kind = conn.execute(
                "SELECT kind FROM entity WHERE id='e-file'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(kind, "file")

    def test_skill_documents_advisory_lease(self):
        text = (REPO_ROOT / "skills" / "memory" / "SKILL.md").read_text(
            encoding="utf-8")
        i = text.find("### reclassify-entities")
        self.assertGreaterEqual(i, 0, "reclassify section must exist")
        end = text.find("\n### ", i)
        section = text[i:(end + 1) if end >= 0 else len(text)]
        self.assertIn("lease", section)
        self.assertIn("advisory", section,
                      "the lease must be documented as advisory, not a mutex")


class DryRunNoSidecarTest(_HardeningStore):
    """C11 (AC11): dry-run writes zero bytes and no WAL sidecars."""

    def test_dry_run_leaves_main_file_and_no_sidecars(self):
        self._seed(("e-file", "README.md"))
        before = Path(self.store).read_bytes()
        self.run_store("reclassify-entities", "--dry-run", "--json")
        self.assertEqual(Path(self.store).read_bytes(), before,
                         "dry-run must not modify the main store file")
        sidecars = [p.name for p in Path(self.tmp).iterdir()
                    if p.name.endswith(("-wal", "-shm"))]
        self.assertEqual(
            sidecars, [],
            f"dry-run must not materialize WAL sidecars: {sidecars}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
