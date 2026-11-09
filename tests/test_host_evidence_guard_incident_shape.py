"""Issue #448 AC1: the incident capture shape must be flagged by the guard.

The shipped incident carried a .json capture whose stdout_raw embedded a
credential key=value assignment (a quoted ADMIN_TOKEN) next to a live
remote_url -- a shape redaction.py's \\S{8,} floor missed because the
assigned value was short/quoted. The frozen post-fix contract (trace
design call, api-contract-spec.md section A):

- ``unsafe_incident_shape_paths(root)`` flags any dict-shaped *.json that
  is capture-shaped (a ``remote_url`` key OR any ``://`` string value) AND
  carries a credential key=value assignment (key family token/api_key/
  secret/password/passwd/pwd/access_key/admin_token, ``[=:]`` separator,
  value of >= 4 non-space chars, quoted values included);
- returns relative posix paths, sorted;
- returns [] on the shipped docs/host-gate-evidence corpus.

Every credential-looking value below is an obviously-synthetic marker; no
real capture content is reproduced.

Run: python tests/test_host_evidence_guard_incident_shape.py
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
from host_evidence_guard import unsafe_incident_shape_paths  # noqa: E402
from host_evidence_guard import (  # noqa: E402
    INCIDENT_SHAPE_DETECTOR_ID,
    git_blob_sha,
)


def _entry_for(path, count, position=0):
    """An allowlist entry built from the CURRENT file state (the same
    construction the allowlist surface documents). The temp corpus in
    every caller holds the capture at the root as capture.json."""
    return {
        "path": "capture.json",
        "detector": INCIDENT_SHAPE_DETECTOR_ID,
        "file_blob_sha": git_blob_sha(path),
        "occurrence_position": position,
        "count": count,
    }


class IncidentShapeDetectorTest(unittest.TestCase):
    """AC1: the incident credential+remote_url capture shape is flagged."""

    def test_synthetic_incident_shape_is_flagged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            incident = root / "incident"
            incident.mkdir()
            capture = {
                "remote_url": "https://hub.synthetic.invalid:8443/api",
                "stdout_raw": (
                    "powershell -Command \"$env:ADMIN_TOKEN = "
                    "'tok_SYNTHETIC_4b1c'; git clone "
                    "https://host.synthetic.invalid/repo\""),
            }
            (incident / "capture.json").write_text(
                json.dumps(capture), encoding="utf-8")
            self.assertEqual(
                unsafe_incident_shape_paths(root),
                ["incident/capture.json"],
                "the incident capture shape (quoted credential assignment "
                "next to a live remote_url) must be flagged with its "
                "relative posix path")

    def test_short_value_below_redaction_floor_is_caught(self):
        # A 4-char assigned value slips under redaction.py's \S{8,} floor;
        # the incident detector's >= 4 non-space-char floor must catch it.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = {
                "remote_url": "https://hub.synthetic.invalid:8443/api",
                "stdout_raw": "TOKEN=abcd",
            }
            (root / "capture.json").write_text(
                json.dumps(capture), encoding="utf-8")
            self.assertEqual(
                unsafe_incident_shape_paths(root),
                ["capture.json"],
                "a credential assignment whose value is shorter than the "
                "redaction floor must still be flagged")

    def test_quoted_value_is_caught(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = {
                "remote_url": "https://hub.synthetic.invalid:8443/api",
                "note": "TOKEN: \"tok_SYNTHETIC_q7\"",
            }
            (root / "capture.json").write_text(
                json.dumps(capture), encoding="utf-8")
            self.assertEqual(
                unsafe_incident_shape_paths(root),
                ["capture.json"],
                "a quoted credential value behind a ':' separator must be "
                "flagged")

    def test_punctuation_bearing_value_is_caught(self):
        # Critic round 1 (BLOCKER 2): the frozen contract says >= 4
        # NON-SPACE chars -- an alphabet whitelist would silently miss real
        # punctuation-bearing values. a!b! is 4 chars of punctuation-bearing
        # value that no [A-Za-z0-9...]{4,} whitelist can match.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = {
                "remote_url": "https://hub.synthetic.invalid:8443/api",
                "stdout_raw": "export TOKEN='a!b!'",
            }
            (root / "capture.json").write_text(
                json.dumps(capture), encoding="utf-8")
            self.assertEqual(
                unsafe_incident_shape_paths(root),
                ["capture.json"],
                "a punctuation-bearing credential value (a!b!) must be "
                "flagged -- non-space semantics, not an alphabet whitelist")

    def test_prefixed_env_var_key_is_caught(self):
        # Implementation-review round 1 (Important): a \b prefix silently
        # excluded the common env-var shapes -- GITHUB_TOKEN=, MY_SECRET=,
        # AUTH_TOKEN= must all flag (the incident's ADMIN_TOKEN only
        # survived via the admin[_-]token enumeration).
        for assignment in ("GITHUB_TOKEN=abcd", "MY_SECRET=abcd",
                           "AUTH_TOKEN=abcd"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                capture = {
                    "remote_url": "https://hub.synthetic.invalid/api",
                    "stdout_raw": assignment,
                }
                (root / "capture.json").write_text(
                    json.dumps(capture), encoding="utf-8")
                self.assertEqual(
                    unsafe_incident_shape_paths(root),
                    ["capture.json"],
                    "%s must be flagged (prefixed env-var credential key)"
                    % assignment)

    def test_scheme_url_value_without_remote_url_key_is_flagged(self):
        # Implementation-review round 1 (Important): the capture gate is
        # remote_url key OR ANY :// string value -- a mutant that keeps
        # only the key branch passes every other leg.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = {
                "note": "cloned from https://host.synthetic.invalid/repo",
                "stdout_raw": "TOKEN=abcd",
            }
            (root / "capture.json").write_text(
                json.dumps(capture), encoding="utf-8")
            self.assertEqual(
                unsafe_incident_shape_paths(root),
                ["capture.json"],
                "a :// string value with NO remote_url key is still "
                "capture-shaped and must be flagged")

    def test_nested_carrier_credential_is_flagged(self):
        # Implementation-review round 1 (Important): the credential walk
        # covers EVERY nested string carrier, not just top-level values.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = {
                "remote_url": "https://hub.synthetic.invalid/api",
                "payload": {"content": [
                    {"type": "output_text",
                     "text": "ran with SECRET=tok_SYNTHETIC_nested"}]},
            }
            (root / "capture.json").write_text(
                json.dumps(capture), encoding="utf-8")
            self.assertEqual(
                unsafe_incident_shape_paths(root),
                ["capture.json"],
                "a credential assignment nested under "
                "payload.content[].text must be flagged")

    def test_camelcase_and_compound_keys_are_caught(self):
        # PR #451 feedback (PRR-004/TF-001/cubic CUBIC-1): camelCase
        # compounds (clientSecret, accessToken) and suffix compounds
        # (SECRET_KEY, PRIVATE_KEY) evade any prefix restriction -- the
        # key family matches ANYWHERE now (over-matching is the safe
        # direction for a human-review flag).
        for assignment in ("clientSecret=abcd", "accessToken=abcd",
                           "SECRET_KEY=abcd", "PRIVATE_KEY=abcd",
                           "sessionToken=abcd"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                capture = {
                    "remote_url": "https://hub.synthetic.invalid/api",
                    "stdout_raw": assignment,
                }
                (root / "capture.json").write_text(
                    json.dumps(capture), encoding="utf-8")
                self.assertEqual(
                    unsafe_incident_shape_paths(root),
                    ["capture.json"],
                    "%s must be flagged (camelCase/suffix compound key)"
                    % assignment)

    def test_lowercase_and_quoted_embedded_values_are_caught(self):
        # PR #451 feedback (TF-001 case mutant, CS-005): lowercase keys
        # match (case-insensitivity pinned) and a quoted value with an
        # embedded quote still matches (value class is non-space, not
        # quote-exclusive).
        for assignment in ("token=abcd", "api_key=abcd",
                           "TOKEN=\"ab'cd\"", "secret:'ab\"cd'"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                capture = {
                    "remote_url": "https://hub.synthetic.invalid/api",
                    "stdout_raw": assignment,
                }
                (root / "capture.json").write_text(
                    json.dumps(capture), encoding="utf-8")
                self.assertEqual(
                    unsafe_incident_shape_paths(root),
                    ["capture.json"],
                    "%s must be flagged (case-insensitive key; "
                    "embedded-quote value)" % assignment)

    def test_schemeless_remote_url_gate_positive(self):
        # PR #451 feedback (TF-002): the remote_url KEY half of the
        # capture gate must fire alone -- the shipped corpus captures use
        # exactly this scheme-less shape ("redacted-hub-url").
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = {
                "remote_url": "redacted-hub-url",
                "stdout_raw": "TOKEN=abcd",
            }
            (root / "capture.json").write_text(
                json.dumps(capture), encoding="utf-8")
            self.assertEqual(
                unsafe_incident_shape_paths(root),
                ["capture.json"],
                "a scheme-less remote_url key with a credential must be "
                "flagged (the OR-branch is load-bearing)")

    def test_duplicate_key_capture_is_flagged(self):
        # PR #451 feedback (PRR-001): a duplicate-key document is
        # VALID-shaped JSON with hostile parse semantics -- flagged under
        # any name, matching the frozen guard's duplicate branch, never
        # silently skipped.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hostile = ('{"remote_url": "https://hub.synthetic.invalid/api",'
                       ' "stdout_raw": "safe",'
                       ' "stdout_raw": "TOKEN=tok_SYNTHETIC_dup"}')
            (root / "capture.json").write_text(hostile, encoding="utf-8")
            self.assertEqual(
                unsafe_incident_shape_paths(root),
                ["capture.json"],
                "a duplicate-key capture must be flagged regardless of "
                "which member survives last-wins parsing")

    def test_multi_occurrence_per_hit_suppression(self):
        # PR #451 feedback (TF-003): suppression is PER OCCURRENCE --
        # one entry silences only its own hit; a whole-file suppressor
        # must fail this file.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = {
                "remote_url": "https://hub.synthetic.invalid/api",
                "stdout_raw": "TOKEN=aaaa GITHUB_TOKEN=bbbb",
            }
            (root / "capture.json").write_text(
                json.dumps(capture), encoding="utf-8")
            self.assertEqual(
                unsafe_incident_shape_paths(root),
                ["capture.json"],
                "precondition: two credential occurrences flagged")
            entries = [_entry_for(
                root / "capture.json", count=2, position=0)]
            self.assertEqual(
                unsafe_incident_shape_paths(root, allowlist=entries),
                ["capture.json"],
                "suppressing occurrence 0 of 2 must leave the file "
                "flagged (occurrence 1 still stands)")
            entries.append(_entry_for(
                root / "capture.json", count=2, position=1))
            self.assertEqual(
                unsafe_incident_shape_paths(root, allowlist=entries),
                [],
                "entries for BOTH occurrences must clear the file")

    def test_benign_capture_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = {
                "remote_url": "redacted-hub-url",
                "stdout_raw": (
                    "[REDACTED: raw recall output removed because it "
                    "contained live credentials]"),
            }
            (root / "capture.json").write_text(
                json.dumps(capture), encoding="utf-8")
            self.assertEqual(
                unsafe_incident_shape_paths(root),
                [],
                "a redacted, URL-free capture must not be flagged")

    def test_url_without_credential_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = {"remote_url": "https://hub.synthetic.invalid/api"}
            (root / "capture.json").write_text(
                json.dumps(capture), encoding="utf-8")
            self.assertEqual(
                unsafe_incident_shape_paths(root),
                [],
                "a live URL alone (no credential assignment) must not be "
                "flagged")

    def test_credential_without_url_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = {"note": "TOKEN=tok_SYNTHETIC_no_url_here"}
            (root / "capture.json").write_text(
                json.dumps(capture), encoding="utf-8")
            self.assertEqual(
                unsafe_incident_shape_paths(root),
                [],
                "a credential assignment alone (no remote_url key, no :// "
                "value -- not capture-shaped) must not be flagged")

    def test_shipped_corpus_stays_clean(self):
        root = _REPO_ROOT / "docs" / "host-gate-evidence"
        self.assertTrue(root.is_dir())
        self.assertEqual(
            unsafe_incident_shape_paths(root),
            [],
            "the shipped host-gate evidence corpus must stay clean under "
            "the incident-shape detector")


if __name__ == "__main__":
    unittest.main()
