"""Issue #448 AC3: the guard's suppression allowlist is path+blob shaped.

The allowlist exists so a REVIEWED true-positive can be suppressed without
editing evidence files -- but the entry must pin the FILE identity (git
blob sha), the detector, the occurrence position and the hit count, never
a hash of the matched secret span (a span hash would keep suppressing the
NEXT credential too). The frozen post-fix contract (trace design call,
api-contract-spec.md section A):

- ``parse_allowlist(text)`` refuses (AllowlistError): non-list root,
  non-dict entry, any key outside ALLOWLIST_ENTRY_KEYS (a span-hash key is
  structurally refused), missing key, wrong types (path/detector non-empty
  str; file_blob_sha exactly 40 lowercase hex; occurrence_position
  int >= 0; count int >= 1);
- ``validate_allowlist(entries, root)`` refuses a stale file_blob_sha
  ( != the file's CURRENT git blob sha), a missing path, and an unknown
  detector;
- a scan hit is suppressed ONLY on a full (path, detector, current blob
  sha, occurrence_position, count) match -- any mismatch and the hit
  stands.

Every credential-looking value below is an obviously-synthetic marker.

Run: python tests/test_host_evidence_guard_allowlist.py
"""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# Module-level import of the NEW guard symbols: at the pre-fix base this
# fails with ImportError (the deterministic RED signature under this
# repo's direct-python CI loop -- never a skip).
from host_evidence_guard import (  # noqa: E402
    AllowlistError,
    INCIDENT_SHAPE_DETECTOR_ID,
    JSONL_RAW_RECALL_DETECTOR_ID,
    parse_allowlist,
    unsafe_incident_shape_paths,
    unsafe_jsonl_transcript_paths,
    validate_allowlist,
)

INCIDENT_REL = "incident/capture.json"


def _git_blob_sha(data: bytes) -> str:
    """Independent git blob sha1 (the same construction the guard must
    use): b"blob " + decimal length + NUL + CRLF-NORMALIZED bytes (the
    guard's documented platform-stable identity -- the PR #451 CI round
    caught raw-byte hashing diverging per checkout core.autocrlf)."""
    normalized = data.replace(b"\r\n", b"\n")
    return hashlib.sha1(b"blob %d\0" % len(normalized) + normalized).hexdigest()


