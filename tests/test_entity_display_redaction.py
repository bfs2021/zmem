"""Issue #455: display-time masking of credential-shaped entity names.

REGENERATED 2026-11-11 round 4 (plan-critic round-3 findings): non-vacuous
reconcile apply legs (a shared-caught mergeable orphan asserted masked in
merged_rows + a no-owner orphan asserted masked in post-apply orphans and
apply-human output), the apply-guard leg now sets row_factory and
save/restores fd 1 around _guard_committed_rendering's devnull dup2, ALL
stdout-derived and raw-field assertions route through curated boolean
gates with static messages, and the degraded preflight legs cover every
command entry point (four read-only arms + three mutating arms with
unchanged-state proofs).

Contract under test (issue #455, revised contract): the four entity REPORT
commands mask credential-shaped names/aliases at DISPLAY time only —
engines and stored rows are untouched. A value is credential-shaped iff
EITHER

  (a) redaction.redact_secret_like_text(value) returns a changed string
      (loaded via the full two-step storelib/write.py bootstrap; on total
      ImportError the report command REFUSES: read-only arms exit 2 with a
      stderr note and zero stdout; apply arms refuse before the engine
      runs), OR
  (b) the value's casefolded strip matches
        ^(?:[a-z0-9]+[-_])*
          (?:password|passwd|secret|token|api[_-]?key|credential|auth)
          (?:[-_][a-z0-9]+)*[-_]?:[0-9]{1,9}$
      (the legacy pre-v16 short-digits shape the shared helper's \S{8,}
      floor misses; the separator before ':' is optional).

Masked form: (a)-shapes use the shared redactor's own output; (b)-shapes
keep the original-case head and replace the payload with [REDACTED_SECRET]
('Password:12345' -> 'Password:[REDACTED_SECRET]'). ids, kinds, counts,
and prefixes are never masked. Legal names (localhost:8080, server:8080,
postgres:5432, api-service, server.log, my-script.py, build-box, the
kind-prefixed 'host:build-box'/'service:auth:4242' composites, and the
undelimited-head 'mytoken:123') are NOT masked — the legal-name side of
the boundary lives in tests/test_entity_display_redaction_legal_names.py
and the helper legs below.

Assertion hygiene rule for this file: every check over captured stdout or
parsed name/alias/suffix/owner fields goes through `self.ck(...)` — a
boolean gate whose failure message names the FIELD, never the values.
Ordinary assertions are reserved for counts, ids, and stored-state
metadata.

Run: python tests/test_entity_display_redaction.py  (no pytest; house
convention — but the file stays pytest-node-selectable: unittest.TestCase
style, no module-level store resolution).
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import contextlib
import io
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

MASK = "[REDACTED_SECRET]"
# Literal expectation constants — the tests must contain these by necessity.
RAW_NAME = "password:12345"            # (b)-shaped: 5-digit payload
RAW_ALIAS = "token:98765"              # (b)-shaped alias on the same entity
RAW_LONGSECRET = "password:supersecret99"  # (a)-shaped: 13-char payload
MASKED_NAME = "password:" + MASK
MASKED_ALIAS = "token:" + MASK
# The reconcile orphan: the kind-prefixed composite is LEGAL as a whole;
# its suffix and owner name are (b)-shaped and must be masked wherever they
# render alone.
LEGAL_ORPHAN = "service:auth:4242"
RAW_SUFFIX = "auth:4242"
MASKED_SUFFIX = "auth:" + MASK
# Extra reconcile orphans seeded into the APPLY-copy legs only (critic
# round 3, F1): a mergeable orphan whose WHOLE name is shared-caught, and a
# no-owner orphan that survives apply and must render masked in the
# remaining-orphans fields.
RAW_CRED_ORPHAN = "host:password:ownersecret99"
MASKED_CRED_ORPHAN = "host:password:" + MASK
RAW_NOOWNER_ORPHAN = "model:secret:supersecret99"
MASKED_NOOWNER_ORPHAN = "model:secret:" + MASK
# Substring-safe blanket set: none of these three occurs inside any legal
# composite this store renders. RAW_SUFFIX (and the two long composites,
# which embed legal prefixes) are deliberately NOT here; every field that
# can render them alone gets per-row/per-field structural gates instead.
RAWS = (RAW_NAME, RAW_ALIAS, RAW_LONGSECRET)

FIXED_TS = "2026-11-11T00:00:00Z"
ID_CRED = "11111111-0000-4000-8000-000000000001"       # other  / password:12345 (+alias token:98765)
ID_HOSTY = "11111111-0000-4000-8000-000000000002"      # other  / api.example.com
ID_OWNER = "11111111-0000-4000-8000-000000000003"      # service / auth:4242 (+alias auth:4242)
ID_ORPHAN = "11111111-0000-4000-8000-000000000004"     # other  / service:auth:4242 (reconcile orphan)
ID_TO = "11111111-0000-4000-8000-000000000006"         # other  / token:98765 (merge target)
ID_LONGSECRET = "11111111-0000-4000-8000-000000000007"  # other / password:supersecret99 (+alias)
# Apply-leg extras (seeded only into the apply copies).
ID_CRED_ORPHAN = "11111111-0000-4000-8000-000000000010"  # other / host:password:supersecret99
ID_CRED_OWNER = "11111111-0000-4000-8000-000000000011"   # host  / password:supersecret99 (+alias)
ID_NOOWNER_ORPHAN = "11111111-0000-4000-8000-000000000012"  # other / model:secret:supersecret99


def _base_env(store_path: str) -> dict:
    """tests/test_entity.py's _base_env pattern, hardened for #455:

    - ZMEM_STORE pinned to the scratch FILE and ZMEM_DATA pinned to the
      scratch DIR (pins, not pops: writer leases and backups must land in
      the scratch area, never the operator's home);
    - CLAUDE_PLUGIN_*/ZCODE_PLUGIN_* leakage stripped;
    - children forced UTF-8 (PYTHONUTF8/PYTHONIOENCODING): reclassify and
      reconcile write JSON as UTF-8 bytes through sys.stdout.buffer while
      human prints follow the locale codepage unless forced.
    """
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


class _MaskStore(unittest.TestCase):
    """Subprocess-driven house pattern: every command is the real CLI."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-entmask-")
        self.store = str(Path(self.tmp) / "store.sqlite")
        self.env = _base_env(self.store)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._seed()

    def _seed(self):
        """One init + direct sqlite seeding (the ReclassifyCliTest pattern):
        exact entity/alias rows, no dependence on extractor eligibility."""
        r = subprocess.run([PYTHON, "-X", "utf8", str(STORE_PY), "init"],
                           capture_output=True, env=self.env, timeout=120)
        if r.returncode != 0:
            self.fail("store.py init failed; stderr tail: "
                      f"{r.stderr.decode('utf-8', 'replace')[-400:]!r}")
        conn = sqlite3.connect(self.store)
        try:
            conn.executemany(
                "INSERT INTO entity(id, kind, canonical_name, created_at, "
                "updated_at) VALUES (?,?,?,?,?)",
                [(ID_CRED, "other", RAW_NAME, FIXED_TS, FIXED_TS),
                 (ID_HOSTY, "other", "api.example.com", FIXED_TS, FIXED_TS),
                 (ID_OWNER, "service", RAW_SUFFIX, FIXED_TS, FIXED_TS),
                 (ID_ORPHAN, "other", LEGAL_ORPHAN, FIXED_TS, FIXED_TS),
                 (ID_TO, "other", RAW_ALIAS, FIXED_TS, FIXED_TS),
                 (ID_LONGSECRET, "other", RAW_LONGSECRET, FIXED_TS, FIXED_TS)])
            conn.executemany(
                "INSERT INTO entity_alias(entity_id, alias_norm) VALUES (?,?)",
                [(ID_CRED, RAW_ALIAS),
                 (ID_OWNER, RAW_SUFFIX),
                 (ID_LONGSECRET, RAW_LONGSECRET)])
            conn.commit()
        finally:
            conn.close()

    def _seed_apply_extras(self, store_path: str) -> None:
        """Add the round-3 reconcile extras to an apply COPY: a mergeable
        orphan whose whole name is shared-caught (with its owner), and a
        no-owner orphan that survives the apply."""
        conn = sqlite3.connect(store_path)
        try:
            conn.executemany(
                "INSERT INTO entity(id, kind, canonical_name, created_at, "
                "updated_at) VALUES (?,?,?,?,?)",
                [(ID_CRED_ORPHAN, "other", RAW_CRED_ORPHAN, FIXED_TS,
                  FIXED_TS),
                 (ID_CRED_OWNER, "host", "password:ownersecret99", FIXED_TS,
                  FIXED_TS),
                 (ID_NOOWNER_ORPHAN, "other", RAW_NOOWNER_ORPHAN, FIXED_TS,
                  FIXED_TS)])
            # Distinct alias (the base seed already owns
            # 'password:supersecret99' for ID_LONGSECRET; alias_norm is
            # GLOBALLY unique — plan-critic round 4 executed this fixture
            # and caught the IntegrityError).
            conn.execute(
                "INSERT INTO entity_alias(entity_id, alias_norm) VALUES "
                "(?,?)", (ID_CRED_OWNER, "password:ownersecret99"))
            conn.commit()
        finally:
            conn.close()

    def _copy_store(self, suffix: str) -> str:
        """A running-store COPY (with WAL sidecars) for mutating apply legs."""
        copy = str(Path(self.tmp) / suffix)
        for f in Path(self.tmp).glob("store.sqlite*"):
            shutil.copyfile(f, copy + f.name[len("store.sqlite"):])
        return copy

    def run_store(self, *args, env=None):
        """Run store.py, return decoded stdout. FAILURE MESSAGES NEVER ECHO
        CAPTURED STDOUT: at a masking defect the stdout haystack holds the
        raw credential literals, and unittest longMessage would embed the
        whole haystack in the failure text."""
        r = subprocess.run(
            [PYTHON, "-X", "utf8", str(STORE_PY), *args],
            capture_output=True, env=env or self.env, timeout=120,
        )
        if r.returncode != 0:
            self.fail(f"store.py {' '.join(args)} exited {r.returncode}; "
                      f"stderr tail: "
                      f"{r.stderr.decode('utf-8', 'replace')[-400:]!r}")
        return r.stdout.decode("utf-8")

    # --- curated boolean gates (static messages only; values never echo) --
    def ck(self, condition: bool, what: str):
        if not condition:
            self.fail(f"masking contract violated: {what}")

    def assert_no_raw(self, surface: str, out: str):
        for raw in RAWS:
            self.ck(raw not in out, f"{surface}: raw value leaked to stdout")

    def assert_masked(self, surface: str, out: str, needle: str):
        self.ck(needle in out,
                f"{surface}: masked form missing from stdout")

    def human_rows(self, out: str) -> dict:
        """Parse entity-list human output into {id: line}."""
        rows = {}
        for line in out.splitlines():
            m = re.match(r"^\[([0-9a-f-]+)\]", line)
            if m:
                rows[m.group(1)] = line
        return rows


