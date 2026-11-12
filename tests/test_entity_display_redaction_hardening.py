"""Issue #455 display-masking hardening legs ported from PR #456 (closed
superseded; owner ruling 2026-11-11): the fail-closed table across every
report command entry point, the apply-guard committed-success proof on
BOTH apply arms, and the pwd/private_key boundary pins.

All legs are ADAPTED to this branch's fail-closed mechanism: the local
head rule masks head-token names WITHOUT the registry, and every other
value ends in the lazily-imported shared-registry consultation, which
raises ``_DisplayRedactionUnavailable`` when the import fails. The
read-only renderers catch it before any stdout write (exit 2, empty
stdout, stderr note); the apply arms flow through
``_guard_committed_rendering`` (committed work reports success from
committed state on a render-only failure). Fixtures therefore seed a
REGISTRY-CONSULTING value (api.example.com / the benign reconcile orphan)
on every arm — an all-head-rule store never touches the registry.

Run: python tests/test_entity_display_redaction_hardening.py
"""

import _store_guard  # noqa: F401  (issue #244 isolation guard)

import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable
sys.path.insert(0, str(SCRIPTS_DIR))

MASK = "[REDACTED_SECRET]"
FIXED_TS = "2026-11-11T00:00:00Z"
ID_CRED = "11111111-0000-4000-8000-000000000001"    # other  / password:12345
ID_HOSTY = "11111111-0000-4000-8000-000000000002"   # other  / api.example.com
ID_OWNER = "11111111-0000-4000-8000-000000000003"   # service / auth:4242
ID_ORPHAN = "11111111-0000-4000-8000-000000000004"  # other  / service:auth:4242
ID_BENIGN_OWNER = "11111111-0000-4000-8000-000000000020"   # host / build-box
ID_BENIGN_ORPHAN = "11111111-0000-4000-8000-000000000021"  # other / host:build-box


def _base_env(store_path: str) -> dict:
    env = dict(os.environ)
    for k in list(env):
        if k.startswith(("CLAUDE_PLUGIN_", "ZCODE_PLUGIN_")):
            env.pop(k, None)
    env["ZMEM_STORE"] = str(store_path)
    env["ZMEM_DATA"] = str(Path(store_path).parent)
    env["ZMEM_MODELS_DIR"] = str(Path(store_path).parent / "no-models")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    for k in ("ZMEM_BACKUP_DIR", "ZMEM_MMR_LAMBDA", "ZMEM_ENTITY_KIND_WEIGHTS",
              "ZMEM_ALLOW_NEWER_SCHEMA"):
        env.pop(k, None)
    return env


_REAL_IMPORT = __builtins__["__import__"] if isinstance(
    __builtins__, dict) else __builtins__.__import__


def _blocked_redaction_import(name, *a, **k):
    if name == "redaction":
        raise ImportError("injected registry failure (fixture)")
    return _REAL_IMPORT(name, *a, **k)


