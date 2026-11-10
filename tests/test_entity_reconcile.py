"""Issue #358: reconcile-entity-aliases — report and merge pre-widening orphans.

A vocabulary widening (v16, twelve kinds) re-keys stored aliases on the next
relink: a pre-widening tag like `host:acme-prod` minted {other, alias
"host:acme-prod"}; post-widening the same token mints {host, alias "acme-prod"},
so the old `other` entity is left with zero memory_entity rows (or keeps stale
links) and `entity-merge` cannot repair it (it refuses the kind mismatch at
entity.py cmd_entity_merge). This file pins the reconciliation surface:

- ReconcileDryRunReportTest (C1): --dry-run reports each orphaned
  `<kind>:<name>` other-entity with the entity that now owns its suffix alias
  (owner may be null), and writes zero database bytes.
- ReconcileApplyMergeTest (C2): --apply moves the orphan's aliases and
  memory_entity links onto the owner (UNIQUE constraints respected), deletes the
  orphan row, leaves the old `<kind>:<name>` alias resolving to the owner, and
  reports + leaves untouched orphans that have no owner.

Run: python tests/test_entity_reconcile.py  (no pytest; house convention)
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import json
import os
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


class _ReconcileStore(unittest.TestCase):
    """Subprocess-driven house pattern; every command is the real CLI.

    Seeds the exact post-v16-upgrade state the migration preserved verbatim:
    a memory whose tags carry `host:acme-prod`, linked to a hand-inserted
    {other, canonical_name "host:acme-prod"} entity (alias = the WHOLE token),
    plus a live {host, "acme-prod"} owner minted by the post-widening path, and
    a second orphan (`svc:billing-api`) whose suffix has NO owner yet.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-reconcile-")
        self.store = str(Path(self.tmp) / "store.sqlite")
        self.env = _base_env(self.store)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_store(self, *args, env=None, check=True):
        r = subprocess.run(
            [PYTHON, str(STORE_PY), *args],
            capture_output=True, text=True, env=env or self.env, timeout=120,
        )
        if check and r.returncode != 0:
            self.fail(f"store.py {' '.join(args)} exited {r.returncode}\n"
                      f"stdout: {r.stdout}\nstderr: {r.stderr}")
        return r

    def _seed(self):
        r = self.run_store(
            "add", "--namespace", "project:reconcile", "--type", "fact",
            "--content", "acme prod host fact", "--tags", "host:acme-prod",
            "--source-ref", "reconcile-358", "--json",
        )
        mid = json.loads(r.stdout)["id"]
        # Post-widening mint of the SAME tag gives the owner {host, acme-prod}
        # (add minted it from the tag directly). Re-key the old memory's link to
        # the hand-inserted pre-widening row, exactly what v16 preserved.
        conn = sqlite3.connect(self.store)
        try:
            now = "2026-01-01T00:00:00Z"
            rows = conn.execute(
                "SELECT id, kind, canonical_name FROM entity").fetchall()
            by_name = {r[2]: r for r in rows}
            if "host:acme-prod" in by_name:
                # The CLI minted the new-form entity; rename nothing. Build the
                # pre-widening row by hand instead.
                pass
            conn.execute(
                "INSERT INTO entity(id,kind,canonical_name,created_at,updated_at)"
                " VALUES(?,?,?,?,?)",
                ("e-old-host", "other", "host:acme-prod", now, now))
            conn.execute(
                "INSERT INTO entity_alias(entity_id,alias_norm) VALUES(?,?)",
                ("e-old-host", "host:acme-prod"))
            # A second orphan whose suffix alias has no owner.
            conn.execute(
                "INSERT INTO entity(id,kind,canonical_name,created_at,updated_at)"
                " VALUES(?,?,?,?,?)",
                ("e-old-svc", "other", "service:billing-api", now, now))
            conn.execute(
                "INSERT INTO entity_alias(entity_id,alias_norm) VALUES(?,?)",
                ("e-old-svc", "service:billing-api"))
            # Move the memory's link onto the pre-widening row (the state the
            # migration left behind, before any relink split it).
            conn.execute("DELETE FROM memory_entity WHERE memory_id=?", (mid,))
            conn.execute(
                "INSERT INTO memory_entity(memory_id,entity_id,role)"
                " VALUES(?,?,'mentions')", (mid, "e-old-host"))
            conn.commit()
            self.mid = mid
        finally:
            conn.close()

    def _seed_bare(self):
        """Store + one clean memory, ZERO orphan rows (chain/cycle fixtures
        build their own graphs on top; the shared _seed's e-old-host would be
        an unrelated mergeable orphan and break exact assertions)."""
        r = self.run_store(
            "add", "--namespace", "project:reconcile", "--type", "fact",
            "--content", "chain cycle fixture content", "--tags", "zzclean",
            "--source-ref", "reconcile-chain", "--json",
        )
        self.mid = json.loads(r.stdout)["id"]

    def _seed_with_dedup_link(self):
        """Variant where the memory is linked to BOTH the orphan and the owner
        (the post-relink split state): UNIQUE(memory_id, entity_id) dedup must
        hold through the merge."""
        self._seed()
        conn = sqlite3.connect(self.store)
        try:
            owner = conn.execute(
                "SELECT id FROM entity WHERE kind='host' AND "
                "canonical_name='acme-prod'").fetchone()
            if owner is None:
                self.fail("owner entity missing: the add must mint it")
            conn.execute(
                "INSERT OR IGNORE INTO memory_entity(memory_id,entity_id,role)"
                " VALUES(?,?,'mentions')", (self.mid, owner[0]))
            conn.commit()
            self.owner_id = owner[0]
        finally:
            conn.close()