class EntityReportMaskingTest(_MaskStore):
    """C1 subprocess legs: every report surface, every mode, masked forms."""

    def test_entity_list_human_masks_name_and_alias(self):
        out = self.run_store("entity-list")
        self.assert_no_raw("entity-list human", out)
        self.assert_masked("entity-list human", out, MASKED_NAME)
        self.assert_masked("entity-list human", out, MASKED_ALIAS)
        # Per-row structural gates (RAW_SUFFIX is a substring of the LEGAL
        # orphan composite, so its absence proof is per-row, not blanket).
        rows = self.human_rows(out)
        self.ck(MASKED_NAME in rows.get(ID_CRED, ""),
                "entity-list human: credential entity name unmasked")
        self.ck(MASKED_ALIAS in rows.get(ID_CRED, ""),
                "entity-list human: credential entity alias unmasked")
        self.ck(RAW_SUFFIX not in rows.get(ID_OWNER, ""),
                "entity-list human: owner row leaks the raw suffix name")
        self.ck(MASKED_SUFFIX in rows.get(ID_OWNER, ""),
                "entity-list human: owner name unmasked")
        self.ck(LEGAL_ORPHAN in rows.get(ID_ORPHAN, ""),
                "entity-list human: legal composite wrongly masked or absent")

    def test_entity_list_json_masks_name_and_every_alias(self):
        out = self.run_store("entity-list", "--json")
        self.assert_no_raw("entity-list --json", out)
        items = {e["id"]: e for e in json.loads(out)}
        self.ck(items[ID_CRED]["name"] == MASKED_NAME,
                "entity-list json: credential name field unmasked")
        self.ck(items[ID_CRED]["aliases"] == [MASKED_ALIAS],
                "entity-list json: credential alias list unmasked")
        self.ck(items[ID_LONGSECRET]["name"] == MASKED_NAME,
                "entity-list json: long-secret name field unmasked")
        self.ck(items[ID_OWNER]["name"] == MASKED_SUFFIX,
                "entity-list json: owner name field unmasked")
        self.ck(items[ID_OWNER]["aliases"] == [MASKED_SUFFIX],
                "entity-list json: owner alias list unmasked")
        self.ck(items[ID_ORPHAN]["name"] == LEGAL_ORPHAN,
                "entity-list json: legal composite wrongly masked or absent")
        self.assertEqual(items[ID_CRED]["kind"], "other")  # kinds never masked

    def test_reclassify_dry_run_human_masks_change_names(self):
        out = self.run_store("reclassify-entities", "--dry-run")
        self.assert_no_raw("reclassify --dry-run human", out)
        self.assert_masked(
            "reclassify --dry-run human", out,
            f"[zmem] reclassify: {ID_CRED} '{MASKED_NAME}' other -> service")
        self.ck("scanned=5 changed=4" in out,
                "reclassify dry human: scan/changed counts wrong")
        self.ck(f"[zmem] reclassify: {ID_ORPHAN} '{LEGAL_ORPHAN}' "
                "other -> service" in out,
                "reclassify dry human: legal composite change line wrong")

    def test_reclassify_dry_run_json_masks_change_names(self):
        out = self.run_store("reclassify-entities", "--dry-run", "--json")
        self.assert_no_raw("reclassify --dry-run --json", out)
        parsed = json.loads(out)
        self.assertEqual(parsed["changed"], 4)
        names = {c["name"] for c in parsed["changes"]}
        self.ck(names == {"api.example.com", LEGAL_ORPHAN, MASKED_NAME,
                          MASKED_ALIAS},
                "reclassify dry json: change-name set has an unmasked member")
        for c in parsed["changes"]:
            self.assertIn(c["id"], (ID_CRED, ID_HOSTY, ID_ORPHAN, ID_TO),
                          "ids are never masked: change ids must be raw")

    def test_reclassify_apply_human_masks_change_names_on_store_copy(self):
        copy = self._copy_store("copy-rc-a.sqlite")
        out = self.run_store("reclassify-entities", "--apply",
                             env=_base_env(copy))
        self.assert_no_raw("reclassify --apply human", out)
        self.assert_masked(
            "reclassify --apply human", out,
            f"[zmem] reclassify: {ID_CRED} '{MASKED_NAME}' other -> service")
        self.ck("scanned=5 changed=4" in out,
                "reclassify apply human: scan/changed counts wrong")

    def test_reclassify_apply_json_masks_change_names_on_store_copy(self):
        copy = self._copy_store("copy-rc-j.sqlite")
        out = self.run_store("reclassify-entities", "--apply", "--json",
                             env=_base_env(copy))
        self.assert_no_raw("reclassify --apply --json", out)
        parsed = json.loads(out)
        self.assertEqual(parsed["changed"], 4)
        self.ck(MASKED_NAME in {c["name"] for c in parsed["changes"]},
                "reclassify apply json: change-name masked form missing")
        self.ck(MASKED_ALIAS in {c["name"] for c in parsed["changes"]},
                "reclassify apply json: alias-entity masked form missing")
        # Display-only: the APPLY arm wrote kinds, and the stored
        # canonical_name rows keep the raw bytes.
        conn = sqlite3.connect(copy)
        try:
            names = {r[0] for r in conn.execute(
                "SELECT canonical_name FROM entity")}
        finally:
            conn.close()
        self.assertIn(RAW_NAME, names,
                      "masking is display-time only: stored rows must keep "
                      "the raw canonical_name")
        self.assertIn(RAW_ALIAS, names)

    def test_entity_merge_dry_run_masks_from_and_to_names(self):
        out = self.run_store("entity-merge", "--from", ID_CRED, "--to", ID_TO)
        self.ck("DRY RUN" in out,
                "entity-merge dry human: DRY RUN banner missing")
        self.assert_no_raw("entity-merge dry-run human", out)
        self.assert_masked("entity-merge dry-run human", out,
                           f"name={MASKED_NAME!r}")
        self.assert_masked("entity-merge dry-run human", out,
                           f"name={MASKED_ALIAS!r}")

    def test_reconcile_dry_run_human_masks_and_keeps_legal_composite(self):
        out = self.run_store("reconcile-entity-aliases", "--dry-run")
        self.assert_no_raw("reconcile --dry-run human", out)
        # The kind-prefixed composite name is LEGAL and renders raw; the
        # owner leg prints the (never-masked) owner ID.
        self.ck(f"[zmem] reconcile: {ID_ORPHAN} {LEGAL_ORPHAN!r} "
                f"(service) owner={ID_OWNER}" in out,
                "reconcile dry human: legal composite line wrong")
        self.ck("scanned=1 mergeable=1" in out,
                "reconcile dry human: scan/mergeable counts wrong")

    def test_reconcile_dry_run_json_masks_suffix_and_owner_name(self):
        out = self.run_store("reconcile-entity-aliases", "--dry-run", "--json")
        self.assert_no_raw("reconcile --dry-run --json", out)
        orphans = {o["id"]: o for o in json.loads(out)["orphans"]}
        row = orphans[ID_ORPHAN]
        self.ck(row["name"] == LEGAL_ORPHAN,
                "reconcile dry json: orphan name field wrong (legal "
                "composite must render raw)")
        self.ck(row["suffix"] == MASKED_SUFFIX,
                "reconcile dry json: suffix field unmasked")
        self.ck(row["owner_name"] == MASKED_SUFFIX,
                "reconcile dry json: owner_name field unmasked")
        self.assertEqual(row["owner_id"], ID_OWNER)
        self.assertEqual(row["prefix"], "service")  # never masked

    def test_reconcile_apply_human_masks_merged_and_remaining_on_copy(self):
        copy = self._copy_store("copy-rec-a.sqlite")
        self._seed_apply_extras(copy)
        out = self.run_store("reconcile-entity-aliases", "--apply",
                             env=_base_env(copy))
        self.assert_no_raw("reconcile --apply human", out)
        # Counts: three orphans scanned (legal + cred-mergeable + no-owner),
        # two merged, one skipped for no owner.
        self.ck("scanned=3 merged=2 skipped_no_owner=1" in out,
                "reconcile apply human: scan/merged/skipped counts wrong")
        # The REMAINING no-owner orphan renders in the apply output with a
        # masked name (the human arm prints post-apply orphans).
        self.ck(f"[zmem] reconcile: {ID_NOOWNER_ORPHAN} "
                f"{MASKED_NOOWNER_ORPHAN!r}" in out,
                "reconcile apply human: remaining no-owner orphan name "
                "unmasked")

    def test_reconcile_apply_json_masks_merged_rows_and_remaining(self):
        copy = self._copy_store("copy-rec-j.sqlite")
        self._seed_apply_extras(copy)
        out = self.run_store("reconcile-entity-aliases", "--apply", "--json",
                             env=_base_env(copy))
        self.assert_no_raw("reconcile --apply --json", out)
        parsed = json.loads(out)
        self.assertEqual(parsed["scanned"], 3)
        self.assertEqual(parsed["merged"], 2)
        self.assertEqual(parsed["skipped_no_owner"], 1)
        merged = {m["id"]: m["name"] for m in parsed["merged_rows"]}
        self.ck(merged.get(ID_ORPHAN) == LEGAL_ORPHAN,
                "reconcile apply json: legal merged-row name wrong")
        self.ck(merged.get(ID_CRED_ORPHAN) == MASKED_CRED_ORPHAN,
                "reconcile apply json: credential-shaped merged-row name "
                "unmasked (shared-caught composite)")
        # Non-vacuous remaining-orphans leg: the no-owner orphan SURVIVES
        # the apply and its whole name must be masked.
        remaining = {o["id"]: o for o in parsed["orphans"]}
        self.ck(len(remaining) >= 1,
                "reconcile apply json: expected a surviving no-owner orphan")
        survivor = remaining.get(ID_NOOWNER_ORPHAN)
        self.ck(survivor is not None,
                "reconcile apply json: the no-owner orphan vanished from "
                "the remaining set")
        self.ck(survivor_name_ok(survivor),
                "reconcile apply json: surviving orphan name/suffix fields "
                "unmasked")
        # Display-only: the orphans MOVED (apply is real) but stored names
        # keep raw bytes on the survivors.
        conn = sqlite3.connect(copy)
        try:
            names = {r[0] for r in conn.execute(
                "SELECT canonical_name FROM entity")}
        finally:
            conn.close()
        self.assertIn(RAW_SUFFIX, names,
                      "the surviving owner keeps its raw stored name")
        self.assertIn(RAW_NOOWNER_ORPHAN, names,
                      "the no-owner orphan keeps its raw stored name")
        self.assertNotIn(LEGAL_ORPHAN, names,
                         "the legal orphan row was merged away by the apply")
        self.assertNotIn(RAW_CRED_ORPHAN, names,
                         "the credential orphan row was merged away")

    def test_display_masking_leaves_stored_rows_untouched(self):
        # Every read-only report surface runs, then the tables still hold
        # the raw bytes: masking touches display copies, never stored rows.
        for args in (("entity-list",), ("entity-list", "--json"),
                     ("reclassify-entities", "--dry-run", "--json"),
                     ("reconcile-entity-aliases", "--dry-run", "--json")):
            out = self.run_store(*args)
            self.assert_no_raw(" ".join(args), out)
        conn = sqlite3.connect(self.store)
        try:
            names = {r[0] for r in conn.execute(
                "SELECT canonical_name FROM entity")}
            aliases = {r[0] for r in conn.execute(
                "SELECT alias_norm FROM entity_alias")}
        finally:
            conn.close()
        for raw in (RAW_NAME, RAW_ALIAS, RAW_SUFFIX, LEGAL_ORPHAN,
                    RAW_LONGSECRET):
            self.assertIn(raw, names | aliases,
                          f"stored row was rewritten by a display surface — "
                          "masking must be display-time only")


