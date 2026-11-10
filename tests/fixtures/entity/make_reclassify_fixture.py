#!/usr/bin/env python
"""Deterministic generator for the issue #176 entity fixtures.

Writes, from the FIXED values in issue #176 Design step 7 (no randomness, no
store access, no network):

  tests/fixtures/entity/classify.json          30 classifier strings + kinds
  tests/fixtures/entity/reclassify.json        fixed other-rows seed store
  tests/fixtures/entity/operator-snapshot.json fixed operator-metrics block

It never writes tests/fixtures/entity/reclassify-expected.json — that file
regenerates in two steps, with no test involved (issue #368):

  python tests/fixtures/entity/make_reclassify_fixture.py --emit-store <dir>
  ZMEM_STORE=<dir>/store.sqlite python skills/memory/scripts/store.py \
      reclassify-entities --dry-run --json \
      > tests/fixtures/entity/reclassify-expected.json

Running this generator twice produces byte-identical outputs: insertion order
is preserved (json.dumps runs with sort_keys=False), 2-space indent, LF
newlines, UTF-8; deterministic because the dict literals below are fixed.

Usage: python tests/fixtures/entity/make_reclassify_fixture.py [--emit-store DIR]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent


def emit_store(path, *, extra_tool: bool = False, env=None) -> None:
    """Build the seeded fixture store at `path` — the EXACT seeding
    ReclassifyTest drives (issue #368: one source of truth for the oracle).

    Creates the store through the real `store.py init` (env must pin
    ZMEM_STORE to `path`, or be passed explicitly), then inserts the FIXED
    memory/entity/alias/link rows from RECLASSIFY verbatim.
    `extra_tool=True` additionally inserts the non-`other` tool row the
    non-other-rows-untouched pin depends on (never in the oracle fixture).
    """
    import os
    import subprocess

    store_py = (FIXTURE_DIR.parent.parent.parent / "skills" / "memory"
                / "scripts" / "store.py")
    init = subprocess.run(
        [sys.executable, str(store_py), "init"],
        env=env if env is not None else os.environ.copy(),
        capture_output=True, text=True, timeout=120,
    )
    if init.returncode != 0:
        raise RuntimeError(
            f"store.py init failed for {path}: {init.stderr.strip()}")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        mem_ids = sorted({m["memory_id"] for m in RECLASSIFY["memory_entity"]})
        conn.executemany(
            "INSERT INTO memory(id, namespace, type, content, tags, "
            "ingestion_ts) VALUES (?,?,?,?,?,?)",
            [(mid, "fixture:entity-176", "fact", f"fixture row {i + 1}", "",
              _TS) for i, mid in enumerate(mem_ids)])
        conn.executemany(
            "INSERT INTO entity(id, kind, canonical_name, created_at, "
            "updated_at) VALUES (?,?,?,?,?)",
            [(e["id"], e["kind"], e["canonical_name"], e["created_at"],
              e["updated_at"]) for e in RECLASSIFY["entities"]]
            + ([("00000000-0000-4000-8000-000000000006", "tool", "ripgrep",
                 _TS, _TS)] if extra_tool else []))
        conn.executemany(
            "INSERT INTO entity_alias(entity_id, alias_norm) VALUES (?,?)",
            [(a["entity_id"], a["alias_norm"]) for a in RECLASSIFY["aliases"]]
            + ([("00000000-0000-4000-8000-000000000006", "rg")]
               if extra_tool else []))
        conn.executemany(
            "INSERT INTO memory_entity(memory_id, entity_id, role) "
            "VALUES (?,?,?)",
            [(m["memory_id"], m["entity_id"], m["role"])
             for m in RECLASSIFY["memory_entity"]]
            + ([("40000000-0000-4000-8000-000000000004",
                 "00000000-0000-4000-8000-000000000006", "mentions")]
               if extra_tool else []))
        conn.commit()
    finally:
        conn.close()

# Issue #176 Design step 7 — the exact 30 strings, in this exact order.
CLASSIFY_ROWS = [
    {"name": "localhost", "expected": "host"},
    {"name": "127.0.0.1", "expected": "host"},
    {"name": "api.example.com", "expected": "host"},
    {"name": "build-host-1.example", "expected": "host"},
    {"name": "db-host.internal", "expected": "host"},
    {"name": "postgres:5432", "expected": "service"},
    {"name": "api-service", "expected": "service"},
    {"name": "cache-server", "expected": "service"},
    {"name": "job-daemon", "expected": "service"},
    {"name": "gpt-5.3", "expected": "model"},
    {"name": "claude-3-5-sonnet", "expected": "model"},
    {"name": "llama-3", "expected": "model"},
    {"name": "text-embedding-3-small", "expected": "model"},
    {"name": "owner/repo", "expected": "repo"},
    {"name": "github.com/openai/codex", "expected": "repo"},
    {"name": "openai/zmem", "expected": "repo"},
    {"name": "https://github.com/org/repo", "expected": "repo"},
    {"name": "C:\\work\\main.py", "expected": "file"},
    {"name": "/var/log/app.log", "expected": "file"},
    {"name": "src/main.py", "expected": "file"},
    {"name": "README.md", "expected": "file"},
    {"name": "git status", "expected": "command"},
    {"name": "python -m unittest", "expected": "command"},
    {"name": "npm test", "expected": "command"},
    {"name": "docker build .", "expected": "command"},
    {"name": "ValueError", "expected": "error_class"},
    {"name": "TimeoutError", "expected": "error_class"},
    {"name": "DatabaseException", "expected": "error_class"},
    {"name": "Brett", "expected": "other"},
    {"name": "alpha", "expected": "other"},
]

# Issue #176 Fixtures-to-add section — the fixed seed rows.
_TS = "2026-09-10T00:00:00Z"
RECLASSIFY = {
    "entities": [
        {"id": "00000000-0000-4000-8000-000000000001", "kind": "other",
         "canonical_name": "api.example.com", "created_at": _TS,
         "updated_at": _TS},
        {"id": "00000000-0000-4000-8000-000000000002", "kind": "other",
         "canonical_name": "postgres:5432", "created_at": _TS,
         "updated_at": _TS},
        {"id": "00000000-0000-4000-8000-000000000003", "kind": "other",
         "canonical_name": "gpt-5.3", "created_at": _TS, "updated_at": _TS},
        {"id": "00000000-0000-4000-8000-000000000004", "kind": "other",
         "canonical_name": "owner/repo", "created_at": _TS,
         "updated_at": _TS},
        {"id": "00000000-0000-4000-8000-000000000005", "kind": "other",
         "canonical_name": "Brett", "created_at": _TS, "updated_at": _TS},
    ],
    "aliases": [
        {"entity_id": "00000000-0000-4000-8000-000000000001",
         "alias_norm": "api.example.com"},
        {"entity_id": "00000000-0000-4000-8000-000000000001",
         "alias_norm": "api"},
    ],
    "memory_entity": [
        {"memory_id": "40000000-0000-4000-8000-000000000001",
         "entity_id": "00000000-0000-4000-8000-000000000001",
         "role": "mentions"},
        {"memory_id": "40000000-0000-4000-8000-000000000002",
         "entity_id": "00000000-0000-4000-8000-000000000002",
         "role": "mentions"},
        {"memory_id": "40000000-0000-4000-8000-000000000003",
         "entity_id": "00000000-0000-4000-8000-000000000003",
         "role": "mentions"},
    ],
}

# The operator snapshot block (fixed bytes): the audit measurement that
# motivated per-kind weights, plus the fixture apply's fixed before/after
# outcome. Numbers are FIXED provenance data from the issue, never measured
# at generation time.
OPERATOR_SNAPSHOT = {
    "as_of": "2026-09-10",
    "source": "maintainer store audit v3 (issue #176 evidence)",
    "memory_entity_links": 26511,
    "other_links": 24826,
    "other_link_share": 24826 / 26511,
    "fixture": {
        "entity_rows": 5,
        "memory_entity_rows": 3,
        "before_other_link_share": 1.0,
        "after_other_link_share": 0.0,
        "changed": 4,
    },
}


def _write_bytes(path: Path, obj: object) -> None:
    payload = json.dumps(obj, indent=2, sort_keys=False, ensure_ascii=False)
    path.write_bytes((payload + "\n").encode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--emit-store", metavar="DIR_OR_PATH",
        help="build the seeded fixture store at this path (ZMEM_STORE must "
             "point at it in the environment) and exit without rewriting "
             "the JSON fixtures")
    args = parser.parse_args()
    if args.emit_store:
        import os
        store_path = os.environ.get("ZMEM_STORE") or str(
            Path(args.emit_store) / "store.sqlite")
        emit_store(store_path)
        print(f"seeded fixture store at {store_path}")
        return 0
    _write_bytes(FIXTURE_DIR / "classify.json", CLASSIFY_ROWS)
    _write_bytes(FIXTURE_DIR / "reclassify.json", RECLASSIFY)
    _write_bytes(FIXTURE_DIR / "operator-snapshot.json", OPERATOR_SNAPSHOT)
    # The reviewed expected file is NEVER written here (see module docstring).
    print("wrote classify.json, reclassify.json, operator-snapshot.json "
          f"under {FIXTURE_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
