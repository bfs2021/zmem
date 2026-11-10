"""Issue #368: entity-kind fixture provenance pins.

- OperatorSnapshotFixtureProvenanceTest (C15/AC15): operator-snapshot.json's
  fixture block is cross-checked against reclassify-expected.json — the
  "committed proof" wording in SKILL.md becomes test-pinned.
- ExpectedRegenerationProvenanceTest (C16/AC16): reclassify-expected.json is
  regenerable OUTSIDE the test — the generator's --emit-store mode builds the
  seeded fixture store and the documented dry-run command byte-equals the
  committed oracle (no dependence on ReclassifyTest._seed).
- RunnerHeadsHermesPinTest (C17/AC17): the hermes plugin's hand-copied
  _PROVIDER_RUNNER_HEADS is pinned equal to ops_tokens.RUNNER_HEADS.

Run: python tests/test_entity_fixture_provenance.py  (no pytest; house convention)
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "entity"
GENERATOR = FIXTURES / "make_reclassify_fixture.py"
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


class OperatorSnapshotFixtureProvenanceTest(unittest.TestCase):
    """C15 (AC15): the snapshot and the oracle describe one fixture run."""

    def test_fixture_block_matches_expected_oracle(self):
        snap = json.loads(
            (FIXTURES / "operator-snapshot.json").read_text(encoding="utf-8"))
        expected = json.loads(
            (FIXTURES / "reclassify-expected.json").read_text(encoding="utf-8"))
        reclassify = json.loads(
            (FIXTURES / "reclassify.json").read_text(encoding="utf-8"))
        fx = snap["fixture"]
        self.assertEqual(fx["before_other_link_share"],
                         expected["before_other_link_share"])
        self.assertEqual(fx["after_other_link_share"],
                         expected["after_other_link_share"])
        self.assertEqual(fx["changed"], expected["changed"])
        self.assertEqual(fx["changed"], len(expected["changes"]))
        self.assertEqual(fx["entity_rows"], expected["scanned"])
        distinct_mids = {row["memory_id"] for row in reclassify["memory_entity"]}
        self.assertEqual(fx["memory_entity_rows"], len(distinct_mids))
        self.assertEqual(fx["memory_entity_rows"],
                         len(reclassify["memory_entity"]))
        # The motivating operator measurement stays what SKILL.md quotes.
        self.assertIn("other_link_share", snap)
        self.assertAlmostEqual(snap["other_link_share"],
                               24826 / 26511, places=12)


class ExpectedRegenerationProvenanceTest(unittest.TestCase):
    """C16 (AC16): the oracle is regenerable from the generator alone."""

    def test_emit_store_documented_invocation_is_self_sufficient(self):
        """Implementation-review finding: SKILL.md documents
        `make_reclassify_fixture.py --emit-store <dir>` WITHOUT an env prefix;
        the generator must force the target store itself so the documented
        command never resolves (or initializes) the DEFAULT operator store."""
        import tempfile
        tmp = tempfile.mkdtemp(prefix="zmem-emitstore-")
        self.addCleanup(shutil.rmtree, tmp, True)
        target = str(Path(tmp) / "nested" / "store.sqlite")
        home = Path(tmp) / "home"
        home.mkdir()
        env = dict(os.environ)
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        env.pop("ZMEM_STORE", None)
        env.pop("ZMEM_DATA", None)
        gen = subprocess.run(
            [sys.executable, str(GENERATOR), "--emit-store", target],
            capture_output=True, text=True, env=env, timeout=120,
        )
        self.assertEqual(
            gen.returncode, 0,
            f"the documented invocation must succeed standalone: {gen.stderr!r}")
        self.assertTrue(Path(target).is_file(),
                        "the store must land at the argument path")
        conn = sqlite3.connect(target)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM entity").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 5, "the seeded fixture entities must be present")
        default_store = home / ".zmem" / "store.sqlite"
        self.assertFalse(
            default_store.exists(),
            "the default store area must never be initialized by --emit-store")

    def test_emit_store_and_documented_dry_run_reproduce_oracle(self):
        tmp = tempfile.mkdtemp(prefix="zmem-provenance-")
        self.addCleanup(shutil.rmtree, tmp, True)
        store = str(Path(tmp) / "store.sqlite")
        env = _base_env(store)
        gen = subprocess.run(
            [PYTHON, str(GENERATOR), "--emit-store", store],
            capture_output=True, text=True, env=env, timeout=120,
        )
        self.assertEqual(
            gen.returncode, 0,
            f"generator --emit-store must build the seeded fixture store: "
            f"{gen.stderr!r}")
        r = subprocess.run(
            [PYTHON, str(STORE_PY), "reclassify-entities", "--dry-run",
             "--json"],
            capture_output=True, text=True, env=env, timeout=120,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        committed = (FIXTURES / "reclassify-expected.json").read_bytes()
        self.assertEqual(
            r.stdout.encode("utf-8"), committed,
            "the documented regeneration command must byte-equal the oracle")


class RunnerHeadsHermesPinTest(unittest.TestCase):
    """C17 (AC17): hermes _PROVIDER_RUNNER_HEADS == ops_tokens.RUNNER_HEADS."""

    def test_provider_runner_heads_equal_ops_tokens(self):
        sys.path.insert(0, str(SCRIPTS_DIR))
        sys.path.insert(0, str(SCRIPTS_DIR / "storelib"))
        try:
            import ops_tokens  # noqa: F401  (storelib package, p03 pattern)
        finally:
            sys.path.remove(str(SCRIPTS_DIR))
            sys.path.remove(str(SCRIPTS_DIR / "storelib"))
        agent = types.ModuleType("agent")
        mp = types.ModuleType("agent.memory_provider")

        class MemoryProvider:
            pass

        mp.MemoryProvider = MemoryProvider
        agent.memory_provider = mp
        sys.modules.setdefault("agent", agent)
        sys.modules.setdefault("agent.memory_provider", mp)
        spec = importlib.util.spec_from_file_location(
            "zmem_runner_heads_pin", REPO_ROOT / "hermes-plugin" / "__init__.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["zmem_runner_heads_pin"] = module
        spec.loader.exec_module(module)
        self.assertEqual(
            frozenset(ops_tokens.RUNNER_HEADS),
            frozenset(module._PROVIDER_RUNNER_HEADS),
            "the hermes local copy must equal ops_tokens.RUNNER_HEADS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
