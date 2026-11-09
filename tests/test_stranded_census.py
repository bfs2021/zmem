"""Issue #323 acceptance check: no stranded storelib function (AC1).

DISCRIMINATING — expected RED at base: the storelib consolidation
retirement family (#158/#117/#119) left functions defined with zero
non-test callers; every one must be wired into a real production caller
or removed with its tests. The guard is GENERAL — it asserts nothing
about specific function names in the passing state; it asserts the
stranded set is EMPTY and on failure enumerates every stranded
``module:function`` so the disposition ledger (issue AC2) can be built
from the failure output.

Census contract (frozen):

- DEF SET — every ``FunctionDef``/``AsyncFunctionDef`` parsed with ast
  from ``skills/memory/scripts/storelib/*.py``, EXCLUDING dunder protocol
  names (``__*__``): Python invokes those implicitly, so a name-reference
  census can never see their callers and flagging them would be noise.

- CALLER SCOPE — every ``*.py`` under ``skills/``, ``hooks/``, and
  ``scripts/`` (repo-relative POSIX path), excluding paths containing
  ``/test``, ``__pycache__``, and ``.agents/``. A function's callers are
  searched in that scope in THREE shapes (issue #323 AC1, the #253
  sweep's false-positive lesson — model all three):
      1. call shape            ``name(``
      2. bare-name dispatch    a name token NOT followed by ``(``
                               (callable passed around, import binding)
      3. dotted dispatch       ``module.name`` (attribute access)
  For every file EXCEPT the defining module the shapes are matched at
  TEXT level (so import bindings and re-exports count as wiring —
  storelib/__init__.py's from-import lines wire the names they carry).
  In the DEFINING module only real code references count (ast.Name /
  ast.Attribute): a comment or docstring mention of a name is NOT a
  caller, and the ``def`` statement itself contributes no reference
  (internal helpers called by their own module are wired, recursion
  counts, prose does not).

- STRANDED — a def with zero callers anywhere in the caller scope.

Known-good wiring anchors this census must respect (if the census flags
these, the caller-shape matching is wrong, not the code):
      clear_retry_state                  -> skills/memory/scripts/storelib/cli.py
      evaluate_injection_items           -> scripts/eval_inject_runner.py
      injection_per_moment               -> scripts/eval_inject_runner.py

Static analysis only — no store is opened, no ZMEM_* environment is
pinned, and _store_guard is intentionally not imported (nothing here can
resolve a store).

Run: python tests/test_stranded_census.py
"""
from __future__ import annotations

import ast
import re
import sys
import unittest
import warnings
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STORELIB_DIR = REPO_ROOT / "skills" / "memory" / "scripts" / "storelib"

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Wiring anchors from the issue: functions the census must NEVER flag.
_WIRED_ANCHORS = (
    ("clear_retry_state",
     "skills/memory/scripts/storelib/ops_tokens.py"),
    ("evaluate_injection_items",
     "skills/memory/scripts/storelib/eval_gold.py"),
    ("injection_per_moment",
     "skills/memory/scripts/storelib/eval_gold.py"),
)


def _caller_shape_regex(name: str) -> re.Pattern:
    """The three caller shapes, one pattern: call ``name(``, bare-name
    dispatch (token not followed by ``(``), dotted ``.name`` dispatch."""
    esc = re.escape(name)
    return re.compile(
        r"\b" + esc + r"\("                  # 1. call shape
        r"|(?<![\w.])" + esc + r"(?!\s*\()"  # 2. bare-name dispatch
        r"|\." + esc + r"\b")                # 3. dotted dispatch


def _caller_scope_files():
    """Every non-test production .py under skills/, hooks/, scripts/ as
    (posix_rel_path, Path) — repo-relative POSIX paths only (no
    backslashes in anything this module builds)."""
    for scope in ("skills", "hooks", "scripts"):
        for path in (REPO_ROOT / scope).rglob("*.py"):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if "/test" in rel or "__pycache__" in rel or "/.agents/" in rel:
                continue
            yield rel, path