class ReconcileDryRunReportTest(_ReconcileStore):
    """C1 (AC1): the dry-run report names every orphan with its owner."""

    def test_dry_run_reports_orphans_with_owner_and_null(self):
        self._seed()
        before = Path(self.store).read_bytes()
        r = self.run_store("reconcile-entity-aliases", "--dry-run", "--json")
        doc = json.loads(r.stdout)
        self.assertIn("orphans", doc)
        by_id = {row["id"]: row for row in doc["orphans"]}
        self.assertIn("e-old-host", by_id, "pre-widening orphan not reported")
        host_row = by_id["e-old-host"]
        self.assertEqual(host_row["name"], "host:acme-prod")
        self.assertEqual(host_row["prefix"], "host")
        self.assertEqual(host_row["suffix"], "acme-prod")
        self.assertTrue(host_row["owner_id"], "owner must resolve by suffix alias")
        self.assertEqual(host_row["owner_name"], "acme-prod")
        self.assertIn("e-old-svc", by_id, "no-owner orphan not reported")
        svc_row = by_id["e-old-svc"]
        self.assertIsNone(svc_row["owner_id"])
        self.assertEqual(svc_row["prefix"], "service")
        self.assertEqual(svc_row["suffix"], "billing-api")
        # Zero database bytes: the report is strictly read-only.
        self.assertEqual(Path(self.store).read_bytes(), before,
                         "dry-run must not modify the store file")

    def test_non_orphan_rows_are_never_reported(self):
        self._seed()
        r = self.run_store("reconcile-entity-aliases", "--dry-run", "--json")
        doc = json.loads(r.stdout)
        names = {row["name"] for row in doc["orphans"]}
        # Plain other rows, typed rows, and non-kind prefixes are not orphans.
        self.assertNotIn("zznoextract", names)
        self.assertNotIn("acme-prod", names)
        self.assertNotIn("reconcile", names)