class _PortStore(unittest.TestCase):
    """Scratch store seeding shared by the ported legs."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-entmask-port-")
        self.store = str(Path(self.tmp) / "store.sqlite")
        self.env = _base_env(self.store)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        r = subprocess.run([PYTHON, "-X", "utf8", str(STORE_PY), "init"],
                           capture_output=True, env=self.env, timeout=120)
        if r.returncode != 0:
            self.fail("store.py init failed; stderr tail: "
                      f"{r.stderr.decode('utf-8', 'replace')[-300:]!r}")
        conn = sqlite3.connect(self.store)
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.executemany(
            "INSERT INTO entity VALUES (?,?,?,?,?)",
            [(ID_CRED, "other", "password:12345", FIXED_TS, FIXED_TS),
             (ID_HOSTY, "other", "api.example.com", FIXED_TS, FIXED_TS),
             (ID_OWNER, "service", "auth:4242", FIXED_TS, FIXED_TS),
             (ID_ORPHAN, "other", "service:auth:4242", FIXED_TS, FIXED_TS),
             (ID_BENIGN_OWNER, "host", "build-box", FIXED_TS, FIXED_TS),
             (ID_BENIGN_ORPHAN, "other", "host:build-box", FIXED_TS,
              FIXED_TS)])
        conn.executemany(
            "INSERT INTO entity_alias VALUES (?,?)",
            [(ID_OWNER, "auth:4242"), (ID_BENIGN_OWNER, "build-box")])
        conn.commit()
        conn.close()

    def _open(self, suffix: str) -> sqlite3.Connection:
        """A store COPY for one mutating arm (arms must not consume each
        other's candidates: a reclassify apply re-kinds the orphan row and
        empties the reconcile candidate set)."""
        copy = str(Path(self.tmp) / suffix)
        for f in Path(self.tmp).glob("store.sqlite*"):
            shutil.copyfile(f, copy + f.name[len("store.sqlite"):])
        conn = sqlite3.connect(copy)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn


class FailClosedEveryEntryTest(_PortStore):
    """With the shared registry unimportable, every READ-ONLY arm refuses
    (exit 2, ZERO stdout, stderr note) — ported from PR #456's 7-entry
    table, adapted to the exception mechanism. Renderers construct all
    masked values before any stdout write, so the whole report refuses."""

    def test_registry_failure_refuses_every_read_only_entry(self):
        from storelib import entity as ent
        conn = sqlite3.connect(self.store)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        cases = [
            ("entity-list", lambda: ent.cmd_entity_list(conn)),
            ("entity-merge dry-run",
             lambda: ent.cmd_entity_merge(conn, ID_HOSTY, ID_ORPHAN,
                                          confirm=False)),
            ("reclassify dry-run",
             lambda: ent.cmd_reclassify_entities(conn, apply_mode=False)),
            ("reconcile dry-run",
             lambda: ent.cmd_reconcile_entity_aliases(conn, apply_mode=False)),
        ]
        for label, call in cases:
            out, err = io.StringIO(), io.StringIO()
            with mock.patch("builtins.__import__",
                            side_effect=_blocked_redaction_import), \
                    contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                rc = call()
            self.assertEqual(rc, 2, f"{label}: must refuse with exit 2")
            self.assertEqual(out.getvalue(), "",
                             f"{label}: refusal must write zero stdout")
            self.assertIn("refusing", err.getvalue().lower(),
                          f"{label}: stderr refusal note missing")

    def test_registry_failure_apply_arms_report_committed_success(self):
        from storelib import entity as ent
        # fd 1 save/restore (cubic batch-2 P3 / review PRR-013): the apply
        # arms reach _guard_committed_rendering, whose failure path dup2s
        # os.devnull onto PROCESS fd 1 — without the save/restore the rest
        # of the pytest process loses stdout under -s/--capture=sys.
        saved_fd = os.dup(1)
        try:
            # Arm 1 (own store copy): reclassify apply — the
            # api.example.com change row consults the registry at render
            # time; the kind change COMMITS, the report is refused.
            conn = self._open("copy-rc.sqlite")
            out, err = io.StringIO(), io.StringIO()
            with mock.patch("builtins.__import__",
                            side_effect=_blocked_redaction_import), \
                    contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                rc = ent.cmd_reclassify_entities(conn, apply_mode=True)
            self.assertEqual(rc, 0, "apply must report committed success "
                             "when only the render fails")
            self.assertIn("could not be printed", err.getvalue())
            kind = conn.execute("SELECT kind FROM entity WHERE id=?",
                                (ID_HOSTY,)).fetchone()[0]
            self.assertEqual(kind, "host",
                             "the kind change must have committed")
            # Arm 2 (fresh store copy): reconcile apply — the benign
            # orphan merge COMMITS, the report is refused.
            conn2 = self._open("copy-rec.sqlite")
            out2, err2 = io.StringIO(), io.StringIO()
            with mock.patch("builtins.__import__",
                            side_effect=_blocked_redaction_import), \
                    contextlib.redirect_stdout(out2), \
                    contextlib.redirect_stderr(err2):
                rc2 = ent.cmd_reconcile_entity_aliases(conn2,
                                                       apply_mode=True)
            self.assertEqual(rc2, 0)
            self.assertIn("could not be printed", err2.getvalue())
            still = conn2.execute("SELECT COUNT(*) FROM entity WHERE id=?",
                                  (ID_BENIGN_ORPHAN,)).fetchone()[0]
            self.assertEqual(still, 0,
                             "the orphan merge must have committed")
        finally:
            os.dup2(saved_fd, 1)
            os.close(saved_fd)


class ApplyGuardBothArmsTest(_PortStore):
    """A masking EXPLOSION inside the guarded apply render (not just a
    registry refusal) must land in _guard_committed_rendering: committed
    work reports success from committed state, never failure after commit.
    Ported from PR #456's round-2 implementation-review finding."""

    def test_mask_explosion_reports_committed_success_both_arms(self):
        from storelib import entity as ent
        orig = ent._mask_entity_display

        def _boom(*a, **k):
            raise RuntimeError("mask explosion (fixture)")

        ent._mask_entity_display = _boom
        self.addCleanup(setattr, ent, "_mask_entity_display", orig)
        saved_fd = os.dup(1)
        try:
            # Arm 1 (own store copy): reclassify apply commits, render
            # explodes inside the guard.
            conn = self._open("copy-boom-rc.sqlite")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                rc = ent.cmd_reclassify_entities(conn, apply_mode=True)
            self.assertEqual(rc, 0)
            self.assertIn("could not be printed", err.getvalue())
            kind = conn.execute("SELECT kind FROM entity WHERE id=?",
                                (ID_CRED,)).fetchone()[0]
            self.assertEqual(kind, "service")
            # Arm 2 (fresh store copy, orphan still kinded other): the
            # reconcile merge commits, render explodes inside the guard.
            conn2 = self._open("copy-boom-rec.sqlite")
            out2, err2 = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out2), \
                    contextlib.redirect_stderr(err2):
                rc2 = ent.cmd_reconcile_entity_aliases(conn2, apply_mode=True)
            self.assertEqual(rc2, 0)
            self.assertIn("could not be printed", err2.getvalue())
            still = conn2.execute("SELECT COUNT(*) FROM entity WHERE id=?",
                                  (ID_BENIGN_ORPHAN,)).fetchone()[0]
            self.assertEqual(still, 0)
        finally:
            os.dup2(saved_fd, 1)
            os.close(saved_fd)


