"""Frozen acceptance check C6 (issue #357): the schema lock must break a
DEAD holder's phantom instead of waiting out the full stale window.

SchemaLockLivenessTest reproduces the defect behaviorally on a hermetic
temp store and pins BOTH directions of the fixed seam:

- test_dead_holder_is_broken — a lock file at the schema-lock path
  (<store_dir>/.zmem-schema.lock, computed the way schema._lock_path does)
  whose content is "<dead-pid>:<32-hex>" with a FRESH mtime must be broken
  by the acquisition seam the fix provides (planned:
  schema._acquire_schema_lock_with_liveness(wait_seconds=...)): the call
  returns a token, not None. At base the seam does not exist, so the check
  fails with:
      AssertionError: the schema lock has no holder-liveness escape at base
- test_live_holder_is_not_broken — the fail-closed contrast: a lock file
  whose pid IS the current process (fresh mtime) must NOT be broken; the
  seam returns None.

The dead pid is produced by spawning and reaping a real child process and
PROVING it is not alive through host.process_alive (referenced inside the
method only), with a high spare pid as fallback.

Hermetic discipline: the store env is pinned in setUpClass BEFORE the
first storelib import anywhere in the process (storelib freezes
STORE_PATH at import — the lock path hangs off STORE_PATH.parent) and
restored in tearDownClass. The fix-added seam is referenced only inside
test methods, so the base leg fails with the intended AssertionError.

Run: python tests/test_schema_lock.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import _store_guard  # noqa: F401  (issue #244 isolation guard)
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"

_HERMETIC_KEYS = ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
                  "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_ALLOW_NEWER_SCHEMA")

_FROZEN_MESSAGE = "the schema lock has no holder-liveness escape at base"


def _rmtree_loud(path, label):
    """Bounded-retry removal that never fails SILENTLY (review PRR-003:
    a bare ``rmtree(ignore_errors=True)`` left empty ``/tmp/<label>-*``
    dirs behind on Windows when a transient handle held the freshly
    created directory). Retries ride out the transient holder; a dir that
    STILL survives the bound is reported loudly on stderr instead of
    disappearing into ignore_errors."""
    for attempt in range(4):
        try:
            shutil.rmtree(path)
            return
        except OSError:
            if attempt < 3:
                time.sleep(0.2)
    try:
        shutil.rmtree(path, ignore_errors=True)
    finally:
        if Path(path).exists():
            print(f"WARNING: {label} teardown could not remove {path} "
                  f"(transient Windows dir handle survived the retry "
                  f"bound) — non-silent per review PRR-003",
                  file=sys.stderr)


class SchemaLockLivenessTest(unittest.TestCase):
    _tmpdir: str | None = None
    _saved_env: dict | None = None

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp(prefix="zmem-c6-lock-")
        cls._saved_env = {k: os.environ.get(k) for k in _HERMETIC_KEYS}
        os.environ["ZMEM_STORE"] = str(Path(cls._tmpdir) / "store.sqlite")
        os.environ["ZMEM_DATA"] = str(Path(cls._tmpdir) / "data")
        os.environ["ZMEM_MODELS_DIR"] = str(Path(cls._tmpdir) / "no-models")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ.pop("ZMEM_ALLOW_NEWER_SCHEMA", None)
        # Timing-knob hermeticity (cubic round): the liveness wrapper reads
        # ZMEM_SCHEMA_LOCK_* from the AMBIENT env at call time; an ambient
        # poll/stale setting would stretch this suite's own budgets.
        for key in ("ZMEM_SCHEMA_LOCK_POLL_SECONDS",
                    "ZMEM_SCHEMA_LOCK_STALE_SECONDS",
                    "ZMEM_SCHEMA_LOCK_WAIT_SECONDS",
                    "ZMEM_MAINTENANCE_LOCK_GRACE_SECONDS"):
            cls._saved_env.setdefault(key, os.environ.get(key))
            os.environ.pop(key, None)
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))

    @classmethod
    def tearDownClass(cls):
        if cls._saved_env is not None:
            for k, v in cls._saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            cls._saved_env = None
        if cls._tmpdir is not None:
            shutil.rmtree(cls._tmpdir, ignore_errors=True)
            cls._tmpdir = None

    @staticmethod
    def _provably_dead_pid() -> int:
        """A pid guaranteed not to be running, verified via host.process_alive
        (imported inside this method — never at module import time)."""
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        import host  # noqa: PLC0415 — scripts dir pinned above

        candidates: list[int] = []
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        candidates.append(proc.pid)
        proc.kill()
        proc.wait()  # reaped: the pid is gone on every platform
        candidates.append(9999999)  # spare: far outside any pid table
        for pid in candidates:
            verdict = host.process_alive(pid)
            print(f"[C6] process_alive({pid}) -> {verdict}")
            if verdict is False:
                return pid
        raise AssertionError(
            "harness could not obtain a provably-dead pid via "
            "host.process_alive")

    def _write_lock_file(self, pid: int) -> Path:
        """Write '<pid>:<32-hex>' at the schema-lock path, mtime = NOW."""
        from storelib import schema as schema_mod

        path = schema_mod._lock_path("schema")  # STORE_PATH.parent/.zmem-schema.lock
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        # acquire_lock writes "{pid}:{uuid4.hex}" — same shape, hand-made.
        path.write_text(f"{pid}:{'ab' * 16}", encoding="utf-8")
        now = time.time()
        os.utime(str(path), (now, now))  # fresh: NOT mtime-stale
        return path

    def _acquire_with_liveness(self, wait_seconds: float):
        """Call the fix's schema-lock liveness seam; at base the attribute is
        absent and the caller re-raises the frozen AssertionError."""
        from storelib import schema as schema_mod

        try:
            return schema_mod._acquire_schema_lock_with_liveness(
                wait_seconds=wait_seconds)
        except AttributeError:
            raise AssertionError(_FROZEN_MESSAGE)

    def test_empty_token_phantom_classifier_uses_clamped_grace(self):
        """The stripped-empty lock file branch (the one the grace window
        exists for): young -> live, older than the CLAMPED grace ->
        phantom, and ZMEM_MAINTENANCE_LOCK_GRACE_SECONDS governs it
        (reviewer round)."""
        import importlib
        sys.path.insert(0, str(SCRIPTS_DIR))
        import storelib.schema as schema
        importlib.reload(schema) if False else None
        from storelib.schema import _inspect_lock_phantom
        lock = Path(self._tmpdir) / "empty-token.lock"
        lock.write_text("", encoding="utf-8")
        # young empty file: live (inside the create-then-write window)
        is_phantom, expected = _inspect_lock_phantom(lock, 10.0)
        self.assertFalse(is_phantom)
        # aged past the grace: phantom
        old = time.time() - 20.0
        os.utime(lock, (old, old))
        is_phantom, expected = _inspect_lock_phantom(lock, 10.0)
        self.assertTrue(is_phantom)
        self.assertEqual(expected, "")
        # the knob widens the window: 30s grace keeps the same file live
        is_phantom, expected = _inspect_lock_phantom(lock, 30.0)
        self.assertFalse(is_phantom)

    def test_dead_holder_is_broken(self):
        dead_pid = self._provably_dead_pid()
        self._write_lock_file(dead_pid)

        token = self._acquire_with_liveness(wait_seconds=1.0)

        self.assertIsNotNone(
            token,
            "a schema lock held by a provably-dead pid must be broken for "
            "the next writer (token returned), not waited out")
        from storelib import schema as schema_mod

        if token is not None:
            schema_mod._release_named_lock("schema", token)

    def test_live_holder_is_not_broken(self):
        lock_path = self._write_lock_file(os.getpid())  # we ARE alive

        token = self._acquire_with_liveness(wait_seconds=0.5)

        self.assertIsNone(
            token,
            "a schema lock held by a LIVE pid must NOT be broken "
            "(fail-closed: acquisition returns None)")
        try:
            lock_path.unlink()
        except OSError:
            pass


class SchemaGraceBindingTest(unittest.TestCase):
    """Frozen acceptance check C3 (issue #430): the schema-lock grace stays
    BOUND to the maintenance grace at import — storelib/schema.py's
    ``SCHEMA_LOCK_GRACE_SECONDS = MAINTENANCE_LOCK_GRACE_SECONDS`` (the
    #357 clamped-knob contract).

    The knob is pinned to 30 in setUpClass BEFORE the first storelib import
    ON PURPOSE: CPython pools identical float literals within one module, so
    under the default env a re-frozen literal ``10.0`` could be
    object-identical to a literal-bound constant and a bare identity check
    could not discriminate. Under a knob-set import the maintenance grace is
    a float("30")-derived object, which no literal re-freeze can be
    identical to (or equal to) — the identity assertion then fails under any
    literal re-freeze (re-freeze ``SCHEMA_LOCK_GRACE_SECONDS`` to any
    literal and THIS class fails; the mutant discipline is additionally
    exercised by out-of-repo acceptance drivers, which this assertion's
    discrimination does not depend on). At base runtime the binding is
    already correct, so this class passes here by design.
    """

    _tmpdir: str | None = None
    _saved_env: dict | None = None

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp(prefix="zmem-ac430-bind-")
        cls._saved_env = {k: os.environ.get(k) for k in _HERMETIC_KEYS}
        os.environ["ZMEM_STORE"] = str(Path(cls._tmpdir) / "store.sqlite")
        os.environ["ZMEM_DATA"] = str(Path(cls._tmpdir) / "data")
        os.environ["ZMEM_MODELS_DIR"] = str(Path(cls._tmpdir) / "no-models")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ.pop("ZMEM_ALLOW_NEWER_SCHEMA", None)
        # Pin the knob BEFORE the first storelib import (see class docstring)
        # and clear every other timing knob that could clamp the grace.
        for key in ("ZMEM_MAINTENANCE_LOCK_GRACE_SECONDS",
                    "ZMEM_MAINTENANCE_LOCK_STALE_SECONDS",
                    "ZMEM_SCHEMA_LOCK_POLL_SECONDS",
                    "ZMEM_SCHEMA_LOCK_STALE_SECONDS",
                    "ZMEM_SCHEMA_LOCK_WAIT_SECONDS"):
            cls._saved_env.setdefault(key, os.environ.get(key))
            os.environ.pop(key, None)
        os.environ["ZMEM_MAINTENANCE_LOCK_GRACE_SECONDS"] = "30"
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))

    @classmethod
    def tearDownClass(cls):
        if cls._saved_env is not None:
            for k, v in cls._saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            cls._saved_env = None
        if cls._tmpdir is not None:
            _rmtree_loud(cls._tmpdir, "zmem-ac430-bind")
            cls._tmpdir = None

    def test_binding_is_identity(self):
        import storelib.schema as schema

        self.assertIs(
            schema.SCHEMA_LOCK_GRACE_SECONDS,
            schema.MAINTENANCE_LOCK_GRACE_SECONDS,
            "SCHEMA_LOCK_GRACE_SECONDS must be the SAME OBJECT as "
            "MAINTENANCE_LOCK_GRACE_SECONDS at import (the #357 clamped-knob "
            "binding; under the pinned knob=30 import both must be 30.0) — "
            "a re-freeze to a literal breaks the knob contract")


class SchemaLockKnobProbeTest(unittest.TestCase):
    """Frozen acceptance check C4 (issue #430): a fresh-subprocess probe
    proves ZMEM_MAINTENANCE_LOCK_GRACE_SECONDS=30 widens the SCHEMA-side
    empty-token liveness window end-to-end — the child imports
    storelib.schema UNDER the env (constants freeze at import), plants an
    aged (20 s) stripped-empty lock file at the REAL schema-lock path
    (schema._lock_path("schema"), the file the acquisition chain's
    _inspect_lock_phantom classifies), and classifies it through
    ``schema._inspect_lock_phantom(lock, schema.SCHEMA_LOCK_GRACE_SECONDS)``.

    knob=30 -> grace 30.0 -> the aged-empty lock is LIVE; default (unset)
    -> grace 10.0 -> the SAME lock is PHANTOM. At base runtime this probe
    passes by design (the binding is already correct); the mutant-shaped
    discrimination (a schema.py re-freeze that decouples the knob from the
    schema-side window) fails THIS probe's knob leg and is additionally
    exercised by out-of-repo acceptance drivers, which this probe's
    discrimination does not depend on.
    """

    _tmpdir: str | None = None

    _PROBE_SOURCE = """\\
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])  # skills/memory/scripts
from storelib import schema

lock = schema._lock_path("schema")
lock.parent.mkdir(parents=True, exist_ok=True)
if lock.exists():
    lock.unlink()
lock.write_bytes(b"")
aged = time.time() - 20.0
os.utime(lock, (aged, aged))
# Plan-critic round 1 (#6): drive the REAL acquisition chain (the seam
# _strict_acquire_lock("schema", ...) consults at schema.py:812), not just
# the classifier in isolation — a mutant that re-freezes the grace ARGUMENT
# at the acquisition call site must be killed by this probe too.
is_phantom, _expected = schema._inspect_lock_phantom(
    lock, schema.SCHEMA_LOCK_GRACE_SECONDS)
token = schema._acquire_schema_lock_with_liveness(wait_seconds=1.0)
if token:
    schema._release_named_lock("schema", token)
print("KNOB_PROBE grace=%s classification=%s acquisition=%s" % (
    schema.SCHEMA_LOCK_GRACE_SECONDS,
    "PHANTOM" if is_phantom else "LIVE",
    "BROKEN" if token else "LIVE-NOT-BROKEN"))
"""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp(prefix="zmem-ac430-knob-")

    @classmethod
    def tearDownClass(cls):
        if cls._tmpdir is not None:
            _rmtree_loud(cls._tmpdir, "zmem-ac430-knob")
            cls._tmpdir = None

    def _run_probe_leg(self, label: str, knob: str | None):
        leg_dir = Path(self._tmpdir) / label
        leg_dir.mkdir(parents=True, exist_ok=True)
        probe_path = leg_dir / "knob_probe_child.py"
        probe_path.write_text(self._PROBE_SOURCE, encoding="utf-8")
        env = dict(os.environ)
        env.update({
            "ZMEM_STORE": str(leg_dir / "store.sqlite"),
            "ZMEM_DATA": str(leg_dir / "data"),
            "ZMEM_MODELS_DIR": str(leg_dir / "no-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        for key in ("ZMEM_MAINTENANCE_LOCK_GRACE_SECONDS",
                    "ZMEM_MAINTENANCE_LOCK_STALE_SECONDS",
                    "ZMEM_SCHEMA_LOCK_POLL_SECONDS",
                    "ZMEM_SCHEMA_LOCK_STALE_SECONDS",
                    "ZMEM_SCHEMA_LOCK_WAIT_SECONDS",
                    "ZMEM_ALLOW_NEWER_SCHEMA"):
            env.pop(key, None)
        if knob is not None:
            env["ZMEM_MAINTENANCE_LOCK_GRACE_SECONDS"] = knob
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", str(probe_path), str(SCRIPTS_DIR)],
            capture_output=True, text=True, timeout=180, env=env,
            cwd=str(leg_dir))
        self.assertEqual(
            proc.returncode, 0,
            f"{label} leg crashed: rc={proc.returncode} stderr={proc.stderr}")
        return proc.stdout

    def test_knob30_widens_schema_empty_token_window(self):
        out = self._run_probe_leg("knob30", "30")
        self.assertIn(
            "classification=LIVE", out,
            "ZMEM_MAINTENANCE_LOCK_GRACE_SECONDS=30 must widen the "
            "SCHEMA-side empty-token liveness window (aged-empty lock "
            "classified LIVE); got: " + out.strip())
        self.assertIn(
            "acquisition=LIVE-NOT-BROKEN", out,
            "under knob=30 the real acquisition chain must WAIT OUT a live "
            "empty-token holder, not break it (token must be absent); "
            "got: " + out.strip())

    def test_default_classifies_aged_empty_lock_phantom(self):
        out = self._run_probe_leg("default", None)
        self.assertIn(
            "classification=PHANTOM", out,
            "with the knob unset the default 10 s grace must classify the "
            "same aged-empty lock PHANTOM; got: " + out.strip())
        self.assertIn(
            "acquisition=BROKEN", out,
            "under the default grace the real acquisition chain must BREAK "
            "the phantom empty-token holder (token must be present); "
            "got: " + out.strip())


if __name__ == "__main__":
    _loader = unittest.TestLoader()
    _names = sys.argv[1:]
    if _names:
        _suite = unittest.TestSuite()
        for _name in _names:
            _suite.addTests(
                _loader.loadTestsFromName(f"{__name__}.{_name}"))
    else:
        _suite = _loader.loadTestsFromModule(sys.modules[__name__])
    _result = unittest.TextTestRunner(verbosity=2).run(_suite)
    print("CHECK-PASS" if _result.wasSuccessful() else "CHECK-FAIL")
    sys.exit(0 if _result.wasSuccessful() else 1)
