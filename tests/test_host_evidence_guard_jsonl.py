"""Issue #448 AC2: raw-recall fences in .jsonl transcripts must be flagged.

A published .jsonl transcript carried live <<<ZMEM_UNTRUSTED_FENCE>>> (and
_NEUTRALIZED) markers whose fenced bodies held raw recall rows instead of
[REDACTED: ...] notices. The frozen post-fix contract (trace design call,
api-contract-spec.md section A):

- ``unsafe_jsonl_transcript_paths(root)`` scans *.jsonl line-by-line;
- a MALFORMED line (json.JSONDecodeError) flags the file -- fail-closed,
  .jsonl-only (the claude-run2.json trailing-text contract for *.json is
  untouched);
- a fence marker whose fenced body carries no ``[REDACTED:`` notice flags
  the file (any string value, at any nesting depth);
- returns relative posix paths, sorted;
- flags the shipped transcript's one reviewed record (allowlisted in the
corpus-pinning leg) (its fences wrap [REDACTED: ...]
  notices).

All fenced bodies below are synthetic markers; no shipped recall content
is reproduced.

Run: python tests/test_host_evidence_guard_jsonl.py
"""

import json
from pathlib import Path
import sys
import tempfile
import unittest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# Module-level import of the NEW guard symbol: at the pre-fix base this
# fails with ImportError (the deterministic RED signature under this
# repo's direct-python CI loop -- never a skip).
from host_evidence_guard import (  # noqa: E402
    JSONL_RAW_RECALL_DETECTOR_ID,
    _jsonl_offending_count,
    git_blob_sha,
    unsafe_jsonl_transcript_paths,
)

FENCE_OPEN = "<<<ZMEM_UNTRUSTED_FENCE>>>"
FENCE_CLOSE = "<<<END_ZMEM_UNTRUSTED_FENCE>>>"
FENCE_NEUTRALIZED_OPEN = "<<<ZMEM_UNTRUSTED_FENCE_NEUTRALIZED>>>"


def _response_item_line(text: str) -> dict:
    return {
        "ordinal": 1,
        "type": "response_item",
        "payload": {"content": [
            {"type": "output_text", "text": text}]},
    }


def _write_jsonl(root: Path, rel: str, lines) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(
        (line if isinstance(line, str) else json.dumps(line)) + "\n"
        for line in lines)
    path.write_text(body, encoding="utf-8")
    return path