class BoundaryPinsTest(unittest.TestCase):
    """Registry-parity head pins (feedback round: cubic batch-1 P1 +
    review PRR-004): the local exact-token head set now carries EVERY
    key-head family of redaction.py's credential pattern (pwd,
    private-key/private_key added to the pre-existing password/passwd/
    secret/token/api-key/api_key/apikey/credential/auth), so a SHORT value
    below the registry's 8-char floor still masks. Glued non-registry
    spellings and undotted suffix-compound keys stay the documented
    unmasked boundary (pinned here as controls)."""

    def test_registry_parity_heads_mask_even_short(self):
        from storelib import entity as ent
        m = ent._mask_entity_display
        for shaped in ("pwd:123", "pwd:12345", "private_key:9",
                       "private-key:4242", "private_key:supersecret99",
                       "pwd=abc", "db.private-key:1"):
            self.assertEqual(m(shaped), MASK, shaped)

    def test_non_registry_boundary_stays_legal(self):
        from storelib import entity as ent
        m = ent._mask_entity_display
        for legal in ("secretkey=abcdefgh", "tokenkey=abcdefgh",
                      "credentialkey=abcdefgh", "passphrase=abcdefgh",
                      "pgpassword=abc123", "tokenized-service",
                      "authentication-service", "author", "secretariat"):
            self.assertEqual(m(legal), legal, legal)

    def test_unicode_and_quote_families_mask(self):
        # review PRR-003 + PRR-005 (executed-probe families from the
        # swarm-pr-review round, fixed by the NFKC fold + quote
        # separators): fullwidth separators, zero-width-joiner glue, and
        # quoted keys all mask; a fullwidth-bearing ordinary name stays
        # legal.
        from storelib import entity as ent
        m = ent._mask_entity_display
        for shaped in ("password\uff1a12345", "password\uff1d12345",
                       "pass\u200dword:12345", '{"password": "value"}',
                       '{"password": "supersecretvalue"}',
                       "'password=x'", '"password":"supersecretvalue"'):
            self.assertEqual(m(shaped), MASK, shaped)
        for legal in ("api.example.com", "localhost:8080"):
            self.assertEqual(m(legal), legal, legal)


