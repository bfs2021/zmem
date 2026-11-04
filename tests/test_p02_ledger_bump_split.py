"""Guardrail for issue #250's defect class.

The behavioural contract is pinned by ``test_p02_ledger_records_expansion.py``
(a neighbour must be recorded, and must render once). This module pins the
*shape* that made the defect possible: the passive selector built ONE list
for TWO accounting sinks, filtered it for popularity (#136), and handed the
same filtered list to the delivery ledger (#114). Rows exempt from one
semantics were silently exempt from the other.

#311 rewrite (decision comment 6639128293): the guard follows the STRUCTURE,
not the spelling. Names are DERIVED from the sink calls — the rowset handed
to BOTH ledger sinks (``ledger.record_undelivered`` AND ``ledger.reserve``;
the reserve path was previously unwatched) and the rowset handed to
``_bump_telemetry`` — so a behaviour-preserving rename keeps the suite green
(pinned by ``RenameResilienceTest``) while a shared-list collapse, a
popularity filter inside the ledger chain, or a helper hiding the filter
order all fail loudly. What the guard covers, stated exactly: the structural
laws below. What it does NOT cover alone: the ledger-rowset arm of the
original #250 defect when reintroduced in a form that preserves every law —
that arm is additionally pinned behaviourally by
``test_rendered_expansion_neighbour_recorded_in_ledger`` and the partial-
exclusion pin (``PartialExclusionExpansionTest``); the earlier docstring's
"cannot be reintroduced quietly" overstated the guard's reach (#311 AC2).

Reads the source of ``select_and_budget_for_injection`` (inject.py)
and of ``recall_memory`` (recall.py); it imports
no storelib module, so it cannot freeze a store path or touch the operator's
``~/.zmem``.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

STORELIB = Path(__file__).resolve().parent.parent / "skills" / "memory" / "scripts" / "storelib"
INJECT_PY = STORELIB / "inject.py"
RECALL_PY = STORELIB / "recall.py"

# Both delivery sinks in the selector. Issue #362 (0.117.0) renamed the bare
# "record" call to the admit-once "record_undelivered" API; issue #284
# (0.109.0) added the reservation sink. The guard follows the SINKS, never a
# spelling of a local variable.
_LEDGER_SINKS = ("record_undelivered", "reserve")
_BUMP_SINK = "_bump_telemetry"


def _selector_source() -> str:
    return INJECT_PY.read_text(encoding="utf-8")


def _selector_body(source: str | None = None) -> ast.FunctionDef:
    """The AST node for select_and_budget_for_injection (of ``source`` when
    given — RenameResilienceTest feeds the renamed tree through here)."""
    src = source if source is not None else _selector_source()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) \
                and node.name == "select_and_budget_for_injection":
            return node
    raise AssertionError("select_and_budget_for_injection not found in inject.py")


def _assigned_name(node: ast.AST) -> str | None:
    """Name a plain or ANNOTATED assignment binds.

    A type annotation (``ledger_rows: list = ...``) is semantically the same
    binding; treating only ``ast.Assign`` as an assignment made the guardrail
    fail on a purely syntactic edit while missing nothing behavioural.
    """
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        return (node.targets[0].id
                if isinstance(node.targets[0], ast.Name) else None)
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    return None


def _assigned_value(node: ast.AST) -> ast.AST | None:
    """Right-hand side of a plain or annotated assignment."""
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        return node.value
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.value
    return None


def _rowset_names(func: ast.FunctionDef) -> set[str]:
    """Locals in ``func`` that hold a list of row dicts.

    A rowset is assigned from ``rows_present_in(...)`` (matched on the CALL
    ATTRIBUTE, so any module alias works) or from a list comprehension over
    another rowset. Naming them lets the sink arguments be matched by meaning
    rather than by position, so a scalar argument like ``resolved_data`` or
    ``moment`` is never mistaken for the row set.
    """
    names: set[str] = set()
    assigns = [node for node in ast.walk(func)
               if _assigned_name(node) is not None]
    for node in assigns:
        target = _assigned_name(node)
        value = _assigned_value(node)
        if isinstance(value, ast.Call):
            if isinstance(value.func, ast.Attribute) \
                    and value.func.attr == "rows_present_in":
                names.add(target)
        elif isinstance(value, ast.ListComp) and len(value.generators) == 1:
            it = value.generators[0].iter
            if isinstance(it, ast.Name) and it.id in names:
                names.add(target)
            elif isinstance(it, ast.Call):
                names.add(target)
    return names


def _call_arg_names(func: ast.FunctionDef, dotted: str) -> list[ast.Call]:
    """Every Call node in ``func`` whose callee name is ``dotted``."""
    out: list[ast.Call] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            name = node.func.attr
        elif isinstance(node.func, ast.Name):
            name = node.func.id
        else:
            name = None
        if name == dotted:
            out.append(node)
    return out


def _sink_rowset(func: ast.FunctionDef, dotted: str) -> str:
    """The PLAIN binding the sink ``dotted`` consumes — fail loudly when no
    argument anchors to a plain rowset ``Name`` (an inline expression over
    non-rowset data is exactly the shape a helper could hide a filter in,
    and the chain laws below could not anchor to it). Sinks take non-rowset
    arguments (resolved_data, session_id, moment, ...) and the bump sink
    consumes an id-comprehension OVER its rowset, so every positional
    argument is examined and a comprehension's iteration binding counts."""
    rowsets = _rowset_names(func)
    calls = _call_arg_names(func, dotted)
    assert calls, f"the {dotted} sink call was not found in the selector"
    names = set()
    for call in calls:
        found = None
        for arg in call.args:
            if isinstance(arg, ast.Name) and arg.id in rowsets:
                found = arg.id
                break
            if isinstance(arg, ast.ListComp) and len(arg.generators) == 1:
                it = arg.generators[0].iter
                if isinstance(it, ast.Name) and it.id in rowsets:
                    found = it.id
                    break
                if isinstance(it, ast.Subscript) \
                        and isinstance(it.value, ast.Name) \
                        and it.value.id in rowsets:
                    found = it.value.id
                    break
        if found is None:
            shapes = [ast.unparse(a) for a in call.args]
            raise AssertionError(
                f"{dotted} must consume a plain rowset binding (directly or "
                f"as its comprehension source); its argument shapes are: "
                f"{shapes}")
        names.add(found)
    if len(names) != 1:
        raise AssertionError(
            f"{dotted} must consume exactly one rowset; got {sorted(names)}")
    return next(iter(names))


