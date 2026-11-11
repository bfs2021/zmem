"""Issue #455: legal label:port / service-noun names are NOT over-masked.

The masking side (credential-shaped values hidden at display time) is owned
by tests/test_entity_display_redaction.py. This file pins the FALSE side of
the predicate: a legal name must render BYTE-IDENTICALLY on the entity
report surfaces, with no [REDACTED_SECRET] marker anywhere in the output.

Legal set under test (issue #455 contract): localhost:8080, server:8080,
postgres:5432, api-service, plus the round-2 boundary pins mytoken:123
(undelimited secret-word head, short payload — deliberately legal) and
token-service (hyphenated non-head use). (server.log, my-script.py,
build-box and the kind-prefixed host:build-box shape are pinned at the
helper level in the masking suite's DisplayMaskHelperTest.)

All six names classify to 'service' at base and head (label:port shapes,
the service noun, and the two boundary pins), so each one appears in the
reclassify proposal — the reclassify legs below are non-vacuous.

Run: python tests/test_entity_display_redaction_legal_names.py  (no pytest;
house convention — unittest.TestCase style keeps pytest node selection
working too).
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

MASK = "[REDACTED_SECRET]"
# Round-2 regeneration: adds the deliberate boundary pins (undelimited
# secret-word head with short payload, and a hyphenated non-head use).
LEGAL_NAMES = ("localhost:8080", "server:8080", "postgres:5432",
               "api-service", "mytoken:123", "token-service")
FIXED_TS = "2026-11-11T00:00:00Z"


def _base_env(store_path: str) -> dict:
    """Same hardened env as tests/test_entity_display_redaction.py: pinned
    ZMEM_STORE/ZMEM_DATA scratch, stripped CLAUDE_PLUGIN_*/ZCODE_PLUGIN_*
    leakage, UTF-8-forced children."""
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


class LegalEntityNameTest(unittest.TestCase):
    """C2: legal names render unmasked across entity-list and reclassify."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-entlegal-")
        self.store = str(Path(self.tmp) / "store.sqlite")
        self.env = _base_env(self.store)
        self.addCleanup(shutil.rmtree, self.tmp, True)
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
                [(f"22222222-0000-4000-8000-{i:012d}", "other", name,
                  FIXED_TS, FIXED_TS)
                 for i, name in enumerate(LEGAL_NAMES, start=1)])
            conn.commit()
        finally:
            conn.close()

    def run_store(self, *args):
        r = subprocess.run(
            [PYTHON, "-X", "utf8", str(STORE_PY), *args],
            capture_output=True, env=self.env, timeout=120,
        )
        if r.returncode != 0:
            self.fail(f"store.py {' '.join(args)} exited {r.returncode}; "
                      f"stderr tail: "
                      f"{r.stderr.decode('utf-8', 'replace')[-400:]!r}")
        return r.stdout.decode("utf-8")

    def test_entity_list_human_keeps_legal_names(self):
        out = self.run_store("entity-list")
        for name in LEGAL_NAMES:
            self.assertIn(name, out)
        self.assertNotIn(MASK, out,
                         "a legal name must never grow a redaction marker")

    def test_entity_list_json_keeps_legal_names(self):
        out = self.run_store("entity-list", "--json")
        self.assertNotIn(MASK, out)
        names = {e["name"] for e in json.loads(out)}
        self.assertEqual(names, set(LEGAL_NAMES))

    def test_reclassify_dry_run_human_keeps_legal_names(self):
        out = self.run_store("reclassify-entities", "--dry-run")
        self.assertNotIn(MASK, out)
        # All six are other -> service proposals: each raw name must be on
        # its own change line.
        for name in LEGAL_NAMES:
            self.assertIn(f"'{name}' other -> service", out)
        self.assertIn("scanned=6 changed=6", out)

    def test_reclassify_dry_run_json_keeps_legal_names(self):
        out = self.run_store("reclassify-entities", "--dry-run", "--json")
        self.assertNotIn(MASK, out)
        parsed = json.loads(out)
        self.assertEqual(parsed["changed"], 6)
        self.assertEqual({c["name"] for c in parsed["changes"]},
                         set(LEGAL_NAMES))


if __name__ == "__main__":
    unittest.main(verbosity=2)
