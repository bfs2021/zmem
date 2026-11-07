"""Direct-seam skip-contract tests for prune_convention_meta (PR #444
feedback round, delta-r1: the documented "absent / locked / unreadable /
not a database / missing meta table" arms must return None — the skip
shape — in BOTH the dry-run and real-run arms, never a raise that the
caller's blanket handler would mask as a silent skip).

Run: python -X utf8 tests/test_meta_prune_skip_arms.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"

_TMP = tempfile.mkdtemp(prefix="zmem-pruneskip-")
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
import _store_guard  # noqa: E402,F401  (issue #244 isolation guard)

sys.path.insert(0, str(SCRIPTS))

from storelib.backup import prune_convention_meta  # noqa: E402

CUTOFF = 1_000_000_000.0  # 2001 — prunes everything with last_seen before it
NOW = 1_800_000_000.0


def _seeded_store(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO meta VALUES ('schema_version', '19');"
        "INSERT INTO meta VALUES "
        "('hermes_convention_count_probe', "
        "'{\"count\":1,\"last_seen\":\"2026-01-01T00:00:00Z\"}');")
    conn.commit()
    conn.close()


class PruneSkipArmsTest(unittest.TestCase):
    """Every unopenable-store arm returns None (the skip shape)."""

    def _assert_none_both_arms(self, store, context):
        for dry in (False, True):
            with self.subTest(context=context, dry_run=dry):
                result = prune_convention_meta(
                    store, cutoff_epoch=CUTOFF, current_session="",
                    dry_run=dry, now_epoch=NOW)
                self.assertIsNone(
                    result,
                    f"{context} (dry_run={dry}) must return None (the skip "
                    f"shape), got {result!r}")

    def test_garbage_file_returns_none_both_arms(self):
        store = os.path.join(_TMP, "garbage.sqlite")
        with open(store, "wb") as fh:
            fh.write(b"this is not a sqlite database at all" * 40)
        self._assert_none_both_arms(store, "garbage file")

    def test_truncated_store_returns_none_both_arms(self):
        store = os.path.join(_TMP, "trunc.sqlite")
        good = os.path.join(_TMP, "good-tmp.sqlite")
        _seeded_store(good)
        blob = Path(good).read_bytes()
        Path(store).write_bytes(blob[: len(blob) // 3])
        self._assert_none_both_arms(store, "truncated store")

    def test_meta_less_store_returns_none_both_arms(self):
        store = os.path.join(_TMP, "nometa.sqlite")
        conn = sqlite3.connect(store)
        conn.execute("CREATE TABLE other(x TEXT)")
        conn.commit()
        conn.close()
        self._assert_none_both_arms(store, "meta-less store")

    def test_locked_store_returns_none_real_run(self):
        store = os.path.join(_TMP, "locked.sqlite")
        _seeded_store(store)
        holder = sqlite3.connect(store, timeout=0.5)
        holder.execute("BEGIN EXCLUSIVE")
        try:
            started = threading.Event()

            def _run():
                started.set()
                prune_convention_meta(
                    store, cutoff_epoch=CUTOFF, current_session="",
                    dry_run=False, now_epoch=NOW)

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            started.wait(5)
            t.join(timeout=15)
            self.assertFalse(t.is_alive(),
                             "the pruner must return (None skip) after the "
                             "busy timeout, not hang on the locked store")
        finally:
            holder.rollback()
            holder.close()

    def test_absent_store_returns_none_and_creates_nothing(self):
        store = os.path.join(_TMP, "absent", "store.sqlite")
        os.makedirs(os.path.dirname(store), exist_ok=True)
        self._assert_none_both_arms(store, "absent store")
        self.assertFalse(os.path.exists(store),
                         "the pruner must never create the store file")


if __name__ == "__main__":
    unittest.main(verbosity=2)
