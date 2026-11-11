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
# `=`-shaped values reach the store via CONTENT backticks (the entity: tag
# grammar cuts the name at '='), and via legacy rows; both paths are driven
# below. Short values (<8 chars) keep the shared registry's floor out of
# the way so only the local head rule can mask them.
ASSIGN_RAW = "DB_PASSWORD=12345"   # backtick-content / legacy-INSERT shape
ASSIGN_RAW2 = "password=1234"      # short value, head rule only
ASSIGN_SPACED = "DB_PASSWORD = 12345"  # impl-review r2: whitespace around =
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

    def test_whitespace_around_separator_masks(self):
        """Impl-review r2: whitespace adjacent to an existing separator is
        absorbed (`DB_PASSWORD = 12345`), while BARE whitespace without a
        separator stays unmasked (narrow fix)."""
        m = entity_mod._mask_entity_display
        for shaped in (ASSIGN_SPACED, "password = 1234", "api_key = x",
                       "password : 12345", "secret = ab"):
            self.assertEqual(m(shaped), MASK, shaped)
        for legal in ("server :8080", "localhost : 8080", "password 12345",
                      "token x"):
            self.assertEqual(m(legal), legal, legal)

    def test_dot_joined_assignment_keys_mask(self):
        """Impl-review r3: a dot-joined assignment KEY containing a secret
        head masks (`db.password=12345`), while dotted hosts and dotted
        keys without a head stay unmasked. Never a global dot split.
        Impl-review r4: an EARLIER separator must not hide the key
        (`redis:db.password=12345`, `--config=db.password=12345`).
        Impl-review r5: the dot-joined SPELLING of a documented head masks
        (`api.key=12345` — pairs join across dots) and whitespace around a
        dot trims (`db. password=12345`); `-`/`_` stay hard joins
        (`db.password-prod=1` unmasked)."""
        m = entity_mod._mask_entity_display
        for shaped in ("db.password=12345", "DB.PASSWORD=67890",
                       "spring.datasource.password=12345",
                       "db.password: 12345", "redis:db.password=12345",
                       "--config=db.password=12345",
                       "host:db.password=12345", "api.key=12345",
                       "API.KEY=12345", "redis:api.key=12345",
                       "api.key: 12345", "db. password=12345",
                       "--config=api.key=12345",
                       "--config=password.db=12345", "--api.key=12345",
                       "--password.db=12345", "--set=secret.val=12345",
                       "flag=token.id=12345"):
            self.assertEqual(m(shaped), MASK, shaped)
        for legal in ("api.example.com", "auth.example.com",
                      "acme.corp:8080", "localhost:8080", "x.password.corp",
                      "db.password-prod=1", "db.password_prod=1"):
            self.assertEqual(m(legal), legal, legal)


class WhitespaceShapeCliTest(unittest.TestCase):
    """Real write-path mint (backtick content) + legacy INSERT for the
    whitespace-adjacent shape; entity-list and entity-merge render masked."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp(prefix="zmem455ws_")
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
        p = subprocess.run(
            [sys.executable, "-X", "utf8", str(STORE_PY), "add", "--type",
             "fact", "--content",
             f"env probe mentions `{ASSIGN_SPACED}` and {LEGAL}",
             "--tags", "entity:host:localhost:8080", "--namespace",
             "user:probe455d"],
            capture_output=True, text=True, encoding="utf-8", env=env,
            cwd=str(REPO))
        assert p.returncode == 0, p.stderr
        # legacy direct-INSERT copy of the same shape on a second entity
        cls.ws = "e-ws-" + uuid.uuid4().hex[:8]
        cls.legal = "e-legal-" + uuid.uuid4().hex[:8]
        conn = sqlite3.connect(env["ZMEM_STORE"])
        now = "2026-11-11T00:00:00Z"
        conn.execute(
            "INSERT INTO entity(id, kind, canonical_name, created_at, "
            "updated_at) VALUES (?,?,?,?,?)",
            (cls.ws, "host", ASSIGN_SPACED, now, now))
        conn.execute(
            "INSERT INTO entity(id, kind, canonical_name, created_at, "
            "updated_at) VALUES (?,?,?,?,?)",
            (cls.legal, "host", "server :8080", now, now))
        conn.commit()
        conn.close()

    def _run(self, args):
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(STORE_PY)] + args,
            capture_output=True, text=True, encoding="utf-8",
            env=self.env, cwd=str(REPO))

    def test_entity_list_masks_whitespace_shapes_both_modes(self):
        for args in (["entity-list", "--json"], ["entity-list"]):
            p = self._run(args)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertNotIn(ASSIGN_SPACED, p.stdout)
            self.assertNotIn(ASSIGN_SPACED.casefold(), p.stdout.casefold())
            self.assertIn(MASK, p.stdout)

    def test_entity_list_keeps_legal_spaced_name(self):
        p = self._run(["entity-list"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("server :8080", p.stdout)

    def test_entity_merge_masks_whitespace_name(self):
        p = self._run(["entity-merge", "--from", self.ws,
                       "--to", self.legal])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn(ASSIGN_SPACED, p.stdout)
        self.assertIn(MASK, p.stdout)

    def test_dot_key_shapes_mask_via_real_write_path(self):
        """Impl-review r3+r4 E2E: `db.password=12345` and the
        earlier-separator forms minted from backtick content must not echo
        raw on entity-list or entity-merge."""
        raws = ("db.password=12345", "redis:db.password=12345",
                "--config=db.password=12345")
        env = dict(self.env)
        p = subprocess.run(
            [sys.executable, "-X", "utf8", str(STORE_PY), "add", "--type",
             "fact", "--content",
             "config note: set `" + "` and `".join(raws) + "` here",
             "--namespace", "user:probe455e"],
            capture_output=True, text=True, encoding="utf-8", env=env,
            cwd=str(REPO))
        self.assertEqual(p.returncode, 0, p.stderr)
        for args in (["entity-list", "--json"], ["entity-list"]):
            q = self._run(args)
            self.assertEqual(q.returncode, 0, q.stderr)
            for raw in raws:
                self.assertNotIn(raw, q.stdout, (args, raw))
        q = self._run(["entity-list", "--json"])
        self.assertIn(MASK, q.stdout)

    def test_legacy_reconcile_dot_key_masks(self):
        """Impl-review r4: a legacy `host:db.password=12345` other-row
        (pre-widening shape) must not echo raw on reconcile dry-run."""
        orphan = "host:db.password=12345"
        conn = sqlite3.connect(self.env["ZMEM_STORE"])
        eid = "e-orp-" + uuid.uuid4().hex[:8]
        now = "2026-11-11T00:00:00Z"
        conn.execute(
            "INSERT INTO entity(id, kind, canonical_name, created_at, "
            "updated_at) VALUES (?,?,?,?,?)", (eid, "other", orphan, now,
                                               now))
        conn.commit()
        conn.close()
        try:
            for args in (["reconcile-entity-aliases", "--dry-run", "--json"],
                         ["reconcile-entity-aliases", "--dry-run"]):
                q = self._run(args)
                self.assertEqual(q.returncode, 0, q.stderr)
                self.assertNotIn(orphan, q.stdout, args)
                self.assertNotIn("db.password=12345", q.stdout, args)
        finally:
            conn = sqlite3.connect(self.env["ZMEM_STORE"])
            conn.execute("DELETE FROM entity WHERE id=?", (eid,))
            conn.commit()
            conn.close()


if __name__ == "__main__":
    unittest.main()