def survivor_name_ok(survivor: dict) -> bool:
    """Boolean gate payload: the surviving orphan's rendered fields carry
    the EXACT masked forms (plan-critic round 4 F3: positive structural
    gates, not raw-absence only). Deterministic for this fixture:
    name == model:secret:[REDACTED_SECRET], suffix == secret:[REDACTED_SECRET],
    owner_name is None (no owner exists)."""
    if survivor is None:
        return False
    if survivor.get("name") != MASKED_NOOWNER_ORPHAN:
        return False
    if survivor.get("suffix") != "secret:" + MASK:
        return False
    if survivor.get("owner_name") is not None:
        return False
    return True


class MergeCollisionRenderingTest(unittest.TestCase):
    """C1 collision leg (plan-critic F6): drive the REAL cmd_entity_merge
    collision join through a permissive schema fixture.

    entity_alias.alias_norm carries a GLOBAL UNIQUE constraint in every
    valid store, so a collision cannot be constructed through normal state.
    This fixture rebuilds the table WITHOUT that constraint (duplicates
    legally insertable there), drives the real dry-run arm (read-only —
    no writes happen without --confirm), and asserts the complete collision
    line renders the masked alias. The permissive schema is a rendering
    fixture, not a claim about valid stores.
    """

    def test_collision_join_masks_each_collision_alias(self):
        sys.path.insert(0, str(SCRIPTS_DIR))
        from storelib import entity as ent
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            "CREATE TABLE entity (id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
            "canonical_name TEXT NOT NULL, created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL); "
            "CREATE TABLE entity_alias (entity_id TEXT NOT NULL, "
            "alias_norm TEXT NOT NULL); "  # no UNIQUE — the fixture point
            "CREATE TABLE memory_entity (memory_id TEXT NOT NULL, "
            "entity_id TEXT NOT NULL, role TEXT NOT NULL DEFAULT "
            "'mentions', UNIQUE(memory_id, entity_id));")
        ts = FIXED_TS
        conn.executemany(
            "INSERT INTO entity VALUES (?,?,?,?,?)",
            [("11111111-0000-4000-8000-00000000000a", "other",
              "password:12345", ts, ts),
             ("11111111-0000-4000-8000-00000000000b", "other",
              "token:98765", ts, ts)])
        # The SAME alias on both entities — impossible under UNIQUE, the
        # collision branch's defensive reason for existing.
        conn.executemany(
            "INSERT INTO entity_alias VALUES (?,?)",
            [("11111111-0000-4000-8000-00000000000a", "token:98765"),
             ("11111111-0000-4000-8000-00000000000b", "token:98765")])
        conn.commit()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ent.cmd_entity_merge(
                conn, "11111111-0000-4000-8000-00000000000a",
                "11111111-0000-4000-8000-00000000000b", confirm=False)
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.ck_col(out)
        # Rendering-only: the fixture's rows are unchanged.
        n = conn.execute("SELECT COUNT(*) FROM entity_alias").fetchone()[0]
        self.assertEqual(n, 2)

    @staticmethod
    def ck_col(out: str):
        if "already on target" not in out:
            raise AssertionError(
                "collision join: collision summary line missing")
        if MASKED_ALIAS not in out:
            raise AssertionError(
                "collision join: masked alias missing from the collision "
                "line")
        if "token:98765" in out:
            raise AssertionError(
                "collision join: raw collision alias leaked through the "
                "join line")