def _assignment_chain(func: ast.FunctionDef, name: str) -> list[str]:
    """Source of every ``name = <expr>`` in ``func``, in source order.

    A name may be assigned more than once (the delivered rowset is filtered,
    then filtered again), so this returns the chain rather than the final
    value.
    """
    out: list[tuple[int, str]] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id == name:
            out.append((node.lineno, ast.unparse(node.value)))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == name:
            out.append((node.lineno,
                        ast.unparse(node.value) if node.value else "<annot>"))
    return [src for _, src in sorted(out)]


def _structural_names(func: ast.FunctionDef) -> tuple[str, str]:
    """(ledger_name, bump_name) derived from the sinks, with the shared-sink
    and distinctness laws enforced."""
    ledger_names = {_sink_rowset(func, sink) for sink in _LEDGER_SINKS}
    assert len(ledger_names) == 1, (
        f"both ledger sinks must consume the SAME rowset; got "
        f"{sorted(ledger_names)}")
    ledger_name = next(iter(ledger_names))
    bump_name = _sink_rowset(func, _BUMP_SINK)
    assert bump_name != ledger_name, (
        f"the ledger sinks and {_BUMP_SINK} must not share a rowset "
        "(the #250 defect shape)")
    return ledger_name, bump_name


class _StructuralBase(unittest.TestCase):
    """Shared setUp: parse the selector once per test."""

    def setUp(self):
        self.func = _selector_body()

    @property
    def names(self) -> tuple[str, str]:
        return _structural_names(self.func)


