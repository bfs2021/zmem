"""Issue #307: the contested-link marker survives strong-token re-delivery.

A hazard neighbour that reached the fence through a ``contradicts`` edge from
its parent is ledgered in the same render, so on the next pretool pull it
enters the session's delivered-exclusion set. The strong-token un-exclusion
(inject.py) releases the PARENT (its ledgered text matches the operation
tokens) — and since #307 (decision comment 6639125597), it also releases the
parent's ledgered CONTRADICTS neighbours, so the neighbour re-renders through
the expansion walk carrying ``[CONTESTED LINK: <parent-id>]`` exactly when
the operator is about to run the hazardous op the parent describes.

Four arms:

- ``ContestedLinkRedeliveryTest`` — the contract (red at base 7b9d871: the
  marker vanished from the re-delivery envelope).
- ``RelatedNeighbourStaysSuppressedTest`` — the exemption is contradicts-
  ONLY: a ``related`` neighbour carries no marker and stays suppressed
  (green at base; must stay green).
- ``ExplicitlyExcludedContestedNeighbourStaysSuppressedTest`` — explicit
  caller ``--exclude`` always applies: a contradicts neighbour named on the
  command line is never re-released even while its parent re-renders.
- ``ParentStrongReleaseIgnoresExplicitExclusionTest`` — pins the documented
  PRE-EXISTING parent behaviour (decision comment 6639125597): a strong-
  matched parent is released even when it is ALSO in ``exclude_ids`` — the
  hazard-override rationale. Pinning makes any future change deliberate.

Drives the real ``store.py`` CLI via subprocess against a throwaway temp
store (the ``_store_guard`` import keeps the operator's ``~/.zmem`` out of
scope). No network, no model download (``ZMEM_MODELS_DIR`` pinned at a
missing directory).

Run: python tests/test_contested_link_redelivery.py
"""
from __future__ import annotations

import _store_guard  # noqa: F401  (issue #244 isolation guard)

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"

_KNOBS = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_ROOT", "ZMEM_NAMESPACE",
    "ZMEM_HOST", "ZMEM_SESSION", "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID",
    "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA", "ZMEM_MODELS_DIR",
    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_AUTO_REKEY",
    "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET", "ZMEM_INJECT_MARGIN",
    "ZMEM_CTX_BUDGET", "ZMEM_TIER_SLOTS", "ZMEM_MMR_LAMBDA", "ZMEM_GRAPH_SEED",
    "ZMEM_LINK_THRESHOLD", "ZMEM_CROSS_ENCODER", "ZMEM_CROSS_ENCODER_SHADOW",
    "ZMEM_CAPTURE", "ZMEM_QUERY_CONTEXT", "ZMEM_DELIVER_WINDOW_S",
    "ZMEM_LEDGER_CAP", "ZMEM_TEST_NOW",
)
_PREFIXES = ("ZMEM_INJECT_FLOOR_", "ZMEM_ARM_CAP_", "ZMEM_UNFOLD_")


