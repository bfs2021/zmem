"""Legal entity names stay unmasked through the display-redaction path
(collated #455 + #366, AC2).

RED at base (the credential-shaped values on the same output still echo
raw), GREEN after the display-masking fix: the legal label:port names
localhost:8080 and server:8080 must render UNMASKED on every surface that
shows them (entity-list json+human, entity-merge to-name site,
reconcile-entity-aliases owner name) — never replaced by [REDACTED_SECRET] —
while the credential-shaped values rendered by the SAME output ARE masked.
Catches the mask-everything implementation that would satisfy
test_entity_display_redaction.py while breaking legal names.

Same hermetic shape as the C1 file: the real store.py runs as a subprocess
against a scratch store (one real `add` + direct INSERTs per the trace
probe); the parent process never resolves a store, and each subprocess env
is scrubbed and repinned (ZMEM_ALLOW_REAL_STORE never set).
"""

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import _store_guard  # noqa: F401  (issue #244 isolation guard)
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STORE_PY = REPO / "skills" / "memory" / "scripts" / "store.py"
MARKER = "[REDACTED_SECRET]"


class LegalNamesStayUnmaskedTest(unittest.TestCase):
    """One test method per surface that renders a legal name."""

    @classmethod
    def setUpClass(cls):
        cls._td = tempfile.TemporaryDirectory(prefix="zmem455leg_")
        scratch = Path(cls._td.name)
        cls.env = dict(os.environ)
        for var in ("ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
                    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_ALLOW_REAL_STORE",
                    "ZMEM_REMOTE_URL", "ZMEM_NAMESPACE", "ZMEM_SESSION",
                    "ZMEM_SESSION_ID", "ZMEM_CONSUME_AGENT_ID",
                    "ZMEM_DELEGATION_AGENT_ID"):
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
            env=cls.env, cwd=str(REPO), timeout=120)
        assert seed.returncode == 0, f"seed add failed: {seed.stderr}"
        suffix = uuid.uuid4().hex[:8]
        cls.cred_other = f"e-cred-other-{suffix}"  # other   password:12345
        cls.cred_svc = f"e-cred-svc-{suffix}"      # service token:98765
        cls.legal = f"e-legal-{suffix}"            # service localhost:8080
        cls.legal2 = f"e-legal2-{suffix}"          # host    server:8080
        cls.orphan = f"e-orphan-{suffix}"          # other   host:token-acme
        cls.legal_other = f"e-legal-oth-{suffix}"  # other   cron:9100 (legal label:port)
        conn = sqlite3.connect(cls.env["ZMEM_STORE"])
        now = "2026-11-11T00:00:00Z"
        for eid, kind, name in (
                (cls.cred_other, "other", "password:12345"),
                (cls.cred_svc, "service", "token:98765"),
                (cls.legal, "service", "localhost:8080"),
                (cls.legal2, "host", "server:8080"),
                (cls.orphan, "other", "host:token-acme"),
                (cls.legal_other, "other", "cron:9100")):
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

    def run_surface(self, label, args, raws, legal):
        """Three directions per leg: raw credential values absent, the masked
        marker present, and every legal name still rendered unmasked."""
        p = subprocess.run(
            [sys.executable, "-X", "utf8", str(STORE_PY)] + args,
            capture_output=True, text=True, encoding="utf-8",
            env=self.env, cwd=str(REPO), timeout=120)
        self.assertEqual(p.returncode, 0,
                         f"{label}: exit {p.returncode}: {p.stderr}")
        leaked = [r for r in raws if r in p.stdout]
        self.assertEqual([], leaked,
                         f"{label}: RAW-LEAK credential value(s) {leaked} "
                         f"reached stdout unmasked; expected {MARKER}")
        self.assertIn(MARKER, p.stdout,
                      f"{label}: masked form {MARKER} absent from output")
        for name in legal:
            self.assertIn(name, p.stdout,
                          f"{label}: LEGAL-MASKED {name!r} must render "
                          f"unmasked, never as {MARKER}")
        return p.stdout

    def test_entity_list_json_keeps_legal_names_unmasked(self):
        self.run_surface("entity-list-json", ["entity-list", "--json"],
                         ["password:12345", "token:98765",
                          "host:token-acme", "token-acme"],
                         ["localhost:8080", "server:8080"])

    def test_entity_list_human_keeps_legal_names_unmasked(self):
        self.run_surface("entity-list-human", ["entity-list"],
                         ["password:12345", "token:98765",
                          "host:token-acme", "token-acme"],
                         ["localhost:8080", "server:8080"])

    def test_entity_merge_dry_run_keeps_legal_to_name_unmasked(self):
        # Dry run (no --confirm): the from-name is credential-shaped (must be
        # masked), the to-name 'localhost:8080' is legal (must stay unmasked).
        self.run_surface("entity-merge-dry",
                         ["entity-merge", "--from", self.cred_svc,
                          "--to", self.legal],
                         ["token:98765"],
                         ["localhost:8080"])

    def test_reclassify_dry_run_keeps_legal_other_name_unmasked(self):
        """cubic batch-1 P3: the legal fixture must exercise
        reclassify-entities (only `other` rows are scanned)."""
        p = self.run_surface(
            "reclassify dry json",
            ["reclassify-entities", "--dry-run", "--json"],
            raws=[], legal=["cron:9100"])
        import json as _json
        payload = _json.loads(p)
        names = {c["name"] for c in payload["changes"]}
        self.assertIn("cron:9100", names,
                      "the legal other-kind row must appear UNMASKED "
                      "in the reclassify change set")

    def test_reconcile_dry_run_json_keeps_owner_legal_name_unmasked(self):
        # The orphan's credential-shaped name must be masked while the owner's
        # legal canonical name 'server:8080' stays unmasked in the same JSON.
        self.run_surface("reconcile-dry-json",
                         ["reconcile-entity-aliases", "--dry-run", "--json"],
                         ["host:token-acme"],
                         ["server:8080"])


if __name__ == "__main__":
    unittest.main()
