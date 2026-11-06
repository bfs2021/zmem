"""Published host evidence must not retain raw recall memory contents.

The stdout_raw guard itself lives behind the production seam
``scripts/host_evidence_guard.py`` (issue #438 AC8): this file drives the
imported guard so the naming rule is enforced wherever the evidence tree
grows, not only under the filenames a past incident happened to clean.
The richer published-corpus verifier (raw recall markers, Tier-0 text,
live remote_url) stays test-local.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

REDACTED = "[REDACTED: raw recall output removed because it contained live credentials]"

# The stdout_raw guard lives behind the production seam
# scripts/host_evidence_guard.py (issue #438 AC8). At a base tree without
# the module the guard-dependent tests FAIL (never skip: a silent skip
# would be a vacuous green under this repo's direct-python CI loop).
_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
try:
    from host_evidence_guard import unsafe_recall_paths  # noqa: E402
except ImportError:  # production seam absent at the pre-fix base
    unsafe_recall_paths = None


def _require_guard(testcase):
    testcase.assertIsNotNone(
        unsafe_recall_paths,
        "production guard module missing: scripts/host_evidence_guard.py")
    return unsafe_recall_paths


LAUNCHER_CAPTURE_GLOBS = (
    "*-launcher-recall.json",
    "*-launcher-pretool-recall.json",
    "*-launcher-session-start.json",
)

# Structural marker labels only -- never raw capture content in output.
_RAW_RECALL_MARKER = "user:global"
_TIER0_MARKERS = ("tier 0", "tier0", "tier-0", "tier_0")


def unsafe_capture_paths(root):
    """Launcher-capture corpus scan for issue #438 AC9: report (filename,
    marker-class labels) only; never include raw capture content in
    assertion output. A capture is unsafe when its stdout_raw is not the
    REDACTED marker, its text carries a raw recall marker (a user:global
    row) or a Tier-0 core-text marker, or any remote_url value is live
    (URL-schemed)."""
    unsafe = []
    seen = set()
    for pattern in LAUNCHER_CAPTURE_GLOBS:
        for path in sorted(root.rglob(pattern)):
            if path in seen:
                continue
            seen.add(path)
            rel = path.relative_to(root).as_posix()
            reasons = []
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                payload = None
            raw_text = ""
            try:
                raw_text = path.read_text(encoding="utf-8").lower()
            except OSError:
                raw_text = ""
            # PRR-005: scan the RAW BYTES, not the parsed object —
            # last-wins parsing erases an earlier duplicate value, so a
            # {"stdout_raw": "<raw>", "stdout_raw": marker} file must be
            # judged on the bytes that stay on disk.
            if _RAW_RECALL_MARKER in raw_text:
                reasons.append("raw-recall-marker")
            if any(marker in raw_text for marker in _TIER0_MARKERS):
                reasons.append("tier0-marker")
            if isinstance(payload, dict):
                if payload.get("stdout_raw") != REDACTED:
                    reasons.append("raw-stdout")
                remote = payload.get("remote_url")
                if isinstance(remote, str) and "://" in remote:
                    reasons.append("live-remote-url")
            if reasons:
                unsafe.append((rel, ",".join(reasons)))
    return unsafe


class HostEvidenceRedactionTests(unittest.TestCase):
    def test_published_recall_outputs_are_redacted(self):
        guard = _require_guard(self)
        root = Path(__file__).resolve().parents[1] / "docs" / "host-gate-evidence"
        self.assertTrue(root.is_dir())
        for harness in ("claude", "codex", "zcode"):
            self.assertFalse((root / "287-ac7" / f"{harness}-launcher-recall.json").exists())
        self.assertEqual(guard(root), [])

    def test_new_nested_raw_or_malformed_recall_is_rejected(self):
        guard = _require_guard(self)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "future-host-gate"
            nested.mkdir()
            path = nested / "new-launcher-recall.json"
            for content in (json.dumps({"stdout_raw": "synthetic private recall"}), "{", "{}"):
                path.write_text(content, encoding="utf-8")
                self.assertEqual(guard(root), ["future-host-gate/new-launcher-recall.json"])
            path.write_text(json.dumps({"stdout_raw": REDACTED}), encoding="utf-8")
            self.assertEqual(guard(root), [])

    def test_pretool_and_session_start_naming_is_guarded(self):
        guard = _require_guard(self)
        # Issue #438 AC8: the guard must be NAME-INDEPENDENT for stdout_raw
        # -- any *.json that parses as a dict and carries a stdout_raw key
        # must carry the REDACTED marker, under the shipped
        # *-launcher-pretool-recall.json / *-launcher-session-start.json
        # names AND under an arbitrary future name. Malformed-JSON scoping
        # stays launcher-capture-shaped: a claude-run2-style JSON-plus-
        # trailing-text file with no stdout_raw must not trip the guard.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "future-host-gate"
            nested.mkdir()
            shipped_pretool = nested / "claude-launcher-pretool-recall.json"
            shipped_start = nested / "claude-launcher-session-start.json"
            future_name = nested / "arbitrary-future-name.json"
            run2_like = nested / "claude-run2.json"
            for path in (shipped_pretool, shipped_start, future_name):
                path.write_text(
                    json.dumps({"stdout_raw": "synthetic private recall"}),
                    encoding="utf-8")
            run2_like.write_text(
                json.dumps({"note": "json plus trailing text"}) + " trailing",
                encoding="utf-8")
            dup = nested / "claude-launcher-dup.json"
            dup.write_text(
                '{"stdout_raw": "synthetic private recall", "stdout_raw": '
                '"' + REDACTED + '"}',
                encoding="utf-8")
            self.assertEqual(
                unsafe_recall_paths(root),
                ["future-host-gate/arbitrary-future-name.json",
                 "future-host-gate/claude-launcher-dup.json",
                 "future-host-gate/claude-launcher-pretool-recall.json",
                 "future-host-gate/claude-launcher-session-start.json"])
            dup.unlink()
            for path in (shipped_pretool, shipped_start, future_name):
                path.write_text(json.dumps({"stdout_raw": REDACTED}),
                                encoding="utf-8")
            self.assertEqual(guard(root), [])

    def test_published_captures_carry_no_raw_payload_or_hub_url(self):
        # Issue #438 AC9: the published launcher captures must carry no raw
        # recall payload (user:global rows), no Tier-0 core text, and no
        # live remote_url value. Filenames and marker labels only in the
        # failure output -- never raw capture content.
        root = Path(__file__).resolve().parents[1] / "docs" / "host-gate-evidence"
        self.assertTrue(root.is_dir())
        self.assertEqual(unsafe_capture_paths(root), [])


if __name__ == "__main__":
    unittest.main()