class OwnerNameMaskingTest(_PortStore):
    """PRR-002: the reconcile orphans[].owner_name masking key has a
    DEDICATED credential-bearing assertion — dropping that one key from
    the renderer's masked-key list must turn this test RED (the reviewer's
    mutation left five of six suites green because only a coincidental
    substring assert caught it)."""

    def test_reconcile_dry_json_masks_credential_owner_name(self):
        from storelib import entity as ent
        # Seed a credential-shaped OWNER canonical name; the orphan's
        # suffix alias resolves to it.
        conn = sqlite3.connect(self.store)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute(
            "INSERT INTO entity VALUES (?,?,?,?,?)",
            ("33333333-0000-4000-8000-000000000030", "host",
             "private_key:4242", FIXED_TS, FIXED_TS))
        conn.execute(
            "INSERT INTO entity_alias VALUES (?,?)",
            ("33333333-0000-4000-8000-000000000030", "private_key:4242"))
        conn.execute(
            "INSERT INTO entity VALUES (?,?,?,?,?)",
            ("33333333-0000-4000-8000-000000000031", "other",
             "host:private_key:4242", FIXED_TS, FIXED_TS))
        conn.commit()
        conn.close()
        # The dry-run JSON arm writes LF-only bytes via sys.stdout.buffer,
        # so this leg drives the REAL CLI via subprocess (an in-process
        # redirect_stdout(StringIO) cannot capture a .buffer write).
        p = subprocess.run(
            [PYTHON, "-X", "utf8", str(STORE_PY),
             "reconcile-entity-aliases", "--dry-run", "--json"],
            capture_output=True, text=True, encoding="utf-8",
            env=self.env, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        payload = json.loads(p.stdout)
        row = next(o for o in payload["orphans"]
                   if o["id"] == "33333333-0000-4000-8000-000000000031")
        self.assertEqual(row["owner_name"], MASK,
                         "owner_name must be masked whole when "
                         "credential-shaped (dedicated PRR-002 assertion)")
        self.assertEqual(row["suffix"], MASK,
                         "the credential-shaped suffix must mask too")


class WholeValueCliContractTest(_PortStore):
    """PRR-008: the CLI-seam oracles must distinguish WHOLE-value masking
    from partial masking — structural equality (parsed field == MASK),
    not substring presence, so a label-preserving mutant
    ('password:[REDACTED_SECRET]') or a label-dropping mutant
    ('[REDACTED_SECRET]:12345') fails here even though substring legs
    stay green."""

    def test_entity_list_json_whole_value_equality(self):
        p = subprocess.run(
            [PYTHON, "-X", "utf8", str(STORE_PY), "entity-list", "--json"],
            capture_output=True, text=True, encoding="utf-8",
            env=self.env, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        items = {e["id"]: e for e in json.loads(p.stdout)}
        self.assertEqual(items[ID_CRED]["name"], MASK)
        self.assertEqual(items[ID_CRED]["aliases"], [],
                         "the seed's credential entity has no alias rows")
        self.assertEqual(items[ID_OWNER]["name"], MASK)
        self.assertEqual(items[ID_OWNER]["aliases"], [MASK])
        self.assertEqual(items[ID_BENIGN_OWNER]["name"], "build-box")

    def test_reclassify_dry_json_whole_value_equality(self):
        p = subprocess.run(
            [PYTHON, "-X", "utf8", str(STORE_PY), "reclassify-entities",
             "--dry-run", "--json"],
            capture_output=True, text=True, encoding="utf-8",
            env=self.env, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        payload = json.loads(p.stdout)
        by_id = {c["id"]: c["name"] for c in payload["changes"]}
        self.assertEqual(by_id[ID_CRED], MASK)
        self.assertNotIn("password", (c["name"] for c in payload["changes"]))

    def test_reconcile_dry_json_suffix_whole_value_equality(self):
        p = subprocess.run(
            [PYTHON, "-X", "utf8", str(STORE_PY),
             "reconcile-entity-aliases", "--dry-run", "--json"],
            capture_output=True, text=True, encoding="utf-8",
            env=self.env, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        payload = json.loads(p.stdout)
        row = next(o for o in payload["orphans"]
                   if o["id"] == ID_ORPHAN)
        self.assertEqual(row["suffix"], MASK)


class DisplayMaskPerfTest(unittest.TestCase):
    """PRR-001 regression pin: the mask predicate must stay LINEAR in the
    input — the pre-fix assignment loop re-split the whole prefix once per
    ':'/'=' separator (quadratic; 16K separators cost ~46s per mask call).
    Machine-independent scaling-ratio assertion (the
    ReDosRegressionTest pattern): doubling the input must not more than
    double the time, with a generous absolute backstop. Bounded sizes so
    the pin itself stays cheap."""

    def test_mask_scales_linearly_on_separator_dense_input(self):
        from storelib import entity as ent
        import time
        small = "aa:" * 2000      # 2K separators
        large = "aa:" * 8000      # 8K separators (4x the work)
        t0 = time.perf_counter()
        ent._mask_entity_display(small)
        t_small = time.perf_counter() - t0
        t0 = time.perf_counter()
        ent._mask_entity_display(large)
        t_large = time.perf_counter() - t0
        self.assertLess(t_large, 2.0,
                        f"mask is super-linear at 8K separators: "
                        f"{t_large:.3f}s (quadratic form measured ~50s)")
        self.assertLess(t_large / max(t_small, 1e-6), 8.0,
                        f"4x the input more than 8x the time "
                        f"({t_small:.4f}s -> {t_large:.4f}s): quadratic "
                        "amplification is back")


if __name__ == "__main__":
    unittest.main(verbosity=2)