class DisplayMaskHelperTest(unittest.TestCase):
    """C1 helper-level legs: the private display mask in storelib.entity.

    Frozen names: ``_mask_credential_shaped_name`` and
    ``_load_shared_secret_redactor`` (consulted per call, no module cache —
    these tests monkeypatch the seam).
    """

    def ck(self, condition: bool, what: str):
        if not condition:
            self.fail(f"masking contract violated: {what}")

    def _entity(self):
        sys.path.insert(0, str(SCRIPTS_DIR))
        from storelib import entity
        return entity

    def test_short_numeric_credentials_mask_via_local_shape(self):
        entity = self._entity()
        mask = entity._mask_credential_shaped_name
        for raw, head in (("password:12345", "password"),
                          ("token:98765", "token"),
                          ("auth:4242", "auth"),
                          ("api-key:7", "api-key"),
                          ("api_key:7", "api_key"),
                          ("apikey:7", "apikey"),
                          ("secret:42", "secret"),
                          ("passwd:123", "passwd"),
                          ("credential:0", "credential"),
                          ("password-:9", "password-")):
            self.ck(mask(raw) == head + ":" + MASK,
                    f"local-shape mask wrong for head {head!r}")

    def test_delimited_compound_heads_mask(self):
        # Plan-critic R3: secret words as REPEATABLE hyphen/underscore
        # delimited components of the head mask.
        entity = self._entity()
        mask = entity._mask_credential_shaped_name
        for raw, head in (("secret_key:42", "secret_key"),
                          ("password-value:9", "password-value"),
                          ("db-password:99", "db-password"),
                          ("db-admin-password:99", "db-admin-password"),
                          ("my-db-password:123", "my-db-password")):
            self.ck(mask(raw) == head + ":" + MASK,
                    f"delimited-compound mask wrong for head {head!r}")

    def test_mask_preserves_the_original_case_head(self):
        entity = self._entity()
        mask = entity._mask_credential_shaped_name
        self.ck(mask("Password:12345") == "Password:" + MASK,
                "original-case head not preserved (Password)")
        self.ck(mask("TOKEN:98765") == "TOKEN:" + MASK,
                "original-case head not preserved (TOKEN)")
        self.ck(mask("Auth-:4242") == "Auth-:" + MASK,
                "original-case head not preserved (Auth-)")
        self.ck(mask("DB_Password:9") == "DB_Password:" + MASK,
                "original-case head not preserved (DB_Password)")

    def test_shared_redactor_shapes_use_its_output(self):
        entity = self._entity()
        mask = entity._mask_credential_shaped_name
        self.ck(mask("password:supersecret99") == "password:" + MASK,
                "shared value-span output not used (password)")
        self.ck(mask("mytoken:12345678") == "mytoken:" + MASK,
                "unanchored compound key not delegated (mytoken)")
        self.ck(mask("pwd:supersecret99") == "pwd:" + MASK,
                "pwd head not delegated to the shared helper")
        self.ck(mask("private_key:supersecret99") ==
                "private_key:" + MASK,
                "private_key head not delegated to the shared helper")
        self.ck(mask("ghp_" + "A" * 36) == MASK,
                "whole-token shape not delegated (ghp_)")
        self.ck(mask("service:password:supersecret99") ==
                "service:password:" + MASK,
                "embedded span not delegated (kind-prefixed composite)")

    def test_legal_values_pass_through_unchanged(self):
        entity = self._entity()
        mask = entity._mask_credential_shaped_name
        for legal in ("localhost:8080", "server:8080", "postgres:5432",
                      "api-service", "server.log", "my-script.py",
                      "build-box", "host:build-box", "mytoken:123",
                      "token-service",
                      # Implementation-review round 1: 'pwd'/'private_key'
                      # are shared-redaction heads (their >=8-char values
                      # mask via the shared leg above) but are NOT in issue
                      # #455's verbatim local head list, so their
                      # short-digit forms are the intentional local
                      # boundary — pinned legal here so the gap is a
                      # documented decision, not an oversight.
                      "pwd:123", "private_key:9", ""):
            self.ck(mask(legal) == legal,
                    f"legal value wrongly masked (case {legal!r} withheld)")
        # Idempotent on its own output.
        self.ck(mask("password:" + MASK) == "password:" + MASK,
                "mask not idempotent on its own output")

    def test_degraded_loader_raises_and_read_only_cmds_refuse(self):
        """Plan-critic R2: a failed shared import ABORTS rendering."""
        entity = self._entity()
        orig = entity._load_shared_secret_redactor

        def _broken():
            raise ImportError("redaction unavailable (fixture)")

        entity._load_shared_secret_redactor = _broken
        self.addCleanup(setattr, entity, "_load_shared_secret_redactor",
                        orig)
        with self.assertRaises(ImportError):
            entity._mask_credential_shaped_name("password:12345")
        # In-process read-only refusal (representative arm): exit 2, ZERO
        # stdout, stderr note.
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE entity (id TEXT PRIMARY KEY, kind TEXT,"
                     " canonical_name TEXT, created_at TEXT, updated_at TEXT)")
        conn.execute("CREATE TABLE entity_alias (entity_id TEXT, "
                     "alias_norm TEXT)")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = entity.cmd_entity_list(conn)
        self.assertEqual(rc, 2)
        self.assertEqual(out.getvalue(), "",
                         "the refusal must not write ANY stdout")
        self.assertIn("redaction", err.getvalue().lower())

    def test_broken_loader_refuses_every_command_entry_point(self):
        """Plan-critic round-3 F4: table-driven degraded preflight for ALL
        four read-only/dry-run arms and ALL three mutating arms — exit 2,
        zero stdout, and (mutating arms) NO state change."""
        entity = self._entity()
        tmp = tempfile.mkdtemp(prefix="zmem-entmask-preflight-")
        self.addCleanup(shutil.rmtree, tmp, True)
        store = str(Path(tmp) / "store.sqlite")
        r = subprocess.run([PYTHON, "-X", "utf8", str(STORE_PY), "init"],
                           capture_output=True, env=_base_env(store),
                           timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        conn = sqlite3.connect(store)
        conn.row_factory = sqlite3.Row
        conn.executemany(
            "INSERT INTO entity VALUES (?,?,?,?,?)",
            [(ID_CRED, "other", RAW_NAME, FIXED_TS, FIXED_TS),
             (ID_TO, "other", RAW_ALIAS, FIXED_TS, FIXED_TS),
             (ID_OWNER, "service", RAW_SUFFIX, FIXED_TS, FIXED_TS)])
        conn.commit()

        def snapshot():
            rows = conn.execute(
                "SELECT id, kind, canonical_name FROM entity "
                "ORDER BY id").fetchall()
            return [(r_["id"], r_["kind"], r_["canonical_name"])
                    for r_ in rows]

        before = snapshot()
        orig = entity._load_shared_secret_redactor

        def _broken():
            raise ImportError("redaction unavailable (fixture)")

        entity._load_shared_secret_redactor = _broken
        self.addCleanup(setattr, entity, "_load_shared_secret_redactor",
                        orig)
        cases = [
            ("entity-list", lambda: entity.cmd_entity_list(conn)),
            ("entity-merge dry-run",
             lambda: entity.cmd_entity_merge(conn, ID_CRED, ID_TO,
                                             confirm=False)),
            ("reclassify dry-run",
             lambda: entity.cmd_reclassify_entities(conn, apply_mode=False)),
            ("reconcile dry-run",
             lambda: entity.cmd_reconcile_entity_aliases(
                 conn, apply_mode=False)),
            ("reclassify apply",
             lambda: entity.cmd_reclassify_entities(conn, apply_mode=True)),
            ("reconcile apply",
             lambda: entity.cmd_reconcile_entity_aliases(
                 conn, apply_mode=True)),
            ("entity-merge confirm",
             lambda: entity.cmd_entity_merge(conn, ID_CRED, ID_TO,
                                             confirm=True)),
        ]
        for label, call in cases:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                rc = call()
            self.assertEqual(rc, 2, f"{label}: degraded preflight must "
                             "refuse with exit 2")
            self.assertEqual(out.getvalue(), "",
                             f"{label}: the refusal must write zero stdout")
            self.assertIn("redaction", err.getvalue().lower(),
                          f"{label}: stderr refusal note missing")
        self.assertEqual(snapshot(), before,
                         "no mutating arm may touch state under a broken "
                         "loader")
        conn.close()

    def test_loader_uses_the_two_step_bootstrap_fallback(self):
        """The loader retries once with the scripts dir on sys.path after a
        plain-import failure (the write.py:293-309 precedent)."""
        entity = self._entity()
        saved_path = list(sys.path)
        saved_mod = sys.modules.pop("redaction", None)
        try:
            # Make the plain import fail: no sys.path entry resolves
            # redaction.py...
            sys.path.clear()
            loader = entity._load_shared_secret_redactor()
            self.assertTrue(callable(loader))
            # ...and the fallback re-added the scripts dir to make it work.
            self.assertIn(str(SCRIPTS_DIR), sys.path)
        finally:
            sys.path[:] = saved_path
            if saved_mod is not None:
                sys.modules["redaction"] = saved_mod
            else:
                sys.modules.pop("redaction", None)

    def test_apply_guard_treats_mask_failure_as_render_failure(self):
        """Plan-critic F9 + implementation-review round 1: inside BOTH apply
        arms, a masking exception stays inside _guard_committed_rendering —
        committed work reports success from committed state (exit 0, stderr
        note), never failure after commit. The reconcile arm is driven too
        (review mutant: removing only its guard left the suite green).

        The guard dup2's devnull onto PROCESS fd 1; redirect_stdout cannot
        restore that, so fd 1 is saved and restored unconditionally.
        """
        entity = self._entity()
        tmp = tempfile.mkdtemp(prefix="zmem-entmask-guard-")
        self.addCleanup(shutil.rmtree, tmp, True)
        store = str(Path(tmp) / "store.sqlite")
        r = subprocess.run([PYTHON, "-X", "utf8", str(STORE_PY), "init"],
                           capture_output=True, env=_base_env(store),
                           timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        conn = sqlite3.connect(store)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO entity VALUES (?,?,?,?,?)",
            (ID_CRED, "other", RAW_NAME, FIXED_TS, FIXED_TS))
        conn.commit()
        orig_mask = entity._mask_credential_shaped_name

        def _boom(*a, **k):
            raise RuntimeError("mask explosion (fixture)")

        entity._mask_credential_shaped_name = _boom
        self.addCleanup(setattr, entity, "_mask_credential_shaped_name",
                        orig_mask)
        saved_fd = os.dup(1)
        try:
            # Arm 1: reclassify apply — the kind change must commit.
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                rc = entity.cmd_reclassify_entities(conn, apply_mode=True)
            self.assertEqual(rc, 0, "committed reclassify apply must report "
                             "success from committed state on a render-only "
                             "failure")
            self.assertIn("could not be printed", err.getvalue())
            kind = conn.execute("SELECT kind FROM entity WHERE id=?",
                                (ID_CRED,)).fetchone()[0]
            self.assertEqual(kind, "service")
            # Arm 2: reconcile apply — a fresh orphan row must merge (commit)
            # and still report success when the render explodes.
            conn.execute(
                "INSERT INTO entity VALUES (?,?,?,?,?)",
                (ID_ORPHAN, "other", LEGAL_ORPHAN, FIXED_TS, FIXED_TS))
            conn.execute(
                "INSERT INTO entity VALUES (?,?,?,?,?)",
                (ID_OWNER, "service", RAW_SUFFIX, FIXED_TS, FIXED_TS))
            conn.execute(
                "INSERT INTO entity_alias VALUES (?,?)",
                (ID_OWNER, RAW_SUFFIX))
            conn.commit()
            out2, err2 = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out2), \
                    contextlib.redirect_stderr(err2):
                rc2 = entity.cmd_reconcile_entity_aliases(
                    conn, apply_mode=True)
            self.assertEqual(rc2, 0, "committed reconcile apply must report "
                             "success from committed state on a render-only "
                             "failure")
            self.assertIn("could not be printed", err2.getvalue())
            still = conn.execute("SELECT COUNT(*) FROM entity WHERE id=?",
                                 (ID_ORPHAN,)).fetchone()[0]
            self.assertEqual(still, 0, "the orphan row was merged away — "
                             "the apply really committed")
        finally:
            os.dup2(saved_fd, 1)
            os.close(saved_fd)
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