def _clean_env(tmp: Path) -> dict:
    """Hermetic subprocess env (throwaway store, model absent, every ambient
    zmem knob stripped so an operator's environment cannot redirect the
    fixture)."""
    strip = set(_KNOBS) | {k for k in os.environ if k.startswith(_PREFIXES)}
    env = {k: v for k, v in os.environ.items() if k not in strip}
    env.update({
        "ZMEM_STORE": str(tmp / "store.sqlite"),
        "ZMEM_DATA": str(tmp / "data"),
        "ZMEM_MODELS_DIR": str(tmp / "no-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_AUTO_REKEY": "0",
        "ZMEM_INJECT": "1",
        "PYTHONUTF8": "1",
    })
    return env


class _RedeliveryBase(unittest.TestCase):
    """Two linked rows in a throwaway store; pretool pulls in one session."""

    RELATION = "contradicts"
    NAMESPACE = "project:p307"
    SESSION = "sess-p307"
    PARENT = ("never git push --force to the shared flange branch "
              "after hours")
    NEIGHBOUR = "zebra migration patterns note okapi crossing"
    LINK_EXTRA: tuple = ("--reason", "p307 contested redelivery fixture")

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-p307-"))
        # Review round (PRR-013 / cubic P3): register the cleanup BEFORE any
        # fixture step that can raise — unittest skips tearDown when setUp
        # raises, so a failing `store.py add` used to leak the temp store.
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = _clean_env(self.tmp)
        self.pid = self._add_row(self.PARENT)
        self.nid = self._add_row(self.NEIGHBOUR)
        self._run_cli("links", "--add", "--id", self.pid, "--id", self.nid,
                      "--relation", self.RELATION, *self.LINK_EXTRA)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_cli(self, *args: str) -> str:
        proc = subprocess.run(
            [sys.executable, str(STORE_PY), *args],
            capture_output=True, text=True, encoding="utf-8",
            env=self.env, timeout=300,
        )
        self.assertEqual(
            proc.returncode, 0,
            f"store.py {' '.join(args)} -> rc={proc.returncode}\n{proc.stderr}")
        return proc.stdout

    def _add_row(self, content: str) -> str:
        stdout = self._run_cli(
            "add", "--namespace", self.NAMESPACE, "--type", "fact",
            "--content", content, "--signal", "test",
            "--confidence", "0.9", "--json")
        return json.loads(stdout)["id"]

    def _call(self, *extra: str, query: str = "git push --force") -> dict:
        """One pretool injection pull in the class session. ``extra`` rides
        through (e.g. ``--exclude`` arms); ``query`` overrides the pull's
        query (the F-02 pin uses a different second-call query)."""
        return json.loads(self._run_cli(
            "recall", "--query", query,
            "--namespace", self.NAMESPACE, "--no-hybrid", "--no-bump",
            "--for-injection", "--json", "--session-id", self.SESSION,
            "--moment", "pretool", *extra))


class ContestedLinkRedeliveryTest(_RedeliveryBase):
    """#307 AC1: a strong-token pretool re-delivery of the parent re-delivers
    its ledgered contradicts neighbour WITH the contested marker."""

    def test_strong_token_redelivery_keeps_contested_marker(self):
        first = self._call()
        fence1 = first.get("rendered") or ""
        self.assertIn(f"[{self.pid}]", fence1,
                      "precondition: parent renders on call 1")
        self.assertIn(f"[{self.nid}]", fence1,
                      "precondition: contested neighbour renders on call 1")
        self.assertIn(f"[CONTESTED LINK: {self.pid}]", fence1,
                      "precondition: call-1 fence carries the contested "
                      "marker naming the parent")
        nrows = [row for row in first.get("results", [])
                 if row.get("id") == self.nid]
        self.assertEqual(len(nrows), 1,
                         "precondition: neighbour appears exactly once")
        self.assertEqual(nrows[0].get("link_relation"), "contradicts")
        self.assertIs(nrows[0].get("contested_link"), True)
        self.assertEqual(nrows[0].get("link_of"), self.pid,
                         "precondition: the contested row names its parent")

        second = self._call()
        fence2 = second.get("rendered") or ""
        self.assertIn(
            f"[{self.pid}]", fence2,
            "precondition: the strong ops tokens re-release the parent "
            "itself (or this test proves nothing)")
        self.assertIn(
            f"[{self.nid}]", fence2,
            "a contradicts neighbour ledgered as this parent's expansion "
            "child must re-render when the parent is re-delivered on a "
            "strong-token pretool event (#307)")
        self.assertIn(
            f"[CONTESTED LINK: {self.pid}]", fence2,
            "the re-delivered neighbour must keep its contested marker "
            "naming the parent (#307)")


class RelatedNeighbourStaysSuppressedTest(_RedeliveryBase):
    """The exemption is contradicts-ONLY (decision 6639125597): a ``related``
    neighbour carries no marker and must NOT be re-released by its parent's
    strong-token re-delivery. Green at base (base releases nothing) and must
    stay green after the exemption landed."""

    RELATION = "related"
    NAMESPACE = "project:p307neg"
    SESSION = "sess-p307neg"
    LINK_EXTRA = ()

    def test_related_neighbour_not_re_released(self):
        fence1 = self._call().get("rendered") or ""
        self.assertIn(f"[{self.pid}]", fence1,
                      "precondition: parent renders on call 1")
        self.assertIn(f"[{self.nid}]", fence1,
                      "precondition: related neighbour renders on call 1")
        fence2 = self._call().get("rendered") or ""
        self.assertIn(f"[{self.pid}]", fence2,
                      "precondition: parent re-released on call 2")
        self.assertNotIn(
            f"[{self.nid}]", fence2,
            "the exemption is contradicts-ONLY: a related neighbour stays "
            "suppressed on the parent's strong-token re-delivery (#307 "
            "decision 6639125597)")


class ExplicitlyExcludedContestedNeighbourStaysSuppressedTest(_RedeliveryBase):
    """Explicit caller ``--exclude`` always applies: a contradicts neighbour
    named on the command line is never re-released by the #307 exemption,
    even while its parent re-renders on the strong-token match."""

    def test_explicitly_excluded_contested_neighbour_stays_suppressed(self):
        first = self._call()
        self.assertIn(f"[{self.nid}]", first.get("rendered") or "",
                      "precondition: neighbour renders on call 1")
        second = self._call("--exclude", self.nid)
        fence2 = second.get("rendered") or ""
        self.assertIn(f"[{self.pid}]", fence2,
                      "precondition: the strong match still re-releases the "
                      "PARENT (exclude_ids does not gate the parent arm)")
        self.assertNotIn(
            f"[{self.nid}]", fence2,
            "explicit exclude_ids must win over the #307 neighbour release: "
            "a caller-excluded contradicts neighbour stays suppressed")


class ParentStrongReleaseIgnoresExplicitExclusionTest(_RedeliveryBase):
    """Pins the DOCUMENTED pre-existing parent behaviour (decision comment
    6639125597): a strong-matched parent is released from exclusions even
    when it is ALSO in ``exclude_ids`` — the hazard override. The #307 PR
    deliberately leaves the parent arm untouched; this pin makes any future
    policy change deliberate rather than accidental."""

    def test_strong_parent_released_despite_explicit_exclusion(self):
        first = self._call()
        self.assertIn(f"[{self.pid}]", first.get("rendered") or "",
                      "precondition: parent renders on call 1")
        second = self._call("--exclude", self.pid)
        fence2 = second.get("rendered") or ""
        self.assertIn(
            f"[{self.pid}]", fence2,
            "PINNED PRE-EXISTING POLICY (#307 decision): a strong-matched "
            "parent is released even when it is also explicitly excluded — "
            "the hazard override. Change this deliberately, with an issue.")


class DirectMatchNeighbourRendersUnmarkedTest(_RedeliveryBase):
    """Maintainer review F-02: the release un-suppresses; it never
    force-includes. The re-render is retrieval-driven, and a contradicts
    neighbour whose OWN text direct-matches the query renders WITHOUT the
    [CONTESTED LINK] marker (the marker is expansion provenance — links.py
    stamps contested_link only on expansion arrivals). This arm pins that
    disclosed behaviour so a future change to add force-include or
    direct-match stamping is deliberate."""

    # The neighbour's own text carries the operation tokens, so it
    # direct-matches every pull of this session (no expansion arrival) —
    # exactly the shape the F-02 pin needs: release fires, retrieval is
    # direct, and the marker must stay OFF.
    NEIGHBOUR = "git push --force postmortem"

    def test_direct_matched_neighbour_renders_unmarked(self):
        # Call 1: parent AND neighbour both direct-match the query (the
        # neighbour's text carries the op tokens) — both render, both land
        # in the ledger. Precondition: the neighbour is delivered.
        first = self._call()
        fence1 = first.get("rendered") or ""
        self.assertIn(f"[{self.nid}]", fence1,
                      "precondition: the neighbour direct-matches and "
                      "renders on call 1")
        self.assertIn(f"[{self.pid}]", fence1,
                      "precondition: the parent renders on call 1")
        # Call 2: same tokens strong-match BOTH ledgered rows — the parent
        # arm releases them (and the #307 contradicts arm is a no-op here:
        # the neighbour is itself strong-matched, already released). The
        # neighbour re-renders via DIRECT retrieval: no expansion walk, so
        # no contested marker — pinning maintainer F-02.
        second = self._call()
        fence2 = second.get("rendered") or ""
        self.assertIn(f"[{self.pid}]", fence2,
                      "precondition: the strong match re-releases the parent")
        self.assertIn(
            f"[{self.nid}]", fence2,
            "precondition: the direct-matching neighbour is re-retrieved on "
            "call 2, or this pin proves nothing")
        self.assertNotIn(
            "[CONTESTED LINK", fence2,
            "a direct-retrieved neighbour re-renders unmarked (the marker "
            "is expansion provenance; maintainer F-02 pin)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
