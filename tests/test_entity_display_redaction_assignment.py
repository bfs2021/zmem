"""Attribute-assignment (`=`-shaped) credential display masking (impl review
r1 HIGH finding, executed probe: `DB_PASSWORD=12345` minted by the real
write path echoed raw). GREEN after the `=`-separator fix; short values
(<8 chars) are used throughout so the shared registry's value floor cannot
mask them — only the local head rule can."""

import json
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
SCRIPTS = REPO / "skills" / "memory" / "scripts"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "storelib"))

from storelib import entity as entity_mod  # noqa: E402

MASK = "[REDACTED_SECRET]"
ASSIGN_RAW = "DB_PASSWORD=12345"   # minted by the REAL write path
ASSIGN_RAW2 = "password=1234"      # short value, head rule only
LEGAL = "localhost:8080"


class AssignmentShapeCliTest(unittest.TestCase):
    """End-to-end: the real CLI masks `=`-shaped names/aliases on both
    entity-list modes and the entity-merge dry-run renderer."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp(prefix="zmem455assign_")
        env = dict(os.environ)
        for k in ("ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
                  "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_ALLOW_REAL_STORE"):
            env.pop(k, None)
        env["ZMEM_STORE"] = str(Path(cls.td) / "store.sqlite")
        env["ZMEM_DATA"] = cls.td
        env["ZMEM_MODELS_DIR"] = str(Path(cls.td) / "models-absent")
        env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        cls.env = env
        # real write path mints the assignment-shaped entity rows
        for tags in (f"entity:other:{ASSIGN_RAW}", f"entity:other:{ASSIGN_RAW2}"):
            p = subprocess.run(
                [sys.executable, "-X", "utf8", str(STORE_PY), "add", "--type",
                 "fact", "--content", f"env probe mentions {LEGAL}",
                 "--tags", tags, "--namespace", "user:probe455c"],
                capture_output=True, text=True, encoding="utf-8", env=env,
                cwd=str(REPO))
            assert p.returncode == 0, p.stderr
        cls.svc = "e-svc-" + uuid.uuid4().hex[:8]
        cls.legal = "e-legal-" + uuid.uuid4().hex[:8]
        conn = sqlite3.connect(env["ZMEM_STORE"])
        now = "2026-11-11T00:00:00Z"
        conn.execute(
            "INSERT INTO entity(id, kind, canonical_name, created_at, "
            "updated_at) VALUES (?,?,?,?,?)",
            (cls.svc, "service", ASSIGN_RAW2, now, now))
        conn.execute(
            "INSERT INTO entity(id, kind, canonical_name, created_at, "
            "updated_at) VALUES (?,?,?,?,?)",
            (cls.legal, "service", LEGAL, now, now))
        conn.commit()
        conn.close()

    def _run(self, args):
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(STORE_PY)] + args,
            capture_output=True, text=True, encoding="utf-8",
            env=self.env, cwd=str(REPO))

    def test_entity_list_json_masks_assignment_names(self):
        p = self._run(["entity-list", "--json"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn(ASSIGN_RAW, p.stdout)
        self.assertNotIn(ASSIGN_RAW2, p.stdout)
        self.assertIn(MASK, p.stdout)
        self.assertIn(LEGAL, p.stdout)

    def test_entity_list_human_masks_assignment_names(self):
        p = self._run(["entity-list"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn(ASSIGN_RAW, p.stdout)
        self.assertNotIn(ASSIGN_RAW2, p.stdout)
        self.assertIn(MASK, p.stdout)
        self.assertIn(LEGAL, p.stdout)

    def test_entity_merge_dry_run_masks_assignment_name(self):
        p = self._run(["entity-merge", "--from", self.svc,
                       "--to", self.legal])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn(ASSIGN_RAW2, p.stdout)
        self.assertIn(MASK, p.stdout)
        self.assertIn(LEGAL, p.stdout)

    def test_reconcile_dry_json_shape_unchanged(self):
        """Impl-review nit: the dry-run envelope must NOT gain merged_rows."""
        p = self._run(["reconcile-entity-aliases", "--dry-run", "--json"])
        self.assertEqual(p.returncode, 0, p.stderr)
        payload = json.loads(p.stdout)
        self.assertNotIn("merged_rows", payload,
                         "dry-run envelope must not gain an apply-mode key")


class AssignmentShapeHelperTest(unittest.TestCase):

    def test_short_assignment_values_mask_locally(self):
        m = entity_mod._mask_entity_display
        for shaped in (ASSIGN_RAW, ASSIGN_RAW2, "api-key=prod", "api_key=x",
                       "apikey=prod", "secret=ab", "token=1", "auth=x1"):
            self.assertEqual(m(shaped), MASK, shaped)
        for legal in ("tokenized-service", "authentication-service",
                      "author", "secretariat", "api.example.com",
                      "localhost:8080", "server:8080", "x=y"):
            self.assertEqual(m(legal), legal, legal)


if __name__ == "__main__":
    unittest.main()