class LedgerBumpSetsAreDistinctTest(_StructuralBase):
    """Issue #250 guardrail: two sinks, two sets — derived by structure."""

    def test_ledger_sinks_and_bump_telemetry_receive_distinct_lists(self):
        """Both ledger sinks consume ONE dedicated rowset; the bump sink
        consumes a DIFFERENT one. Tripwires (non-empty derived sets, plain
        bindings) fire inside the derivation, so the disjointness law can
        never pass vacuously after a refactor unanchors it."""
        ledger_name, bump_name = self.names  # raises loudly when unanchored
        # The derivation itself is the assertion: plain bindings, both sinks
        # agreeing, and distinctness. reached: the two sinks never share.
        self.assertNotEqual(ledger_name, bump_name)

    def test_bump_set_is_derived_from_the_ledger_set(self):
        """bump_rows must be a subset of the ledger rowset, never the
        reverse, and derived in ONE step so a single withheld-secret guard
        covers both consumers."""
        ledger_name, bump_name = self.names
        assigns: dict[str, str] = {}
        for node in ast.walk(self.func):
            target = _assigned_name(node)
            if target is None:
                continue
            value = _assigned_value(node)
            if isinstance(value, ast.ListComp) and len(value.generators) == 1:
                it = value.generators[0].iter
                if isinstance(it, ast.Name):
                    assigns[target] = it.id
        self.assertEqual(
            assigns.get(bump_name), ledger_name,
            f"{bump_name} must be derived in one step from {ledger_name} so "
            "the withheld-secret guard covers both consumers "
            "(issue #250 / #256)")
        self.assertNotEqual(assigns.get(ledger_name), bump_name,
                             f"{ledger_name} must not be derived from "
                             f"{bump_name}; that would re-exempt expansion "
                             "and graph-only rows from the delivery ledger "
                             "(issue #250)")

    def test_ledger_set_is_still_bounded_by_the_rendered_fence(self):
        """#114: the rendered fence is the ceiling. Never record a row the
        model did not see, e.g. a budget-dropped candidate."""
        ledger_name, _bump = self.names
        chain = _assignment_chain(self.func, ledger_name)
        self.assertTrue(chain, f"no assignment to {ledger_name} found")
        self.assertIn(
            "rows_present_in(", chain[0],
            f"{ledger_name} must be seeded from rows_present_in(rows, "
            "rendered) so a budget-dropped or gate-rejected row is never "
            "recorded (#114)")

    def test_withheld_guard_precedes_the_popularity_filter(self):
        """#256 + #311's new law: a withheld row is not rendered content —
        never bumped, never ledgered — AND the #136 popularity filter lives
        ONLY on the bump derivation. The withheld guard is the LAST filter on
        the ledger chain (nothing downstream can reintroduce a withheld row),
        and a popularity filter inside the ledger chain is the exact
        #250-introduced-on-ledger_rows defect the pre-rewrite guard missed
        (plan-critic round 1 executed the mutation: 5/5 green at base)."""
        ledger_name, bump_name = self.names
        ledger_chain = _assignment_chain(self.func, ledger_name)
        bump_chain = _assignment_chain(self.func, bump_name)
        self.assertTrue(ledger_chain, f"no assignment to {ledger_name} found")
        self.assertTrue(bump_chain, f"no assignment to {bump_name} found")

        withheld_at = [i for i, src in enumerate(ledger_chain)
                       if "withheld_for_secret" in src]
        self.assertTrue(
            withheld_at,
            f"{ledger_name} must drop withheld-for-secret rows (#256)")
        self.assertEqual(
            withheld_at[0], len(ledger_chain) - 1,
            "the withheld-secret guard must be the LAST filter applied to "
            f"{ledger_name}, so nothing downstream can reintroduce a "
            "withheld row")

        for popularity in ("link_relation", "_graph_arrival_only"):
            self.assertNotIn(
                popularity, "".join(ledger_chain),
                f"the #136 popularity filter ({popularity}) may appear ONLY "
                f"in the bump derivation; finding it in {ledger_name}'s "
                "chain re-exempts expansion and graph-only rows from the "
                "delivery ledger (issue #250 — the ledger-rowset arm)")
        self.assertEqual(
            len(bump_chain), 1,
            f"{bump_name} must be derived in one step from the already-"
            f"filtered {ledger_name}; a re-filter here could drift from the "
            "withheld guard")
        self.assertNotIn(
            "withheld_for_secret", bump_chain[0],
            f"{bump_name} inherits the withheld guard from {ledger_name}; "
            "repeating the filter here is the drift risk (#256)")
        self.assertIn(
            "link_relation", bump_chain[0],
            f"{bump_name} is the popularity set and must apply the #136 "
            "filter")

    def test_plain_recall_path_does_not_own_the_ledger(self):
        """``recall_memory`` does not record deliveries.

        A plain recall must never write the delivery ledger: if a future edit
        hands ``recall_memory`` either accounting set, the two sinks would
        fuse again on a second path. The check keys on the SELECTOR-DERIVED
        names (post-#311: whatever the bindings are called this week), not on
        yesterday's spellings.
        """
        ledger_name, bump_name = self.names
        tree = ast.parse(RECALL_PY.read_text(encoding="utf-8"))
        funcs = [node for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef)
                 and node.name == "recall_memory"]
        self.assertEqual(
            len(funcs), 1,
            "recall_memory must be found exactly once in recall.py for this "
            "guard to be meaningful")
        for name in (ledger_name, bump_name):
            self.assertFalse(
                _assignment_chain(funcs[0], name),
                f"recall_memory now assigns {name}; the delivery ledger stays "
                "owned by the passive selector only")
        self.assertFalse(
            any(isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("record",) + _LEDGER_SINKS
                and isinstance(node.func.value, ast.Name)
                and "ledger" in node.func.value.id.lower()
                for node in ast.walk(funcs[0])),
            "recall_memory must not call a ledger record function (either "
            "spelling); the plain lane is not a delivery surface")