class ReconcileApplyMergeTest(_ReconcileStore):
    """C2 (AC2): --apply merges orphans into their owners, safely."""

    def test_apply_moves_aliases_and_links_and_deletes_orphan(self):
        self._seed()
        r = self.run_store("reconcile-entity-aliases", "--apply", "--json")
        doc = json.loads(r.stdout)
        self.assertGreaterEqual(doc.get("merged", 0), 1)
        conn = sqlite3.connect(self.store)
        try:
            orphans = conn.execute(
                "SELECT COUNT(*) FROM entity WHERE id IN ('e-old-host')"
            ).fetchone()[0]
            self.assertEqual(orphans, 0, "merged orphan row must be deleted")
            # The old full-token alias now resolves to the OWNER.
            owner_alias = conn.execute(
                "SELECT entity_id FROM entity_alias WHERE alias_norm=?",
                ("host:acme-prod",)).fetchone()
            self.assertIsNotNone(owner_alias,
                                 "old alias must survive the merge")
            self.assertNotEqual(owner_alias[0], "e-old-host")
            self.assertIn("merged_rows", doc,
                          "apply must report each merged row with its owner")
            self.assertEqual(owner_alias[0], doc["merged_rows"][0]["owner_id"])
            # The memory's link moved to the owner and dedup held.
            links = conn.execute(
                "SELECT entity_id FROM memory_entity WHERE memory_id=?",
                (self.mid,)).fetchall()
            self.assertEqual(len(links), 1, "dedup: exactly one link per pair")
            owner = conn.execute(
                "SELECT id FROM entity WHERE kind='host' AND "
                "canonical_name='acme-prod'").fetchone()[0]
            self.assertEqual(links[0][0], owner)
            # The no-owner orphan is untouched.
            svc = conn.execute(
                "SELECT COUNT(*) FROM entity WHERE id='e-old-svc'").fetchone()[0]
            self.assertEqual(svc, 1, "no-owner orphan must be left in place")
            svc_alias = conn.execute(
                "SELECT COUNT(*) FROM entity_alias WHERE entity_id='e-old-svc'"
            ).fetchone()[0]
            self.assertEqual(svc_alias, 1)
        finally:
            conn.close()

    def test_apply_respects_unique_constraints_on_dual_links(self):
        self._seed_with_dedup_link()
        r = self.run_store("reconcile-entity-aliases", "--apply", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = sqlite3.connect(self.store)
        try:
            links = conn.execute(
                "SELECT COUNT(*) FROM memory_entity WHERE memory_id=? AND "
                "entity_id=?", (self.mid, self.owner_id)).fetchone()[0]
            self.assertEqual(links, 1,
                             "UNIQUE(memory_id, entity_id) must dedupe the move")
            dup = conn.execute(
                "SELECT COUNT(*) FROM (SELECT memory_id, entity_id, COUNT(*) c"
                " FROM memory_entity GROUP BY memory_id, entity_id HAVING c > 1)"
            ).fetchone()[0]
            self.assertEqual(dup, 0, "no duplicate link pairs may exist")
        finally:
            conn.close()

    def test_chained_orphans_merge_into_the_terminal_live_owner(self):
        """A=other:host:service:foo -> B=other:service:foo -> C={service, foo}:
        resolving owners one at a time can delete B (A's direct owner) before
        A's rows move; A must land on the TERMINAL owner C, and every moved
        alias/link must reference a live entity afterwards."""
        self._seed_bare()
        conn = sqlite3.connect(self.store)
        try:
            now = "2026-01-01T00:00:00Z"
            # Terminal owner C: a live service entity that owns alias 'foo'.
            conn.execute(
                "INSERT INTO entity(id,kind,canonical_name,created_at,"
                "updated_at) VALUES(?,?,?,?,?)",
                ("e-chain-terminal", "service", "foo", now, now))
            conn.execute(
                "INSERT INTO entity_alias(entity_id,alias_norm) VALUES(?,?)",
                ("e-chain-terminal", "foo"))
            # Mid orphan B: other/service:foo, whose suffix 'foo' -> C.
            conn.execute(
                "INSERT INTO entity(id,kind,canonical_name,created_at,"
                "updated_at) VALUES(?,?,?,?,?)",
                ("e-chain-mid", "other", "service:foo", now, now))
            conn.execute(
                "INSERT INTO entity_alias(entity_id,alias_norm) VALUES(?,?)",
                ("e-chain-mid", "service:foo"))
            # Top orphan A: other/host:service:foo, whose suffix
            # 'service:foo' -> B.
            conn.execute(
                "INSERT INTO entity(id,kind,canonical_name,created_at,"
                "updated_at) VALUES(?,?,?,?,?)",
                ("e-chain-top", "other", "host:service:foo", now, now))
            conn.execute(
                "INSERT INTO entity_alias(entity_id,alias_norm) VALUES(?,?)",
                ("e-chain-top", "host:service:foo"))
            conn.execute(
                "INSERT INTO memory_entity(memory_id,entity_id,role)"
                " VALUES(?,?,'mentions')", (self.mid, "e-chain-top"))
            conn.commit()
        finally:
            conn.close()
        r = self.run_store("reconcile-entity-aliases", "--apply", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        check = sqlite3.connect(self.store)
        try:
            live = {row[0] for row in check.execute("SELECT id FROM entity")}
            self.assertNotIn("e-chain-mid", live)
            self.assertNotIn("e-chain-top", live)
            # The terminal owner C (e-chain-terminal, kind service, alias
            # 'foo') must end up owning BOTH chained full-token forms; every
            # alias references a live entity.
            dangling = check.execute(
                "SELECT COUNT(*) FROM entity_alias ea WHERE ea.entity_id NOT "
                "IN (SELECT id FROM entity)").fetchone()[0]
            self.assertEqual(dangling, 0, "no alias may reference a dead entity")
            for alias in ("host:service:foo", "service:foo"):
                owner = check.execute(
                    "SELECT entity_id FROM entity_alias WHERE alias_norm=?",
                    (alias,)).fetchone()
                self.assertIsNotNone(owner, f"alias {alias} must survive")
                self.assertEqual(
                    owner[0], "e-chain-terminal",
                    f"alias {alias} must reach the TERMINAL owner")
            moved_link = check.execute(
                "SELECT entity_id FROM memory_entity WHERE memory_id=? AND "
                "entity_id='e-chain-terminal'", (self.mid,)).fetchall()
            self.assertEqual(moved_link, [("e-chain-terminal",)],
                             "the chained link must land on the terminal owner")
            total_links = check.execute(
                "SELECT COUNT(*) FROM memory_entity WHERE memory_id=?",
                (self.mid,)).fetchone()[0]
            self.assertEqual(
                total_links, 3,
                "bare seed links (project + zzclean entities) plus the moved "
                "chain link = exactly: project, zzclean, terminal")
            links = check.execute(
                "SELECT COUNT(*) FROM memory_entity WHERE entity_id NOT IN "
                "(SELECT id FROM entity)").fetchone()[0]
            self.assertEqual(links, 0, "no link may reference a dead entity")
            merged_owners = {row["owner_id"] for row in doc["merged_rows"]}
            self.assertEqual(
                merged_owners, {"e-chain-terminal"},
                "both chained merges must report the terminal live owner")
        finally:
            check.close()

    def test_owner_cycle_is_refused_reported_and_untouched(self):
        """E1=other:host:alpha (alias host:alpha); E2=other:service:host:alpha
        (aliases service:host:alpha AND alpha). E1 suffix 'alpha' -> E2; E2
        suffix 'host:alpha' -> E1: a genuine two-node owner cycle. On a bare
        store the cycle is the ONLY candidate, so refusing it must leave the
        store file byte-identical."""
        self._seed_bare()
        conn = sqlite3.connect(self.store)
        try:
            now = "2026-01-01T00:00:00Z"
            conn.execute(
                "INSERT INTO entity(id,kind,canonical_name,created_at,"
                "updated_at) VALUES(?,?,?,?,?)",
                ("e-cyc-1", "other", "host:alpha", now, now))
            conn.execute(
                "INSERT INTO entity_alias(entity_id,alias_norm) VALUES(?,?)",
                ("e-cyc-1", "host:alpha"))
            conn.execute(
                "INSERT INTO entity(id,kind,canonical_name,created_at,"
                "updated_at) VALUES(?,?,?,?,?)",
                ("e-cyc-2", "other", "service:host:alpha", now, now))
            conn.execute(
                "INSERT INTO entity_alias(entity_id,alias_norm) VALUES(?,?)",
                ("e-cyc-2", "service:host:alpha"))
            # E2 owns alias 'alpha' (E1's suffix); E1 owns 'host:alpha'
            # (E2's suffix) -> a genuine two-node owner cycle.
            conn.execute(
                "INSERT INTO entity_alias(entity_id,alias_norm) VALUES(?,?)",
                ("e-cyc-2", "alpha"))
            conn.commit()
        finally:
            conn.close()
        before_bytes = Path(self.store).read_bytes()
        r = self.run_store("reconcile-entity-aliases", "--apply", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertGreaterEqual(doc.get("skipped_cycle", 0), 2,
                                "both cycle members must be reported skipped")
        self.assertIn("e-cyc-1", doc.get("cycle_ids", []),
                      "cycle ids must be reported")
        self.assertIn("e-cyc-2", doc.get("cycle_ids", []))
        check = sqlite3.connect(self.store)
        try:
            live = {row[0] for row in check.execute("SELECT id FROM entity")}
            cyc_aliases = check.execute(
                "SELECT entity_id, alias_norm FROM entity_alias WHERE "
                "alias_norm IN ('host:alpha','service:host:alpha','alpha') "
                "ORDER BY alias_norm").fetchall()
        finally:
            check.close()
        self.assertIn("e-cyc-1", live)
        self.assertIn("e-cyc-2", live)
        self.assertEqual(
            cyc_aliases,
            [("e-cyc-2", "alpha"), ("e-cyc-1", "host:alpha"),
             ("e-cyc-2", "service:host:alpha")],
            "the cycle rows must be exactly as seeded")
        self.assertEqual(Path(self.store).read_bytes(), before_bytes,
                         "a refused cycle on an otherwise-empty candidate set "
                         "must leave the store file byte-identical")

    def test_apply_then_dry_run_reports_zero_mergeable(self):
        self._seed()
        self.run_store("reconcile-entity-aliases", "--apply", "--json")
        r = self.run_store("reconcile-entity-aliases", "--dry-run", "--json")
        doc = json.loads(r.stdout)
        mergeable = [row for row in doc["orphans"] if row.get("owner_id")]
        self.assertEqual(mergeable, [],
                         "after a merge no orphan may still own a suffix match")


if __name__ == "__main__":
    unittest.main(verbosity=2)
