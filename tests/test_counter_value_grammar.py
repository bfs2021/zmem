"""Counter-value grammar regression tests (PR #444 review round, PRR-001).

The review found that ``str.isdigit()`` — the original legacy predicate in
``schema_meta.classify_convention_count`` — admits non-decimal digit
characters (``'²'``) and over-CPython-limit digit strings whose ``int()``
raises, with three consequences at the then-head:

1. the sweep's meta prune DELETED such rows as "legacy" (violating the
   malformed-retention law this same PR ships),
2. the hermes verify hook crashed with an uncaught ValueError/RecursionError
   (its main() catches only sqlite3.Error),
3. the storelib increment silently lost the event (rollback + ``{}``).

These tests pin the fixed grammar: an explicit ASCII-decimal predicate with
an int()-parseability gate, ``RecursionError`` caught as malformed, twin
parity between schema_meta and the hook's local copy, and the pruner
RETAINING digit-shaped malformed rows.

Run: python -X utf8 tests/test_counter_value_grammar.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"

# Pin the env BEFORE any storelib/schema_meta import (issue #244 isolation
# law) — these tests import schema_meta directly (pure functions, no store)
# and drive the pruner through a scratch store in-process.
_TMP = tempfile.mkdtemp(prefix="zmem-cvg-")
os.environ.update({
    "ZMEM_STORE": os.path.join(_TMP, "store.sqlite"),
    "ZMEM_DATA": _TMP,
    "ZMEM_MODEL_AUTODOWNLOAD": "0",
    "PYTHONIOENCODING": "utf-8",
})
for _k in ("ZMEM_HOME", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
           "ZCODE_PROJECT_DIR", "CLAUDE_PROJECT_DIR",
           "ZMEM_SESSION", "ZMEM_SESSION_ID", "ZMEM_BACKUP_DIR"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(SCRIPTS))

from schema_meta import classify_convention_count, parse_convention_count  # noqa: E402

SUPERSCRIPT = "²"
OVER_LIMIT = "1" * 5000
DEEP_NESTED = "[" * 3000
CANONICAL_JSON = json.dumps({"count": 3, "last_seen": "2026-01-01T00:00:00Z"})


class ClassifyGrammarTest(unittest.TestCase):
    """classify must never call a value int()-unparseable "legacy"."""

    def test_superscript_digit_is_malformed(self):
        self.assertEqual(classify_convention_count(SUPERSCRIPT), "malformed")
        self.assertEqual(parse_convention_count(SUPERSCRIPT), 0)

    def test_over_limit_digit_string_is_malformed(self):
        # ASCII digits — but int() raises over CPython's conversion limit.
        # classify must not label it "legacy" (the pruner deletes non-current
        # legacy rows; the retention law protects malformed rows).
        self.assertEqual(classify_convention_count(OVER_LIMIT), "malformed")
        self.assertEqual(parse_convention_count(OVER_LIMIT), 0)

    def test_deeply_nested_json_is_malformed(self):
        # json.loads raises RecursionError, which the original
        # except (ValueError, TypeError) did not catch.
        self.assertEqual(classify_convention_count(DEEP_NESTED), "malformed")
        self.assertEqual(parse_convention_count(DEEP_NESTED), 0)

    def test_real_shapes_unchanged(self):
        self.assertEqual(classify_convention_count("3"), "legacy")
        self.assertEqual(classify_convention_count("007"), "legacy")
        self.assertEqual(classify_convention_count("0"), "legacy")
        self.assertEqual(parse_convention_count("007"), 7)
        self.assertEqual(classify_convention_count(CANONICAL_JSON), "json")
        self.assertEqual(parse_convention_count(CANONICAL_JSON), 3)
        self.assertEqual(classify_convention_count("not-json{"), "malformed")


class PrunerRetainsDigitShapedMalformedTest(unittest.TestCase):
    """The sweep's meta prune must RETAIN a digit-shaped malformed row
    (counted unparseable), never delete it as legacy — the executed
    data-loss shape the review proved at the pre-fix head."""

    def test_sweep_retains_superscript_counter_row(self):
        from storelib.backup import prune_convention_meta
        import time
        d = tempfile.mkdtemp(prefix="zmem-cvg-prune-")
        store = os.path.join(d, "store.sqlite")
        conn = sqlite3.connect(store)
        conn.executescript(
            "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO meta VALUES ('schema_version', '19')")
        conn.execute(
            "INSERT INTO meta VALUES (?, ?)",
            ("hermes_convention_count_prune-sup", SUPERSCRIPT))
        conn.execute(
            "INSERT INTO meta VALUES (?, ?)",
            ("hermes_convention_count_prune-5000", OVER_LIMIT))
        conn.commit()
        conn.close()

        now = time.time()
        result = prune_convention_meta(
            store, cutoff_epoch=now - 7 * 86400, current_session="",
            dry_run=False, now_epoch=now)
        self.assertIsNotNone(result)
        removed, retained, unparseable = result
        self.assertEqual(unparseable, 2,
                         f"both digit-shaped malformed rows must be counted "
                         f"unparseable; got {result!r}")
        self.assertEqual(removed, 0, "no row may be deleted")
        conn = sqlite3.connect(store)
        try:
            survivors = dict(conn.execute(
                "SELECT key, value FROM meta WHERE key LIKE "
                "'hermes_convention_count%'"))
        finally:
            conn.close()
        self.assertEqual(survivors.get("hermes_convention_count_prune-sup"),
                         SUPERSCRIPT, "the row must be retained byte-identical")
        self.assertEqual(survivors.get("hermes_convention_count_prune-5000"),
                         OVER_LIMIT)


class VerifyHookTwinParityTest(unittest.TestCase):
    """The hook's local twin must stay byte-equivalent to schema_meta over
    the grammar matrix (loadstep: the hook copy loads standalone)."""

    def test_twin_parity_over_grammar_matrix(self):
        import importlib.util
        hook = REPO_ROOT / "hermes-plugin" / "hooks" / "zmem-hermes-verify.py"
        spec = importlib.util.spec_from_file_location("cvg_hook_twin", hook)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        matrix = [SUPERSCRIPT, OVER_LIMIT, DEEP_NESTED, "3", "007", "",
                  "not-json{", CANONICAL_JSON,
                  json.dumps({"count": True, "last_seen": "2026-01-01T00:00:00Z"}),
                  json.dumps({"count": 2}),
                  json.dumps({"count": -1, "last_seen": "x"})]
        for value in matrix:
            with self.subTest(value=value[:40]):
                self.assertEqual(mod._parse_convention_count(value),
                                 parse_convention_count(value),
                                 f"twin diverged on {value!r}")

    def test_hook_parse_never_raises(self):
        import importlib.util
        hook = REPO_ROOT / "hermes-plugin" / "hooks" / "zmem-hermes-verify.py"
        spec = importlib.util.spec_from_file_location("cvg_hook_nr", hook)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for value in (SUPERSCRIPT, OVER_LIMIT, DEEP_NESTED):
            self.assertEqual(mod._parse_convention_count(value), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
