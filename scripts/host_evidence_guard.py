"""Host-gate evidence stdout_raw guard (issue #438 AC8, production seam).

The published host-gate evidence corpus under docs/host-gate-evidence/
must never retain raw recall memory in a capture's ``stdout_raw`` field.
The rule is NAME-INDEPENDENT for the payload rule: any ``*.json`` that
parses as a dict and carries a ``stdout_raw`` key must carry the fixed
REDACTED marker, whatever the capture is named. Launcher-capture names
additionally require a well-formed dict carrying the marker — a missing
key (e.g. ``{}``) or malformed JSON under a launcher-capture name is
unsafe. Malformed JSON under any other name is ignored (a
claude-run2-shaped file is JSON plus trailing text with no stdout_raw).

Imported by tests/test_host_evidence_redaction.py; reports relative posix
filenames only, never evidence content.
"""
from __future__ import annotations

import json
from pathlib import Path

REDACTED = ("[REDACTED: raw recall output removed because it contained "
            "live credentials]")

LAUNCHER_CAPTURE_SUFFIXES = (
    "-launcher-recall.json",
    "-launcher-pretool-recall.json",
    "-launcher-session-start.json",
)


def _is_launcher_capture(name: str) -> bool:
    return any(name.endswith(suffix) for suffix in LAUNCHER_CAPTURE_SUFFIXES)


def unsafe_recall_paths(root: Path) -> list:
    """Return filenames (relative posix) of unsafe captures under root.

    Never include raw evidence in the returned values.
    """
    unsafe = []
    for path in sorted(root.rglob("*.json")):
        name = path.name
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            payload = None
        if _is_launcher_capture(name):
            # Launcher-capture contract: a well-formed dict whose
            # stdout_raw IS the marker. A missing key is unsafe.
            safe = (isinstance(payload, dict)
                    and payload.get("stdout_raw") == REDACTED)
        elif isinstance(payload, dict) and "stdout_raw" in payload:
            # Name-independent rule: any parsed dict that carries the
            # field must carry the marker.
            safe = payload["stdout_raw"] == REDACTED
        else:
            # Non-launcher name without the field: not this guard's
            # concern (malformed non-launcher JSON is ignored).
            safe = True
        if not safe:
            unsafe.append(path.relative_to(root).as_posix())
    return unsafe