class _CorpusTestCase(unittest.TestCase):
    """A one-file corpus whose incident capture is flagged pre-allowlist.

    The capture carries exactly ONE credential assignment, so the hit
    count for (path, INCIDENT_SHAPE_DETECTOR_ID) is unambiguously 1.
    """

    def setUp(self):
        directory = tempfile.TemporaryDirectory(
            prefix="zmem-ac448-allowlist-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        incident = self.root / "incident"
        incident.mkdir()
        self.capture = {
            "remote_url": "https://hub.synthetic.invalid:8443/api",
            "stdout_raw": "TOKEN=tok_SYNTHETIC_4b1c",
        }
        self.capture_path = incident / "capture.json"
        self.capture_path.write_text(
            json.dumps(self.capture), encoding="utf-8")
        # Sanity precondition: the corpus IS a hit before suppression.
        self.assertEqual(
            unsafe_incident_shape_paths(self.root), [INCIDENT_REL])

    def _entry(self, **overrides):
        data = self.capture_path.read_bytes()
        entry = {
            "path": INCIDENT_REL,
            "detector": INCIDENT_SHAPE_DETECTOR_ID,
            "file_blob_sha": _git_blob_sha(data),
            "occurrence_position": 0,
            "count": 1,
        }
        entry.update(overrides)
        return entry


class AllowlistSuppressionTest(_CorpusTestCase):
    """A fully-matching entry suppresses the hit; anything less does not."""

    def test_valid_entry_suppresses_hit(self):
        entries = [self._entry()]
        self.assertEqual(
            unsafe_incident_shape_paths(self.root, allowlist=entries),
            [],
            "a fully-matching allowlist entry (path + detector + current "
            "git blob sha + occurrence_position + hit count) must "
            "suppress the hit")

    def test_hit_survives_wrong_count(self):
        entries = [self._entry(count=99)]
        self.assertEqual(
            unsafe_incident_shape_paths(self.root, allowlist=entries),
            [INCIDENT_REL],
            "an entry whose count does not match the file's actual hit "
            "count must leave the hit standing")

    def test_hit_survives_wrong_blob_sha(self):
        # Critic round 1 (Important 3): scan-level stale-blob leg -- a
        # WELL-FORMED but wrong 40-hex blob sha passed straight to the scan
        # (bypassing validate_allowlist) must not suppress anything.
        other = b'{"unrelated": "content"}'
        entries = [self._entry(file_blob_sha=_git_blob_sha(other))]
        self.assertEqual(
            unsafe_incident_shape_paths(self.root, allowlist=entries),
            [INCIDENT_REL],
            "an entry whose blob sha does not match the file's CURRENT "
            "content must leave the hit standing at scan level")

    def test_hit_survives_wrong_position(self):
        # Critic round 1 (Important 3): occurrence_position is the zero-based
        # ordinal of the hit; the only hit is at position 0, so position 7
        # suppresses nothing.
        entries = [self._entry(occurrence_position=7)]
        self.assertEqual(
            unsafe_incident_shape_paths(self.root, allowlist=entries),
            [INCIDENT_REL],
            "an entry pinning the wrong occurrence position must leave the "
            "hit standing at scan level")

    def test_hit_survives_wrong_path(self):
        # Implementation-review round 1 (Important): a decoy entry whose
        # every field is right EXCEPT the path must suppress nothing.
        entries = [self._entry(path="elsewhere/capture.json")]
        self.assertEqual(
            unsafe_incident_shape_paths(self.root, allowlist=entries),
            [INCIDENT_REL],
            "an entry pinning a different path must leave the hit standing")

    def test_hit_survives_wrong_detector(self):
        # Implementation-review round 1 (Important): same, for the
        # detector axis.
        from host_evidence_guard import JSONL_RAW_RECALL_DETECTOR_ID  # noqa: E402
        entries = [self._entry(detector=JSONL_RAW_RECALL_DETECTOR_ID)]
        self.assertEqual(
            unsafe_incident_shape_paths(self.root, allowlist=entries),
            [INCIDENT_REL],
            "an entry pinning a different detector id must leave the hit "
            "standing")


class JsonlSuppressionTest(unittest.TestCase):
    """Critic round 1 (Important 3): jsonl-detector suppression legs."""

    FENCE_OPEN = "<<<ZMEM_UNTRUSTED_FENCE>>>"
    FENCE_CLOSE = "<<<END_ZMEM_UNTRUSTED_FENCE>>>"
    REL = "session/transcript.jsonl"

    def setUp(self):
        directory = tempfile.TemporaryDirectory(
            prefix="zmem-ac448-allowlist-jsonl-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        session = self.root / "session"
        session.mkdir()
        self.path = session / "transcript.jsonl"
        self.line = {
            "ordinal": 1,
            "type": "response_item",
            "payload": {"content": [{"type": "output_text", "text": (
                self.FENCE_OPEN + "\n- [synthetic-id-9] raw recall row\n"
                + self.FENCE_CLOSE)}]},
        }
        self.path.write_text(
            json.dumps(self.line) + "\n", encoding="utf-8")
        # Precondition: the transcript IS a hit before suppression.
        self.assertEqual(
            unsafe_jsonl_transcript_paths(self.root), [self.REL])

    def _entry(self, **overrides):
        entry = {
            "path": self.REL,
            "detector": JSONL_RAW_RECALL_DETECTOR_ID,
            "file_blob_sha": _git_blob_sha(self.path.read_bytes()),
            "occurrence_position": 0,
            "count": 1,
        }
        entry.update(overrides)
        return entry

    def test_valid_jsonl_entry_suppresses(self):
        self.assertEqual(
            unsafe_jsonl_transcript_paths(
                self.root, allowlist=[self._entry()]),
            [],
            "a fully-matching jsonl allowlist entry must suppress the "
            "fence hit")

    def test_mismatched_jsonl_entry_leaves_hit(self):
        stale = self._entry(file_blob_sha=_git_blob_sha(b"stale-bytes"))
        self.assertEqual(
            unsafe_jsonl_transcript_paths(self.root, allowlist=[stale]),
            [self.REL],
            "a mismatched jsonl allowlist entry must leave the fence hit "
            "standing")

    def _write_lines(self, *texts):
        lines = []
        for index, text in enumerate(texts):
            lines.append(json.dumps({
                "ordinal": index,
                "type": "response_item",
                "payload": {"content": [
                    {"type": "output_text", "text": text}]},
            }))
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _safe_text(self):
        return "plain assistant text with no fence markers"

    def _fence_text(self, marker_id):
        return (self.FENCE_OPEN + "\n- [%s] raw recall row\n"
                % marker_id + self.FENCE_CLOSE)

    def test_hit_ordinal_is_not_the_line_number(self):
        # Critic round 2 (Critical 2): occurrence_position is the ZERO-BASED
        # HIT ORDINAL, never the physical line ordinal. A safe line followed
        # by one offending line puts the ONLY hit at ordinal 0 even though
        # it sits on physical line 1 (0-based): an entry pinning position 1
        # must suppress nothing.
        self._write_lines(self._safe_text(), self._fence_text("syn-id-a"))
        self.assertEqual(
            unsafe_jsonl_transcript_paths(self.root), [self.REL])
        wrong_position = self._entry(occurrence_position=1)
        self.assertEqual(
            unsafe_jsonl_transcript_paths(
                self.root, allowlist=[wrong_position]),
            [self.REL],
            "an entry pinning the LINE NUMBER (1) instead of the HIT "
            "ORDINAL (0) must not suppress the hit")
        right_position = self._entry(occurrence_position=0)
        self.assertEqual(
            unsafe_jsonl_transcript_paths(
                self.root, allowlist=[right_position]),
            [],
            "an entry pinning the HIT ORDINAL (0) with the right count "
            "must suppress the hit")

    def test_multi_hit_count_and_per_occurrence_suppression(self):
        # Two offending lines => count == 2, two occurrences (ordinals 0
        # and 1). One entry suppresses only its own occurrence; both
        # entries are needed to clear the file.
        self._write_lines(
            self._fence_text("syn-id-b"), self._fence_text("syn-id-c"))
        self.assertEqual(
            unsafe_jsonl_transcript_paths(self.root), [self.REL])
        first_only = [self._entry(occurrence_position=0, count=2)]
        self.assertEqual(
            unsafe_jsonl_transcript_paths(self.root, allowlist=first_only),
            [self.REL],
            "suppressing occurrence 0 of 2 must leave the file flagged "
            "(occurrence 1 still stands)")
        both = [
            self._entry(occurrence_position=0, count=2),
            self._entry(occurrence_position=1, count=2),
        ]
        self.assertEqual(
            unsafe_jsonl_transcript_paths(self.root, allowlist=both),
            [],
            "entries for BOTH occurrences must clear the file")
        wrong_count = [self._entry(occurrence_position=0, count=99)]
        self.assertEqual(
            unsafe_jsonl_transcript_paths(self.root, allowlist=wrong_count),
            [self.REL],
            "an entry with the wrong TOTAL count must suppress nothing")


class AllowlistParseValidationTest(_CorpusTestCase):
    """parse_allowlist/validate_allowlist refuse every malformed or stale
    entry -- in particular a hash of the matched secret SPAN."""

    def test_value_hash_entry_is_refused(self):
        # A hash of the matched secret span is NOT the file's blob sha.
        # Leg 1: a sha256 span hash (64 hex) fails parse_allowlist's
        # 40-lowercase-hex rule outright.
        sha256_span = hashlib.sha256(b"tok_SYNTHETIC_4b1c").hexdigest()
        with self.assertRaises(AllowlistError):
            parse_allowlist(json.dumps([self._entry(
                file_blob_sha=sha256_span)]))
        # Leg 2: a sha1 span hash (40 lowercase hex) parses, but
        # validate_allowlist must refuse it as a stale blob sha.
        sha1_span = hashlib.sha1(b"tok_SYNTHETIC_4b1c").hexdigest()
        parsed = parse_allowlist(json.dumps([self._entry(
            file_blob_sha=sha1_span)]))
        with self.assertRaises(AllowlistError):
            validate_allowlist(parsed, self.root)

    def test_unknown_key_is_refused(self):
        entry = self._entry(
            span_sha256=hashlib.sha256(b"tok_SYNTHETIC_4b1c").hexdigest())
        with self.assertRaises(AllowlistError):
            parse_allowlist(json.dumps([entry]))

    def test_missing_key_is_refused(self):
        entry = self._entry()
        del entry["count"]
        with self.assertRaises(AllowlistError):
            parse_allowlist(json.dumps([entry]))

    def test_stale_blob_sha_is_refused(self):
        other = json.dumps({
            "remote_url": "https://other.synthetic.invalid/x",
            "stdout_raw": "TOKEN=tok_SYNTHETIC_deadbeef",
        }).encode("utf-8")
        entry = self._entry(file_blob_sha=_git_blob_sha(other))
        parsed = parse_allowlist(json.dumps([entry]))
        with self.assertRaises(AllowlistError):
            validate_allowlist(parsed, self.root)

    def test_unknown_detector_is_refused(self):
        entry = self._entry(detector="not-a-detector")
        parsed = parse_allowlist(json.dumps([entry]))
        with self.assertRaises(AllowlistError):
            validate_allowlist(parsed, self.root)

    def test_malformed_json_document_is_refused(self):
        # Critic round 1 (NIT 1): a malformed allowlist JSON document is an
        # AllowlistError (fail closed), never a raw json.JSONDecodeError.
        with self.assertRaises(AllowlistError):
            parse_allowlist("{not json")

    def test_non_list_root_is_refused(self):
        # PR #451 feedback (TF-007): a scalar root raises
        # AllowlistError, never a raw TypeError from the iteration.
        with self.assertRaises(AllowlistError):
            parse_allowlist("5")

    def test_missing_path_is_refused(self):
        # PR #451 feedback (TF-008): a path that does not exist raises
        # AllowlistError, never a raw FileNotFoundError from the hash.
        entry = self._entry(path="nope/missing.json")
        parsed = parse_allowlist(json.dumps([entry]))
        with self.assertRaises(AllowlistError):
            validate_allowlist(parsed, self.root)

    def test_uppercase_blob_sha_is_refused(self):
        # PR #451 feedback (TF-009): exactly 40 LOWERCASE hex; an
        # uppercase blob identity is refused by the parse contract.
        upper = _git_blob_sha(self.capture_path.read_bytes()).upper()
        with self.assertRaises(AllowlistError):
            parse_allowlist(json.dumps([self._entry(file_blob_sha=upper)]))

    def test_bool_count_does_not_suppress_at_scan(self):
        # PR #451 feedback (PRR-006): the scan-level suppression applies
        # the same type rules as parse_allowlist -- a bool count (or a
        # float) handed straight to the scanner suppresses nothing.
        entries = [self._entry(count=True)]
        self.assertEqual(
            unsafe_incident_shape_paths(self.root, allowlist=entries),
            [INCIDENT_REL],
            "count=true must not suppress a hit the parser refuses")
        entries = [self._entry(count=1.0)]
        self.assertEqual(
            unsafe_incident_shape_paths(self.root, allowlist=entries),
            [INCIDENT_REL],
            "count=1.0 must not suppress a hit the parser refuses")

    def test_duplicate_key_document_is_refused(self):
        # PR #451 feedback (PRR-018/cubic): a duplicate-key allowlist
        # document is ambiguous -- refused, never last-wins parsed.
        dup = ('[{"path": "a.json", "path": "b.json", "detector": '
               '"incident-credential-remote-url", "file_blob_sha": "'
               + "0" * 40 + '", "occurrence_position": 0, "count": 1}]')
        with self.assertRaises(AllowlistError):
            parse_allowlist(dup)

    def test_positive_parse_validate_suppress_roundtrip(self):
        # PR #451 feedback (cubic CUBIC-6): the happy path is pinned too
        # -- an over-strict parser that always raises would otherwise
        # pass every refusal leg.
        parsed = parse_allowlist(json.dumps([self._entry()]))
        validate_allowlist(parsed, self.root)
        self.assertEqual(
            unsafe_incident_shape_paths(self.root, allowlist=parsed),
            [],
            "a valid, current entry parses, validates, and suppresses")

    def test_bool_for_int_field_is_refused(self):
        # Critic round 1 (NIT 1): bool is an int subclass in Python -- a
        # True count must not pass the int type check.
        with self.assertRaises(AllowlistError):
            parse_allowlist(json.dumps([self._entry(count=True)]))
        with self.assertRaises(AllowlistError):
            parse_allowlist(json.dumps(
                [self._entry(occurrence_position=False)]))


if __name__ == "__main__":
    unittest.main()
