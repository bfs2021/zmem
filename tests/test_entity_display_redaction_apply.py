"""Supplementary display-masking coverage beyond the frozen C1/C2 files
(plan-critic r1 blocker 4): apply-mode legs, the entity-merge collision
renderer, suffix/owner_name masking, import-failure fault injection, and the
over-masking negative pins (blocker 2). GREEN after the display-masking fix;
the frozen C1/C2 files stay byte-identical to their checkpoint blobs.

Every apply test seeds its OWN scratch store (apply legs are stateful and
idempotent re-runs render empty change sets, so shared state would be
vacuous)."""

import atexit
import io
import json
import os
import shutil
import contextlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import _store_guard  # noqa: F401  (issue #244 isolation guard)
import uuid
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
STORE_PY = REPO / "skills" / "memory" / "scripts" / "store.py"
SCRIPTS = REPO / "skills" / "memory" / "scripts"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "storelib"))

from storelib import entity as entity_mod  # noqa: E402

MASK = "[REDACTED_SECRET]"
RAW = "password:12345"
RAW2 = "token:98765"
RAW_ALIAS = "token-acme"
RAW_ORPHAN = "host:token-acme"          # mergeable (owner owns suffix alias)
RAW_ORPHAN_NOOWNER = "service:secret-live"  # no owner: survives --apply
LEGAL = "localhost:8080"
LEGAL2 = "server:8080"
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


def _seed():
    """Fresh scratch store: one real add + the probe fixture rows."""
    td = tempfile.mkdtemp(prefix="zmem455apply_")
    atexit.register(shutil.rmtree, td, True)
    env = dict(os.environ)
    for k in ("ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
              "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_ALLOW_REAL_STORE",
              "ZMEM_REMOTE_URL", "ZMEM_NAMESPACE", "ZMEM_SESSION",
              "ZMEM_SESSION_ID", "ZMEM_CONSUME_AGENT_ID",
              "ZMEM_DELEGATION_AGENT_ID"):
        env.pop(k, None)
    env["ZMEM_STORE"] = str(Path(td) / "store.sqlite")
    env["ZMEM_DATA"] = td
    env["ZMEM_MODELS_DIR"] = str(Path(td) / "models-absent")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    p = subprocess.run(
        [sys.executable, "-X", "utf8", str(STORE_PY), "add", "--type",
         "fact", "--content", "supplementary seed mentions localhost:8080",
         "--tags", "entity:host:localhost:8080", "--namespace",
         "user:probe455b"],
        capture_output=True, text=True, encoding="utf-8", env=env,
        cwd=str(REPO), timeout=120)
    assert p.returncode == 0, p.stderr
    sfx = uuid.uuid4().hex[:8]
    conn = sqlite3.connect(env["ZMEM_STORE"])
    now = "2026-11-11T00:00:00Z"
    rows = [
        (f"e-cred-{sfx}", "other", RAW),
        (f"e-svc-{sfx}", "service", RAW2),
        (f"e-legal-{sfx}", "service", LEGAL),
        (f"e-legal2-{sfx}", "host", LEGAL2),
        (f"e-orphan-{sfx}", "other", RAW_ORPHAN),
        (f"e-orphan-n-{sfx}", "other", RAW_ORPHAN_NOOWNER),
    ]
    for eid, kind, name in rows:
        conn.execute(
            "INSERT INTO entity(id, kind, canonical_name, created_at, "
            "updated_at) VALUES (?,?,?,?,?)", (eid, kind, name, now, now))
    conn.execute(
        "INSERT INTO entity_alias(entity_id, alias_norm) VALUES (?,?)",
        (f"e-legal-{sfx}", RAW))
    conn.execute(
        "INSERT INTO entity_alias(entity_id, alias_norm) VALUES (?,?)",
        (f"e-legal2-{sfx}", RAW_ALIAS))
    conn.commit()
    conn.close()
    return env, sfx


def _run(args, env):
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(STORE_PY)] + args,
        capture_output=True, text=True, encoding="utf-8", env=env,
        cwd=str(REPO), timeout=120)