@contextmanager
def _quiet_parse():
    """ast.parse on the analyzed sources re-emits their own Deprecation
    warnings (invalid escape sequences inside the repo's regex strings);
    scoped suppression keeps the failure evidence clean."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        yield


def _defining_module_references(source: str) -> set:
    """Name/Attribute identifiers actually referenced by CODE in a module
    (comments, docstrings and strings are not code references; a def
    statement's own name is not an ast.Name node, so a def never wires
    itself). SyntaxError -> empty set (a file we cannot parse contributes
    no defining-module references; its defs then strand only if no other
    file references them either)."""
    try:
        with _quiet_parse():
            tree = ast.parse(source)
    except SyntaxError:
        return set()
    refs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            refs.add(node.id)
        elif isinstance(node, ast.Attribute):
            refs.add(node.attr)
    return refs


def _module_defs(tree: ast.Module) -> set:
    """Every function/method name defined in a module, dunder protocols
    excluded."""
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not (node.name.startswith("__") and node.name.endswith("__"))
    }


def run_census():
    """Return (stranded, wired_callers) where stranded is a sorted list of
    ``module.py:function`` with zero callers and wired_callers maps a
    function name to its caller files when non-empty (anchors only)."""
    scope = {}
    scope_tokens = {}
    for rel, path in _caller_scope_files():
        source = path.read_text(encoding="utf-8", errors="replace")
        scope[rel] = source
        scope_tokens[rel] = set(_TOKEN_RE.findall(source))

    stranded = []
    wired_callers = {}
    anchor_names = {name for name, _ in _WIRED_ANCHORS}
    for mod in sorted(STORELIB_DIR.glob("*.py")):
        rel = mod.relative_to(REPO_ROOT).as_posix()
        source = scope.get(rel) or mod.read_text(encoding="utf-8",
                                                 errors="replace")
        own_refs = _defining_module_references(source)
        try:
            with _quiet_parse():
                mod_tree = ast.parse(source)
        except SyntaxError as exc:
            raise AssertionError(
                "storelib module failed to parse — census cannot run: "
                + rel + ": " + str(exc)) from exc
        for name in sorted(_module_defs(mod_tree)):
            callers = []
            if name in own_refs:
                callers.append(rel)
            pattern = _caller_shape_regex(name)
            for crel, csrc in scope.items():
                if crel == rel:
                    continue
                if name in scope_tokens[crel] and pattern.search(csrc):
                    callers.append(crel)
            if not callers:
                stranded.append(rel + ":" + name)
            elif name in anchor_names:
                wired_callers[name] = callers
    return sorted(stranded), wired_callers


class StrandedCensusTest(unittest.TestCase):
    maxDiff = None  # the full stranded enumeration IS the evidence

    @classmethod
    def setUpClass(cls):
        cls.stranded, cls.wired_callers = run_census()

    def test_no_stranded_storelib_functions(self):
        listing = "\n".join("  " + entry for entry in self.stranded)
        self.assertEqual(
            self.stranded, [],
            "stranded storelib functions (defined with zero non-test "
            "callers under skills/, hooks/, scripts/ — wire each into a "
            "real production caller or remove it with its tests):\n"
            + listing)

    def test_known_wired_anchors_are_not_stranded(self):
        # Caller-shape sanity anchors from the issue: clear_retry_state is
        # CLI-wired (storelib/cli.py) and the eval pair is wired via
        # scripts/eval_inject_runner.py. If THIS census flags them, the
        # census's caller matching is wrong (the #253 lesson: all three
        # call shapes must be modeled).
        flagged = {
            name: defining
            for name, defining in _WIRED_ANCHORS
            if any(entry == defining + ":" + name
                   for entry in self.stranded)
        }
        self.assertEqual(
            flagged, {},
            "census caller-shape matching is wrong: known-wired functions "
            "flagged as stranded " + repr(flagged))
        for name, _defining in _WIRED_ANCHORS:
            self.assertTrue(
                self.wired_callers.get(name),
                "anchor " + name + " must have at least one caller in the "
                "census's caller scope")


if __name__ == "__main__":
    unittest.main(verbosity=2)