class JsonlRawRecallDetectorTest(unittest.TestCase):
    """AC2: live-fence and malformed .jsonl transcript shapes are flagged."""

    def test_live_fence_body_is_flagged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_jsonl(root, "session/transcript.jsonl", [_response_item_line(
                FENCE_OPEN + "\n- [synthetic-id-1] [conf=0.9] synthetic "
                "raw recall row\n" + FENCE_CLOSE)])
            self.assertEqual(
                unsafe_jsonl_transcript_paths(root),
                ["session/transcript.jsonl"],
                "a live fence body without a [REDACTED: notice must be "
                "flagged with its relative posix path")

    def test_redacted_notice_fence_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_jsonl(root, "session/transcript.jsonl", [_response_item_line(
                FENCE_OPEN + "\n[REDACTED: raw user:global recall row "
                "removed - see README redaction note]\n" + FENCE_CLOSE)])
            self.assertEqual(
                unsafe_jsonl_transcript_paths(root),
                [],
                "a fence wrapping a [REDACTED: notice must not be flagged")

    def test_event_msg_nested_mirror_is_flagged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            line = {
                "ordinal": 1,
                "type": "response_item",
                "payload": {"item": {"content": [
                    {"type": "output_text", "text": (
                        FENCE_OPEN + "\n- [synthetic-id-2] synthetic "
                        "mirrored raw recall row\n" + FENCE_CLOSE)}]}},
            }
            _write_jsonl(root, "session/mirror.jsonl", [line])
            self.assertEqual(
                unsafe_jsonl_transcript_paths(root),
                ["session/mirror.jsonl"],
                "a live fence nested under payload.item.content[].text "
                "must be flagged")

    def test_neutralized_fence_with_notice_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_jsonl(root, "session/transcript.jsonl", [_response_item_line(
                FENCE_NEUTRALIZED_OPEN + "\n[REDACTED: neutralized raw "
                "recall row removed]\n" + FENCE_CLOSE)])
            self.assertEqual(
                unsafe_jsonl_transcript_paths(root),
                [],
                "a neutralized fence wrapping a [REDACTED: notice must "
                "not be flagged")

    def test_neutralized_fence_without_notice_is_flagged(self):
        # Critic round 1 (Important 4): negative control -- every other
        # neutralized leg carries a notice and expects SAFE, so an
        # implementation that ignores the neutralized marker entirely would
        # pass vacuously. A neutralized open marker with a notice-free body
        # MUST be flagged.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_jsonl(root, "session/transcript.jsonl", [_response_item_line(
                FENCE_NEUTRALIZED_OPEN + "\n- [synthetic-id-3] [conf=0.9] "
                "unredacted neutralized-fence recall row\n" + FENCE_CLOSE)])
            self.assertEqual(
                unsafe_jsonl_transcript_paths(root),
                ["session/transcript.jsonl"],
                "a NEUTRALIZED fence whose body carries no [REDACTED: "
                "notice is raw-recall content and must be flagged")

    def test_notice_outside_fence_body_is_flagged(self):
        # Implementation-review round 1 (Important): the notice must be
        # evaluated INSIDE the fenced body -- a mutant that accepts the
        # whole string whenever any [REDACTED: appears anywhere passes
        # every other leg. Here the notice precedes the fence while the
        # body is raw.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_jsonl(root, "session/transcript.jsonl", [_response_item_line(
                "[REDACTED: unrelated notice in plain text]\n" + FENCE_OPEN
                + "\n- [synthetic-id-4] raw unredacted recall row\n"
                + FENCE_CLOSE)])
            self.assertEqual(
                unsafe_jsonl_transcript_paths(root),
                ["session/transcript.jsonl"],
                "a notice OUTSIDE the fence body must not clear the fence: "
                "the raw body is still flagged")

    def test_malformed_jsonl_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_jsonl(root, "session/transcript.jsonl", [
                {"ordinal": 1, "type": "response_item",
                 "payload": {"content": []}},
                "{not json",
            ])
            self.assertEqual(
                unsafe_jsonl_transcript_paths(root),
                ["session/transcript.jsonl"],
                "a .jsonl file holding a malformed line must be flagged "
                "fail-closed")

    def test_json_extension_malformed_still_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run2_like = root / "claude-run2.json"
            run2_like.write_text(
                json.dumps({"note": "json plus trailing text"}) + " trailing",
                encoding="utf-8")
            self.assertEqual(
                unsafe_jsonl_transcript_paths(root),
                [],
                "fail-closed malformed-line handling is .jsonl-only; a "
                "claude-run2-shape *.json with trailing text must stay "
                "invisible to the jsonl scanner")

    def test_shipped_transcript_hits_are_reviewed_and_allowlisted(self):
        # PR #451 feedback (SH-A-001): the jsonl detector now also governs
        # capture-shaped credential lines, and the SHIPPED transcript's
        # world_state record (deliberate SSRF-probe test values in
        # curated 287-ac7 evidence) is a real hit. The honest end state:
        # the unsuppressed scan FLAGS it, and the reviewed whole-file
        # allowlist entry (current blob identity + the true offending
        # count) suppresses it -- proving both the detection and the
        # suppression surface on the live corpus. A regenerated
        # transcript (new blob) invalidates the entry and fails here,
        # which is the designed re-review trigger.
        root = _REPO_ROOT / "docs" / "host-gate-evidence"
        self.assertTrue(root.is_dir())
        transcript = root / "287-ac7" / "codex-exec-transcript.jsonl"
        flagged = unsafe_jsonl_transcript_paths(root)
        self.assertIn(
            "287-ac7/codex-exec-transcript.jsonl", flagged,
            "the shipped transcript's capture-shaped credential record "
            "must be detected (SSRF-probe values in curated evidence)")
        # LITERAL identity pin (delta-review round: a self-derived
        # entry can never fail, so the documented re-review trigger
        # could not fire). These literals pin the REVIEWED transcript
        # bytes and its offending-line count under the frozen detector
        # semantics; ANY regeneration of the evidence file (new blob)
        # or detector change (new count) fails this check and forces a
        # fresh human review of the corpus before the guard passes it.
        REVIEWED_BLOB_SHA = "f7951941c29828a9271da1c67b3d950a3b9f5924"
        REVIEWED_OFFENDING_LINES = 1
        self.assertEqual(
            git_blob_sha(transcript), REVIEWED_BLOB_SHA,
            "the shipped transcript must be the reviewed bytes -- a "
            "regenerated transcript invalidates the review")
        self.assertEqual(
            _jsonl_offending_count(transcript), REVIEWED_OFFENDING_LINES,
            "the reviewed transcript must carry exactly one offending "
            "line under the frozen detector semantics -- a detector "
            "change invalidates the review")
        entry = {
            "path": "287-ac7/codex-exec-transcript.jsonl",
            "detector": JSONL_RAW_RECALL_DETECTOR_ID,
            "file_blob_sha": REVIEWED_BLOB_SHA,
            "occurrence_position": 0,
            "count": REVIEWED_OFFENDING_LINES,
        }
        self.assertEqual(
            unsafe_jsonl_transcript_paths(root, allowlist=[entry]),
            [],
            "the reviewed whole-file allowlist entry must suppress the "
            "shipped transcript's reviewed hit")
        other = [p for p in flagged
                 if p != "287-ac7/codex-exec-transcript.jsonl"]
        self.assertEqual(
            other, [],
            "no OTHER corpus file may be flagged (the transcript hit is "
            "the only reviewed one)")

    def test_duplicate_key_hidden_fence_is_flagged(self):
        # PR #451 feedback (PRR-002, critic-survived HIGH): last-wins
        # parsing must not let a surviving member hide a live fence from
        # the walk -- a duplicate-key line flags fail-closed. The hostile
        # line is written as RAW TEXT (a plain json.loads in the fixture
        # itself would launder the duplicate before the scan sees it).
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fence_body = (FENCE_OPEN + "\nraw recall row\n" + FENCE_CLOSE)
            first = json.dumps(
                {"payload": {"content": [{"text": fence_body}]}},
                ensure_ascii=False)
            # splice a SECOND payload member (redacted) into the object:
            # strip the closing brace, append the duplicate, re-close.
            line = first[:-1] + (
                ', "payload": {"content": [{"text": '
                '"[REDACTED: clean]"}]}}')
            session = root / "session"
            session.mkdir(parents=True)
            (session / "dup.jsonl").write_text(
                line + "\n", encoding="utf-8")
            self.assertEqual(
                unsafe_jsonl_transcript_paths(root),
                ["session/dup.jsonl"],
                "a duplicate-key line whose FIRST member carries the "
                "fence must be flagged (last-wins parsing may not "
                "launder it)")

    def test_non_utf8_jsonl_is_flagged_not_raised(self):
        # PR #451 feedback (PRR-003): a non-UTF-8 transcript flags the
        # file fail-closed; the scan never aborts and later files still
        # receive verdicts.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bad.jsonl").write_bytes(b'{"a": "\xff\xfe"}')
            good = {"ordinal": 1, "type": "response_item",
                    "payload": {"content": []}}
            _write_jsonl(root, "good.jsonl", [good])
            self.assertEqual(
                unsafe_jsonl_transcript_paths(root),
                ["bad.jsonl"],
                "an undecodable transcript must be flagged (not raised, "
                "not skipped) and the scan must continue to later files")

    def test_unterminated_fence_is_flagged(self):
        # PR #451 feedback (TF-005): a truncated transcript's live fence
        # (open marker, raw body, NO end marker) is exactly the artifact
        # an interrupted session leaves behind.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_jsonl(root, "session/trunc.jsonl", [_response_item_line(
                FENCE_OPEN + "\n- [synthetic-id-7] raw recall rows, "
                "session cut mid-fence")])
            self.assertEqual(
                unsafe_jsonl_transcript_paths(root),
                ["session/trunc.jsonl"],
                "an unterminated fence with a notice-free body must be "
                "flagged (end-of-string closes the body)")

    def test_blank_separator_line_is_tolerated(self):
        # PR #451 feedback (TF-006): blank lines are common in JSONL; a
        # blank separator must not read as a malformed line.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_jsonl(root, "session/blank.jsonl", [
                _response_item_line("plain text"),
                _response_item_line("more plain text"),
            ])
            path = root / "session" / "blank.jsonl"
            path.write_text(
                path.read_text(encoding="utf-8").replace("\n", "\n\n"),
                encoding="utf-8")
            self.assertEqual(
                unsafe_jsonl_transcript_paths(root),
                [],
                "blank separator lines must be tolerated")

    def test_capture_shaped_credential_line_is_flagged(self):
        # PR #451 feedback (SH-A-001): the incident credential shape
        # (capture-shaped dict + credential key=value) is governed in
        # transcripts too -- the detectors are no longer
        # extension-disjoint for this shape.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            line = {"remote_url": "https://hub.synthetic.invalid/api",
                    "stdout_raw": "TOKEN=abcd"}
            _write_jsonl(root, "session/capture.jsonl", [line])
            self.assertEqual(
                unsafe_jsonl_transcript_paths(root),
                ["session/capture.jsonl"],
                "a capture-shaped transcript line carrying a credential "
                "must be flagged")


if __name__ == "__main__":
    unittest.main()
