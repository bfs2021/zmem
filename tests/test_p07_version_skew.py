"""Issue #322 (P07) acceptance checks: mixed-version delivery-state skew.

Frozen acceptance contract for a shared data dir touched by clients of
different versions (0.95.0 tooling against 0.96.0 delivery-state files):

  AC1 DoctorSkewTest (NEW-SURFACE, RED at base by absence) — the doctor
      must surface delivery-state segment-family skew and name a recovery
      action. Two trigger legs:
        (a) an UNKNOWN family file (``<8hex>.ledger.seg2.<hex>``) seeded
            under <data>/ops — skew by construction, no seam needed;
        (b) a KNOWN segment (``<8hex>.ledger.seg.<hex>``) seeded while the
            client's supported-family set is REDUCED via the environment
            seam below.
      Plus a NEGATIVE CONTROL: a clean ops dir (no ledger-family files)
      must produce no skew diagnostic — green at base, must STAY green
      after the fix.

      The asserted stable properties (do NOT pin wording beyond this): the
      ``doctor.py --format json`` output includes a check whose id contains
      "skew", OR whose text (summary/details, case-insensitive) contains
      "skew" AND ("ledger.seg" OR "ops"). An always-present healthy check
      carrying "skew" in its id violates the negative control: the skew
      diagnostic is expected only when skew exists.

  AC2 PurgeOrderTest (DISCRIMINATING, RED at base) — when a purge runs on
      a data dir with segment-family files the running client cannot
      scrub, it must fail closed (exit 5) BEFORE deleting store content.
      At base the exit-5 refusal arrives AFTER the store deletion has
      committed, so the row-still-exists assertion is the required RED.

SEAM CONTRACT — ZMEM_TEST_LEDGER_FAMILIES (binding on the implementer):

    When ZMEM_TEST_LEDGER_FAMILIES is set to a comma-separated list of
    delivery-state family tokens, the client treats EXACTLY that set as
    its supported delivery-state family set (REPLACING the built-in
    default) wherever the supported set is consulted — including the
    doctor's skew detection and purge's scrub-family coverage. Family
    tokens (mapped however the implementation names its registry):
        ledger      -> plain ledger files, ``*.ledger``
        ledger.seg  -> segment sidecars, ``*.ledger.seg.*``
        ledger.tmp  -> temp ledgers, ``*.ledger.tmp.*``
    Therefore ``ZMEM_TEST_LEDGER_FAMILIES=ledger`` means: ONLY the plain
    ``*.ledger`` family is supported; any ``*.ledger.seg.*`` file present
    under <data>/ops is outside the supported set (version skew) and the
    doctor must surface it, exactly as an unknown-family file does with
    the default set. Unset -> the client's real default supported set.

Isolation (tests/test_hermes_convention_order.py pattern): the store env
is pinned to a throwaway dir BEFORE the issue #244 guard import (this
file never imports storelib in-process; store.py and doctor.py run as
subprocesses against per-test scratch stores); tearDownModule restores
the ambient environment and evicts any storelib modules a co-run froze
in. The real ~/.zmem store is never touched.

Run (no pytest required):
    python -X utf8 tests/test_p07_version_skew.py                 (all)
    python tests/test_p07_version_skew.py PurgeOrderTest          (class)
    python tests/test_p07_version_skew.py \
        PurgeOrderTest.test_residue_check_precedes_store_deletion (one)
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
DOCTOR_PY = SCRIPTS_DIR / "doctor.py"
PYTHON = sys.executable
NS = "project:skewtest"

NEEDLE = "p07skewneedle-9f3ac1d7"
TARGET_CONTENT = NEEDLE + " carrier row for the version-skew probe"

# Environment the subprocess surfaces re-pin per test; these keys are
# snapshotted at import and restored in tearDownModule.
_PIN_KEYS = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR", "ZMEM_MODEL_AUTODOWNLOAD",
    "ZMEM_HOME", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "ZMEM_CAPTURE_MODE", "ZMEM_TEST_LEDGER_FAMILIES", "ZMEM_TEST_NOW",
    "ZMEM_INJECT", "ZMEM_BACKUP_DIR",
    # HOME/USERPROFILE are SET to the scratch home for every child (PR
    # #452 review round): doctor reads Path.home() for the host plugin
    # registry, so an install-skew host would leak its marketplace-skew /
    # duplicate-install checks into these legs (tests/test_doctor.py:123
    # sets both to a scratch home for the same reason).
)

# Import-time scratch pins so the issue #244 guard (and anything else
# resolving a store in-process) never sees the operator's real ~/.zmem.
# _HERMETIC_HOME is a real scratch directory SET as HOME/USERPROFILE in
# every child env (popping them is not enough: Windows Path.home() falls
# back through HOMEDRIVE/HOMEPATH, so the host home still leaks — PR #452
# review round 2). HOMEDRIVE/HOMEPATH are pinned to the scratch drive-root
# form so that fallback lands inside the scratch home too.
_BOOT_TMP = tempfile.mkdtemp(prefix="zmem-p07-boot-")
_HERMETIC_HOME = os.path.join(_BOOT_TMP, "home")
os.makedirs(_HERMETIC_HOME, exist_ok=True)
atexit.register(shutil.rmtree, _BOOT_TMP, True)
_SAVED_ENV = {key: os.environ.get(key) for key in _PIN_KEYS}
_SAVED_MODULES = {key: value for key, value in list(sys.modules.items())
                  if key == "storelib" or key.startswith("storelib.")}
os.environ.update({
    "ZMEM_STORE": os.path.join(_BOOT_TMP, "store.sqlite"),
    "ZMEM_DATA": _BOOT_TMP,
    "ZMEM_MODELS_DIR": os.path.join(_BOOT_TMP, "no-such-models"),
    "ZMEM_MODEL_AUTODOWNLOAD": "0",
})
for _k in ("ZMEM_HOME", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
           "ZMEM_TEST_LEDGER_FAMILIES"):
    os.environ.pop(_k, None)

import _store_guard  # noqa: E402,F401  (issue #244 isolation guard)


def tearDownModule():  # noqa: N802 - unittest hook
    """Restore the ambient env and evict storelib modules this file's
    co-run may have loaded (pytest citizenship, hermes PRR-022 pattern)."""
    for key in [key for key in list(sys.modules)
                if key == "storelib" or key.startswith("storelib.")]:
        del sys.modules[key]
    sys.modules.update(_SAVED_MODULES)
    for key, value in _SAVED_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _restore_ambient() -> None:
    tearDownModule()


atexit.register(_restore_ambient)


class _P07Base(unittest.TestCase):
    """One throwaway store + its pinned subprocess env (model absent)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-p07-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = os.path.join(self.tmp, "store.sqlite")
        self.data_dir = self.tmp
        self.env = {**os.environ}
        self.env.update({
            "ZMEM_STORE": self.store,
            "ZMEM_DATA": self.data_dir,
            "ZMEM_MODELS_DIR": os.path.join(self.tmp, "no-such-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "PYTHONUTF8": "1",
        })
        for var in _PIN_KEYS:
            if var not in ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
                           "ZMEM_MODEL_AUTODOWNLOAD"):
                self.env.pop(var, None)
        # Hermetic home (see _HERMETIC_HOME): SET, never just popped —
        # Windows Path.home() falls back through HOMEDRIVE/HOMEPATH.
        self.env["HOME"] = _HERMETIC_HOME
        self.env["USERPROFILE"] = _HERMETIC_HOME
        self.env["HOMEDRIVE"] = _HERMETIC_HOME[:2]
        self.env["HOMEPATH"] = _HERMETIC_HOME[2:]

    # -- subprocess surfaces -------------------------------------------------

    def _run_store(self, *args, timeout=600):
        return subprocess.run(
            [PYTHON, "-X", "utf8", str(STORE_PY), *args],
            env=self.env, capture_output=True, text=True, timeout=timeout,
            cwd=str(REPO_ROOT),
        )

    def _run_doctor(self):
        return subprocess.run(
            [PYTHON, "-X", "utf8", str(DOCTOR_PY), "--format", "json"],
            env=self.env, capture_output=True, text=True, timeout=300,
            cwd=str(REPO_ROOT),
        )

    def add_row(self, content):
        """Seed through the REAL CLI add --json; return the new row id."""
        r = self._run_store(
            "add", "--namespace", NS, "--type", "fact",
            "--content", content, "--signal", "test",
            "--confidence", "0.9", "--tags", "p07skew", "--json",
            timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["id"]

    def seed_ops_file(self, name):
        """Write a delivery-state file under <data>/ops holding the needle
        bytes (the residue the running client cannot scrub)."""
        ops = Path(self.data_dir) / "ops"
        ops.mkdir(parents=True, exist_ok=True)
        target = ops / name
        target.write_text(
            "delivery envelope text " + NEEDLE
            + " in an out-of-family segment\n",
            encoding="utf-8")
        self.assertTrue(target.is_file(),
                        "fixture: seeded ops file must exist: " + name)
        return target

    def doctor_payload(self):
        r = self._run_doctor()
        try:
            return json.loads(r.stdout)
        except ValueError:
            self.fail(
                "fixture error: doctor did not print JSON (rc="
                + str(r.returncode) + ")\nstderr tail:\n"
                + "\n".join(r.stderr.strip().splitlines()[-10:]))

    # -- sqlite surfaces -----------------------------------------------------

    def qone(self, sql, params=()):
        """Scalar read against ZMEM_STORE via a read-only handle, closed
        promptly (Windows open-handle hazard)."""
        conn = sqlite3.connect(
            "file:" + self.store.replace(os.sep, "/") + "?mode=ro", uri=True)
        try:
            return conn.execute(sql, params).fetchone()[0]
        finally:
            conn.close()

    # -- the skew-check predicate (AC1 stable properties) --------------------

    @staticmethod
    def _skew_checks(payload):
        """Checks matching the frozen skew contract: the id names the
        delivery-state skew check, or the serialized text (case-insensitive)
        carries "delivery-state" AND "skew" (the substring-loose earlier
        matcher also matched the unrelated pre-existing marketplace-skew
        check — PR #452 review round)."""
        out = []
        for check in payload.get("checks", []) or []:
            cid = str(check.get("id", "")).lower()
            blob = json.dumps(check, sort_keys=True).lower()
            if "delivery-state-skew" in cid:
                out.append(check)
            elif "delivery-state" in blob and "skew" in blob:
                out.append(check)
        return out


# ---------------------------------------------------------------------------
# AC1 (#322, NEW-SURFACE): the doctor names delivery-state segment skew
# and the recovery step when the ops dir carries files outside the
# running client's supported family set.
# ---------------------------------------------------------------------------
class DoctorSkewTest(_P07Base):
    def test_doctor_flags_unknown_family_segment(self):
        # Leg (a): a delivery-state file in a family NO client knows
        # (`.ledger.seg2.`) — skew by construction, no seam involved.
        self.add_row(TARGET_CONTENT)
        self.seed_ops_file(
            "aa11bb22.ledger.seg2." + uuid.uuid4().hex)

        payload = self.doctor_payload()
        matched = self._skew_checks(payload)
        self.assertTrue(
            matched,
            "doctor must surface a delivery-state skew diagnostic when "
            "<data>/ops holds a delivery-state file outside the running "
            "client's supported family set (unknown family "
            "aa11bb22.ledger.seg2.*): a check whose id names "
            "delivery-state-skew or whose text carries 'delivery-state' "
            "plus 'skew' (naming the recovery). Got "
            + str(len(payload.get("checks", []) or [])) + " checks, none "
            "matching: "
            + ", ".join(str(c.get("id")) for c in payload.get("checks", [])))

    def test_doctor_flags_known_family_outside_supported_set(self):
        # Leg (b): a REAL segment family file (`.ledger.seg.`), skew only
        # because the seam reduced the supported set to plain-ledger.
        self.add_row(TARGET_CONTENT)
        self.seed_ops_file(
            "cc33dd44.ledger.seg." + uuid.uuid4().hex)
        self.env["ZMEM_TEST_LEDGER_FAMILIES"] = "ledger"

        payload = self.doctor_payload()
        matched = self._skew_checks(payload)
        self.assertTrue(
            matched,
            "doctor must surface a skew diagnostic when a known segment "
            "family file (cc33dd44.ledger.seg.*) is outside the client's "
            "supported family set (ZMEM_TEST_LEDGER_FAMILIES=ledger): a "
            "check whose id contains 'skew' or whose text mentions 'skew' "
            "plus 'ledger.seg'/'ops' (naming the recovery). Got "
            + str(len(payload.get("checks", []) or [])) + " checks, none "
            "matching: "
            + ", ".join(str(c.get("id")) for c in payload.get("checks", [])))

    def test_doctor_clean_ops_dir_has_no_skew_diagnostic(self):
        # NEGATIVE CONTROL (green at base, must stay green after the fix):
        # a clean ops dir — no ledger-family files at all — must carry no
        # skew diagnostic.
        self.add_row(TARGET_CONTENT)
        ops = Path(self.data_dir) / "ops"
        if ops.is_dir():
            for stale in ops.glob("*.ledger*"):
                stale.unlink()

        payload = self.doctor_payload()
        matched = self._skew_checks(payload)
        self.assertEqual(
            matched, [],
            "a clean ops dir (no ledger-family files) must produce no skew "
            "diagnostic; matched: "
            + json.dumps(matched, sort_keys=True))


# ---------------------------------------------------------------------------
# AC2 (#322, DISCRIMINATING): purge fails closed BEFORE deleting store
# content — "exit 5 with the store intact", not "exit 5 after the store
# deletion committed".
# ---------------------------------------------------------------------------
class PurgeOrderTest(_P07Base):
    def test_residue_check_precedes_store_deletion(self):
        mid = self.add_row(TARGET_CONTENT)
        self.assertEqual(
            self.qone("SELECT COUNT(*) FROM memory WHERE id=?", (mid,)), 1,
            "fixture: the seeded row must exist before the purge")

        self.seed_ops_file(
            "aa11bb22.ledger.seg2." + uuid.uuid4().hex)

        r = self._run_store("purge", "--id", mid, timeout=600)
        self.assertEqual(
            r.returncode, 5,
            "purge on an unscrubbable-family data dir must fail closed "
            "with exit 5 (rc=" + str(r.returncode) + ")\nstderr tail:\n"
            + "\n".join(r.stderr.strip().splitlines()[-6:]))

        self.assertEqual(
            self.qone("SELECT COUNT(*) FROM memory WHERE id=?", (mid,)), 1,
            "the exit-5 fail-closed refusal must arrive BEFORE the store "
            "deletion commits — the store row must still exist (the store "
            "was destroyed before purge's late residue verify; move the "
            "residue check ahead of the destructive step)")


# ---------------------------------------------------------------------------
# AC4 (#322, PRESERVING, executable form): the documented recovery —
# delete `ops/*.ledger.seg.*` — is executable exactly as printed.
# ---------------------------------------------------------------------------
class RecoveryDocsTest(_P07Base):
    """AC4 (#322) executable form: the CHANGELOG's documented recovery
    command works as printed and is correctly scoped.

    Documented artifact (CHANGELOG.md, section ``## [0.96.0] - 2026-10-13``,
    line 2024 at freeze time): "rolling back to 0.95.0 requires deleting
    ``ops/*.ledger.seg.*`` first (0.95.0 neither sweeps nor scrubs
    segments, and its purge verify fails closed on them)". The check binds
    to the glob the doc names (``ops/*.ledger.seg.*``) — never to the line
    number, which drifts as the CHANGELOG grows.

    PRESERVING — green at base AND at head: the command works today; this
    pins that it stays executable, that it selects exactly the segment
    family, and that it never over-deletes (a plain ``*.ledger`` file in
    the same ops dir must survive the recovery byte-identical).
    """

    def test_documented_recovery_command_executes_as_printed(self):
        changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        start = changelog.find("## [0.96.0]")
        self.assertNotEqual(
            start, -1,
            "CHANGELOG.md must carry the 0.96.0 section that documents "
            "the delivery-state segment rollback step")
        end = changelog.find("\n## [", start)
        section = changelog[start:end if end != -1 else len(changelog)]
        self.assertIn(
            "ops/*.ledger.seg.*", section,
            "the 0.96.0 CHANGELOG section must document the recovery step "
            "verbatim: deleting `ops/*.ledger.seg.*`")

        # Scratch fixture only (never a real data dir): one real store
        # row, TWO segment-family files (the exact family the recovery
        # names), and one plain ledger that must survive.
        self.add_row(TARGET_CONTENT)
        seg_a = self.seed_ops_file(
            "aa11bb22.ledger.seg." + uuid.uuid4().hex)
        seg_b = self.seed_ops_file(
            "bb33cc44.ledger.seg." + uuid.uuid4().hex)
        plain = self.seed_ops_file("ddeeff00.ledger")
        plain_bytes = plain.read_bytes()
        self.assertTrue(seg_a.is_file() and seg_b.is_file()
                        and plain.is_file(),
                        "fixture: seeded ops files must exist")

        # The documented deletion, executed as printed against the
        # scratch ops dir: remove `ops/*.ledger.seg.*`.
        removed = []
        for victim in (Path(self.data_dir) / "ops").glob("*.ledger.seg.*"):
            victim.unlink()
            removed.append(victim.name)
        self.assertEqual(
            sorted(removed), sorted([seg_a.name, seg_b.name]),
            "the documented glob must select exactly the seeded segment "
            "files (correct scoping: no more, no less)")

        self.assertFalse(
            seg_a.exists() or seg_b.exists(),
            "both segment files must be gone after the documented "
            "recovery")
        self.assertTrue(
            plain.is_file() and plain.read_bytes() == plain_bytes,
            "the recovery must NOT over-delete: a plain *.ledger file in "
            "the same ops dir keeps its bytes")
        self.assertEqual(
            self.qone("SELECT COUNT(*) FROM memory"), 1,
            "the store must still open read-only (sqlite URI, no "
            "exception) with its row intact after the recovery")


def _run_filtered(argv):
    """__main__ runner accepting optional positional filters of the form
    ClassName or ClassName.test_method (the issue's argv shape
    `python tests/test_p07_version_skew.py PurgeOrderTest.
    test_residue_check_precedes_store_deletion`); no filters -> whole module.
    """
    module = sys.modules[__name__]
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for spec in argv:
        class_name, dot, method = spec.partition(".")
        cls = getattr(module, class_name, None)
        if not (isinstance(cls, type) and issubclass(cls, unittest.TestCase)):
            print("unknown test class: " + class_name, file=sys.stderr)
            return 2
        if dot:
            if not hasattr(cls, method):
                print("unknown test method: " + spec, file=sys.stderr)
                return 2
            suite.addTest(cls(method))
        else:
            suite.addTests(loader.loadTestsFromTestCase(cls))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    _argv = sys.argv[1:]
    if _argv:
        raise SystemExit(_run_filtered(_argv))
    unittest.main(verbosity=2)
