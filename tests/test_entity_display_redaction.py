"""Entity display redaction: credential-shaped names/aliases are masked at
every entity report surface (collated #455 + #366, AC1).

RED at base (surfaces echo raw), GREEN after the display-masking fix: when a
credential-shaped canonical name or alias is rendered by an entity report
surface, the whole value must be replaced by the literal marker
[REDACTED_SECRET]; the raw value must never reach stdout. Legal label:port
names are pinned unmasked by tests/test_entity_display_redaction_legal_names.py.

Every leg drives the real skills/memory/scripts/store.py as a subprocess in
a hermetic scratch store (fixture per the trace probe: one real `add`, then
direct INSERTs of the legacy pre-v16 shapes). This module never resolves a
store in-process — each subprocess gets a scrubbed env with the scratch
store pinned (ambient ZMEM_* stripped; ZMEM_ALLOW_REAL_STORE never set).
"""

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STORE_PY = REPO / "skills" / "memory" / "scripts" / "store.py"
MARKER = "[REDACTED_SECRET]"


class EntityDisplayRedactionTest(unittest.TestCase):
    """One test method per report surface; each failure names its leg."""

    @classmethod
    def setUpClass(cls):
        cls._td = tempfile.TemporaryDirectory(prefix="zmem455red_")
        scratch = Path(cls._td.name)
        cls.env = dict(os.environ)
        for var in ("ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
                    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_ALLOW_REAL_STORE"):
            cls.env.pop(var, None)
        cls.env["ZMEM_STORE"] = str(scratch / "store.sqlite")
        cls.env["ZMEM_DATA"] = str(scratch)
        cls.env["ZMEM_MODELS_DIR"] = str(scratch / "models-absent")
        cls.env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        cls.env["PYTHONDONTWRITEBYTECODE"] = "1"
        seed = subprocess.run(
            [sys.executable, "-X", "utf8", str(STORE_PY), "add", "--type",
             "fact", "--content",
             "seed mentions localhost:8080 so the extractor mints the schema",
             "--tags", "entity:host:localhost:8080",
             "--namespace", "user:probe455"],
            capture_output=True, text=True, encoding="utf-8",
            env=cls.env, cwd=str(REPO))
        assert seed.returncode == 0, f"seed add failed: {seed.stderr}"
        suffix = uuid.uuid4().hex[:8]
        cls.cred_other = f"e-cred-other-{suffix}"  # other   password:12345
        cls.cred_svc = f"e-cred-svc-{suffix}"      # service token:98765
        cls.legal = f"e-legal-{suffix}"            # service localhost:8080
        cls.legal2 = f"e-legal2-{suffix}"          # host    server:8080
        cls.orphan = f"e-orphan-{suffix}"          # other   host:token-acme
        conn = sqlite3.connect(cls.env["ZMEM_STORE"])
        now = "2026-11-11T00:00:00Z"
        for eid, kind, name in (
                (cls.cred_other, "other", "password:12345"),
                (cls.cred_svc, "service", "token:98765"),
                (cls.legal, "service", "localhost:8080"),
                (cls.legal2, "host", "server:8080"),
                (cls.orphan, "other", "host:token-acme")):
            conn.execute(
                "INSERT INTO entity(id, kind, canonical_name, created_at, "
                "updated_at) VALUES (?,?,?,?,?)", (eid, kind, name, now, now))
        conn.executemany(
            "INSERT INTO entity_alias(entity_id, alias_norm) VALUES (?,?)",
            [(cls.legal, "password:12345"),   # credential alias on legal row
             (cls.legal2, "token-acme")])     # suffix alias the orphan resolves to
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        cls._td.cleanup()

    def run_surface(self, label, args, raws):
        """Both directions per leg: no raw credential value on stdout AND the
        masked marker rendered (raw-absent alone would pass on empty output)."""
        p = subprocess.run(
            [sys.executable, "-X", "utf8", str(STORE_PY)] + args,
            capture_output=True, text=True, encoding="utf-8",
            env=self.env, cwd=str(REPO))
        self.assertEqual(p.returncode, 0,
                         f"{label}: exit {p.returncode}: {p.stderr}")
        leaked = [r for r in raws if r in p.stdout]
        self.assertEqual([], leaked,
                         f"{label}: RAW-LEAK credential value(s) {leaked} "
                         f"reached stdout unmasked; expected {MARKER}")
        self.assertIn(MARKER, p.stdout,
                      f"{label}: masked form {MARKER} absent from output")
        return p.stdout

    def test_reclassify_dry_run_json_masks_credential_name(self):
        self.run_surface("reclassify-dry-json",
                         ["reclassify-entities", "--dry-run", "--json"],
                         ["password:12345", "token:98765", "host:token-acme"])

    def test_reclassify_dry_run_human_masks_credential_name(self):
        self.run_surface("reclassify-dry-human",
                         ["reclassify-entities", "--dry-run"],
                         ["password:12345", "token:98765", "host:token-acme"])

    def test_entity_list_json_masks_names_and_aliases(self):
        self.run_surface("entity-list-json", ["entity-list", "--json"],
                         ["password:12345", "token:98765",
                          "host:token-acme", "token-acme"])

    def test_entity_list_human_masks_names_and_aliases(self):
        self.run_surface("entity-list-human", ["entity-list"],
                         ["password:12345", "token:98765",
                          "host:token-acme", "token-acme"])

    def test_entity_merge_dry_run_masks_from_name(self):
        # entity-merge is DRY RUN without --confirm; the from-name site is
        # credential-shaped, the to-name site is a legal name (C2's leg).
        self.run_surface("entity-merge-dry",
                         ["entity-merge", "--from", self.cred_svc,
                          "--to", self.legal],
                         ["token:98765", "password:12345"])

    def test_reconcile_dry_run_json_masks_orphan_name(self):
        self.run_surface("reconcile-dry-json",
                         ["reconcile-entity-aliases", "--dry-run", "--json"],
                         ["host:token-acme"])

    def test_reconcile_dry_run_human_masks_orphan_name(self):
        self.run_surface("reconcile-dry-human",
                         ["reconcile-entity-aliases", "--dry-run"],
                         ["host:token-acme"])


if __name__ == "__main__":
    unittest.main()