class ReclassifyApplyMaskingTest(unittest.TestCase):
    """reclassify --apply envelopes mask exactly like dry-run."""

    def test_apply_json_masks_change_names(self):
        env, _ = _seed()
        p = _run(["reclassify-entities", "--apply", "--json"], env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn(RAW, p.stdout)
        self.assertIn(MASK, p.stdout)
        payload = json.loads(p.stdout)
        self.assertTrue(payload["changes"], "fresh fixture must change")

    def test_apply_human_masks_change_names(self):
        env, _ = _seed()
        p = _run(["reclassify-entities", "--apply"], env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn(RAW, p.stdout)
        self.assertIn(MASK, p.stdout)


class ReconcileApplyMaskingTest(unittest.TestCase):
    """reconcile --apply: merged_rows masked; surviving no-owner orphan
    masked in name AND suffix, in both output modes."""

    def test_apply_json_masks_merged_rows_and_surviving_suffix(self):
        env, _ = _seed()
        p = _run(["reconcile-entity-aliases", "--apply", "--json"], env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn(RAW_ORPHAN, p.stdout)
        self.assertNotIn(RAW_ORPHAN_NOOWNER, p.stdout)
        self.assertNotIn("secret-live", p.stdout)
        self.assertIn(MASK, p.stdout)
        payload = json.loads(p.stdout)
        self.assertTrue(payload["merged_rows"], "mergeable orphan must merge")
        for row in payload["merged_rows"]:
            self.assertNotEqual(row.get("name"), RAW_ORPHAN)
        survivors = [r for r in payload["orphans"]
                     if r.get("suffix") == MASK or r.get("name") == MASK]
        self.assertTrue(survivors, "no-owner orphan must remain, masked")
        # the apply really committed: the mergeable orphan is gone
        conn = sqlite3.connect(env["ZMEM_STORE"])
        n = conn.execute("SELECT COUNT(*) FROM entity WHERE canonical_name=?",
                         (RAW_ORPHAN,)).fetchone()[0]
        conn.close()
        self.assertEqual(n, 0)

    def test_apply_human_masks_remaining_orphans(self):
        env, _ = _seed()
        p = _run(["reconcile-entity-aliases", "--apply"], env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn(RAW_ORPHAN, p.stdout)
        self.assertNotIn("secret-live", p.stdout)
        self.assertIn(MASK, p.stdout)


class MaskHelperBoundaryTest(unittest.TestCase):
    """owner_name None passthrough + the exact-token boundary matrix."""

    def test_none_passthrough(self):
        self.assertIsNone(entity_mod._mask_entity_display(None))
        self.assertEqual(entity_mod._mask_entity_display(LEGAL), LEGAL)
        self.assertEqual(entity_mod._mask_entity_display(RAW), MASK)

    def test_boundary_values(self):
        m = entity_mod._mask_entity_display
        for legal in ("tokenized-service", "authentication-service",
                      "author", "secretariat", "api.example.com",
                      "localhost:8080", "server:8080"):
            self.assertEqual(m(legal), legal, legal)
        for shaped in (RAW, RAW2, RAW_ORPHAN, RAW_ALIAS,
                       "api-key:prod", "api_key:x", "apikey:prod",
                       "secret-sauce:1", "auth-service"):
            self.assertEqual(m(shaped), MASK, shaped)


class ImportFailureFailClosedTest(unittest.TestCase):
    """A broken shared registry must never produce a raw echo."""

    def test_registry_failure_fails_closed(self):
        real_import = __builtins__["__import__"] if isinstance(
            __builtins__, dict) else __builtins__.__import__

        def blocked(name, *a, **k):
            if name == "redaction":
                raise ImportError("injected registry failure")
            return real_import(name, *a, **k)

        with mock.patch("builtins.__import__", side_effect=blocked):
            # head-rule shape: masks without consulting the registry
            self.assertEqual(entity_mod._mask_entity_display(RAW), MASK)
            # shared-registry-only shape: FAILS CLOSED (raises), never raw
            with self.assertRaises(entity_mod._DisplayRedactionUnavailable):
                entity_mod._mask_entity_display(AWS_KEY)


class MergeCollisionRendererTest(unittest.TestCase):
    """entity-merge dry-run masks the alias-collision join (display seam).

    alias_norm is globally UNIQUE, so a real store cannot hold a collision;
    this drives cmd_entity_merge in-process against a stub connection that
    replants the collision rows — exercising ONLY the renderer contract."""

    def test_collision_join_is_masked(self):
        frm = {"id": "f1", "kind": "service", "canonical_name": RAW2}
        to = {"id": "t1", "kind": "service", "canonical_name": LEGAL}

        class Cur:
            def __init__(self, rows):
                self._rows = rows

            def fetchone(self):
                return self._rows[0] if self._rows else None

            def fetchall(self):
                return list(self._rows)

        class StubConn:
            def execute(self, sql, params=()):
                if sql.startswith("SELECT * FROM entity"):
                    return Cur([frm] if params[0] == "f1" else [to])
                if sql.startswith("SELECT alias_norm"):
                    # from-side and to-side share the credential alias
                    return Cur([[RAW_ALIAS]])
                if sql.startswith("SELECT memory_id"):
                    return Cur([])
                raise AssertionError(f"unexpected sql: {sql}")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = entity_mod.cmd_entity_merge(StubConn(), "f1", "t1")
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertNotIn(RAW2, out)
        self.assertNotIn(RAW_ALIAS, out)
        self.assertIn(MASK, out)
        self.assertIn(LEGAL, out)


if __name__ == "__main__":
    unittest.main()