class RenameResilienceTest(_StructuralBase):
    """#311 AC3: a behaviour-preserving rename of the delivered-rowset
    binding must keep the whole structural guardrail green (the pre-rewrite
    guard matched the ``ledger_rows`` spelling and failed 4/5 on a rename —
    executed at base, trace 307-311-352 probe P2b).

    The rename is applied to the parsed SOURCE in memory (word-boundary; the
    distinct ``post_ledger_rows`` parameter is untouched) and every law is
    re-run against the renamed tree.
    """

    RENAMED = "delivered_rows"

    def test_rename_keeps_every_law_green(self):
        source = _selector_source()
        renamed, count = re.subn(
            r"(?<![A-Za-z_])ledger_rows(?![A-Za-z_])",
            self.RENAMED, source)
        self.assertGreater(
            count, 0,
            "the rename probe found nothing to rename — inject.py's binding "
            "changed spelling; update this test's source token deliberately")
        func = _selector_body(renamed)

        # The derivation must succeed (plain bindings, both sinks, distinct).
        ledger_name, bump_name = _structural_names(func)
        self.assertEqual(ledger_name, self.RENAMED)
        self.assertEqual(bump_name, "bump_rows")

        # Law: seeded from rows_present_in.
        chain = _assignment_chain(func, ledger_name)
        self.assertTrue(chain)
        self.assertIn("rows_present_in(", chain[0])

        # Law: withheld-last + no popularity filter in the ledger chain.
        withheld_at = [i for i, src in enumerate(chain)
                       if "withheld_for_secret" in src]
        self.assertTrue(withheld_at)
        self.assertEqual(withheld_at[0], len(chain) - 1)
        for popularity in ("link_relation", "_graph_arrival_only"):
            self.assertNotIn(popularity, "".join(chain))

        # Law: bump derived in one step WITH the popularity filter, from the
        # renamed ledger set.
        bump_chain = _assignment_chain(func, bump_name)
        self.assertEqual(len(bump_chain), 1)
        self.assertIn("link_relation", bump_chain[0])
        bump_sources = {
            _assigned_name(node): _assigned_value(node)
            for node in ast.walk(func)
            if _assigned_name(node) == bump_name
        }
        comp = bump_sources.get(bump_name)
        self.assertIsInstance(comp, ast.ListComp,
                               "the bump set must be a comprehension over "
                               "the renamed ledger set")
        self.assertIsInstance(comp.generators[0].iter, ast.Name)
        self.assertEqual(comp.generators[0].iter.id, ledger_name,
                         "the bump set must still be derived from the "
                         "renamed ledger set")


if __name__ == "__main__":
    unittest.main(verbosity=2)
