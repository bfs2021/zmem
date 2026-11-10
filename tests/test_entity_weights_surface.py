"""Issue #367 (O-005 + explain/warning): entity-kind weight surface pins.

- DualMatchedKindWeightTest (C9/AC9): a memory matched by BOTH an `other`
  alias and a typed alias keeps weight 1.0 — the reduction is the MAX over
  QUERY-MATCHED kinds (a max->min mutant flips the assertion below).
- ExplainWeightsOverrideValuesTest (C10/AC10): `recall --explain --json`
  reports the env override's VALUES in explain.entity_kind_weights, not just
  key presence.
- InvalidWeightsWarningBoundedTest (C12/AC12): the invalid-weights warning
  truncates the echoed key — one bounded stderr line even for a 5000-char key.

Run: python tests/test_entity_weights_surface.py  (no pytest; house convention)
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


class _WeightsStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-weights-")
        self.store = str(Path(self.tmp) / "store.sqlite")
        self.env = _base_env(self.store)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_store(self, *args, env=None, check=True, timeout=120):
        r = subprocess.run(
            [PYTHON, str(STORE_PY), *args],
            capture_output=True, text=True, env=env or self.env, timeout=timeout,
        )
        if check and r.returncode != 0:
            self.fail(f"store.py {' '.join(args)} exited {r.returncode}\n"
                      f"stdout: {r.stdout}\nstderr: {r.stderr}")
        return r


class DualMatchedKindWeightTest(_WeightsStore):
    """C9 (AC9): weight is the MAX over the kinds the QUERY matched.

    A carries `entity:Brett` (other) and `entity:api.example.com` (host); the
    query matches BOTH aliases, so A's matched kinds are {other, host} and its
    entity-lane weight must be 1.0 (max), STABLE under an `other` damping
    override. A max->min (or linked-kinds) reduction would damp A to the other
    weight and flip A below the lexical row exactly when the override lands.
    B is the strong lexical row with no matched alias (the #176 pattern).
    """

    def test_dual_matched_memory_keeps_full_weight_under_damping(self):
        for content, tags in (
            ("unrelated prose about pottery and glaze",
             "entity:Brett entity:api.example.com"),
            ("brett api.example.com brett api.example.com repeated", ""),
        ):
            self.run_store(
                "add", "--namespace", "project:ew9", "--type", "lesson",
                "--content", content, "--tags", tags, "--signal", "user")

        os.environ["ZMEM_STORE"] = self.store
        os.environ["ZMEM_MODELS_DIR"] = str(Path(self.store).parent / "no-models")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        sys.path.insert(0, str(SCRIPTS_DIR))
        self.addCleanup(sys.path.remove, str(SCRIPTS_DIR))
        for mod in [m for m in list(sys.modules)
                    if m == "storelib" or m.startswith("storelib.")]:
            del sys.modules[mod]
        import storelib.recall as recall_mod
        from storelib.recall import _recall_one_tier

        # Exact-weight seam: capture the production entity_weight_by_memory
        # map at the _rrf_fuse call site (recall.py:1547-1550 pass it through).
        captured = {}
        real_fuse = recall_mod._rrf_fuse

        def fuse_spy(*args, **kwargs):
            weights = kwargs.get("entity_weight_by_memory")
            if weights:
                captured.update(weights)
            return real_fuse(*args, **kwargs)

        recall_mod._rrf_fuse = fuse_spy
        self.addCleanup(setattr, recall_mod, "_rrf_fuse", real_fuse)

        conn = sqlite3.connect(f"file:{self.store}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            ids = {r["content"][:16]: r["id"] for r in conn.execute(
                "SELECT id, content FROM memory WHERE namespace='project:ew9'")}
            entity_row = "unrelated prose "  # content[:16] incl. space
            fts_row = "brett api.example"[:16]

            def ranks(weight_env):
                if weight_env is None:
                    os.environ.pop("ZMEM_ENTITY_KIND_WEIGHTS", None)
                else:
                    os.environ["ZMEM_ENTITY_KIND_WEIGHTS"] = weight_env
                scored = _recall_one_tier(
                    conn, query="Brett api.example.com",
                    ns_list=["project:ew9"], limit=5,
                    min_confidence=None, hybrid=False, now_epoch=0,
                    collect_lanes=True,
                )
                lanes = {row["id"]: row["_lanes"] for _s, row in scored}
                return (lanes[ids[entity_row]]["rrf_rank"],
                        lanes[ids[fts_row]]["rrf_rank"])

            # Baseline: the dual-matched row fuses above the lexical row.
            self.assertEqual(ranks(None), (1, 2),
                             "default weights: dual-matched row must lead")
            # MAX over matched kinds: damping `other` cannot touch a memory
            # that ALSO matched the typed host alias — rank stable at (1, 2).
            self.assertEqual(
                ranks(json.dumps({"other": 0.001})), (1, 2),
                "a memory matched through a typed alias keeps weight 1.0 "
                "(max over query-matched kinds; min/linked-kinds damps it)")
            # The override DOES reach the lane: damping BOTH matched kinds
            # must be able to drop the entity slice below the lexical row.
            self.assertEqual(
                ranks(json.dumps({"other": 0.001, "host": 0.001})), (2, 1),
                "damping every matched kind must flip the fused order")
            # EXACT contract (AC9): under `other` damping, the dual-matched
            # memory's production weight is exactly 1.0 (max over the
            # QUERY-matched kinds {other, host}), not merely rank-stable.
            captured.clear()
            ranks(json.dumps({"other": 0.001}))
            ids_exact = {r["id"]: r["content"][:16] for r in conn.execute(
                "SELECT id, content FROM memory WHERE namespace='project:ew9'")}
            dual_id = [mid for mid, c in ids_exact.items()
                       if c == "unrelated prose "][0]
            self.assertIn(dual_id, captured,
                          "the dual-matched memory must carry a lane weight")
            self.assertEqual(
                captured[dual_id], 1.0,
                "weight must be EXACTLY 1.0 under other-damping "
                "(max over query-matched kinds)")
        finally:
            conn.close()
            os.environ.pop("ZMEM_ENTITY_KIND_WEIGHTS", None)


class ExplainWeightsOverrideValuesTest(_WeightsStore):
    """C10 (AC10): explain carries the override's VALUES."""

    def test_explain_reports_override_values(self):
        self.run_store(
            "add", "--namespace", "project:ew10", "--type", "lesson",
            "--content", "explain override seed row", "--tags", "zzexplain",
            "--signal", "user")
        env = dict(self.env)
        env["ZMEM_ENTITY_KIND_WEIGHTS"] = json.dumps(
            {"other": 0.25, "host": 1.0})
        r = self.run_store(
            "recall", "--query", "explain override", "--namespace",
            "project:ew10", "--json", "--limit", "1", "--no-hybrid",
            "--no-mmr", "--explain", env=env)
        doc = json.loads(r.stdout)
        weights = doc["explain"]["entity_kind_weights"]
        self.assertEqual(weights["other"], 0.25,
                         "the override's VALUE must be reported, not presence")
        self.assertEqual(weights["host"], 1.0)


class InvalidWeightsWarningBoundedTest(_WeightsStore):
    """C12 (AC12): the warning truncates the echoed key."""

    def test_oversized_unknown_key_yields_one_bounded_line(self):
        self.run_store(
            "add", "--namespace", "project:ew12", "--type", "lesson",
            "--content", "warning bound seed row", "--tags", "zzwarn",
            "--signal", "user")
        env = dict(self.env)
        env["ZMEM_ENTITY_KIND_WEIGHTS"] = json.dumps(
            {"K" * 5000: 1.0})
        r = self.run_store(
            "recall", "--query", "warning bound", "--namespace",
            "project:ew12", "--json", "--limit", "1", "--no-hybrid",
            "--no-mmr", "--explain", env=env)
        lines = [ln for ln in r.stderr.splitlines()
                 if "entity-kind weights" in ln]
        self.assertEqual(len(lines), 1,
                         "exactly one warning line per process/raw value")
        self.assertLess(
            max(len(ln) for ln in r.stderr.splitlines()), 300,
            "the echoed key must be truncated (base echoes 5000+ chars)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
