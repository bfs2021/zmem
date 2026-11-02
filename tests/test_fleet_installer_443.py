"""Static contract + shipped-classifier behavioral tests for the hub\ninstallers' serve-port behavior (issue #423). The installers are host-only\nscripts (a Tailscale node, a scheduled task / systemd unit — not\nCI-executable), so the static pins hold the contract at the source level,\nwhile the behavioral oracle drives the SHIPPED classifiers\n(scripts/fleet/serve-status-classify.sh + scripts/fleet/serve-status-lib.ps1)\nthrough a shared status matrix — a mutant that edits the shipped matcher\nturns these red, because the test executes the shipped code rather than a\nre-implementation (review round 3: PRR-002/FB-8, FB-7).\n\nRun: python tests/test_fleet_installer_443.py\n"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PS1 = REPO_ROOT / "scripts" / "fleet" / "install-hub.ps1"
SH = REPO_ROOT / "scripts" / "fleet" / "install-hub.sh"
CLS_SH = REPO_ROOT / "scripts" / "fleet" / "serve-status-classify.sh"
LIB_PS1 = REPO_ROOT / "scripts" / "fleet" / "serve-status-lib.ps1"
FLEET = REPO_ROOT / "docs" / "FLEET.md"

# The shared status matrix:
# (name, our_port, target_port, status, want_occupier, want_ours)
CASES = [
    ("plex-443", 8765, 443, "https://host.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:32400", 1, 0),
    ("all-ours-idempotent", 8765, 443, "https://host.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:8765", 1, 1),
    ("funnel-dup-ours", 8765, 443,
     "https://host.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:8765\n\n"
     "# Funnel on:\nhttps://host.ts.net (funnel)\n|-- / proxy http://127.0.0.1:8765", 1, 1),
    ("tcp-443", 8765, 443, "|-- tcp://foo.ts.net:443 (TLS-terminated TCP, tailnet only)", 1, 0),
    ("non443-header", 8765, 443, "https://host.ts.net:8443 (tailnet only)\n|-- / proxy http://127.0.0.1:8765", 0, 0),
    ("no-config", 8765, 443, "No serve config", 0, 0),
    # PRR-001: a hub handler under a non-default header never masks a :443 operator.
    ("prr001-hub-8443-operator-443", 8765, 443,
     "https://host.ts.net:8443 (tailnet only)\n|-- / proxy http://127.0.0.1:8765\n"
     "https://host.ts.net (tailnet only)\n|-- / proxy http://127.0.1:32400", 1, 0),
    ("mixed-block", 8765, 443,
     "https://host.ts.net (tailnet only)\n|-- / proxy http://127.0.1:32400\n"
     "|-- /zmem proxy http://127.0.0.1:8765", 1, 0),
    # FB-3: a plain http:// handler is a :80 handler, not a :443 occupier.
    ("http-80-not-occupier", 8765, 443, "http://host.ts.net (tailnet only)\n|-- / proxy http://127.0.1:8080", 0, 0),
    ("http-443-explicit", 8765, 443, "http://host.ts.net:443 (tailnet only)", 1, 0),
    ("port-prefix", 8765, 443, "https://host.ts.net (tailnet only)\n|-- / proxy http://127.0.1:87650", 1, 0),
    ("empty-input", 8765, 443, "", 0, 0),
    # Review round 4: EVERY target-port block counts (the ps1
    # last-block-only regression) - an all-ours :443 block followed by a
    # :8443 block stays ours.
    ("ours443-then-8443-block", 8765, 443,
     "https://host.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:8765\n\n"
     "https://host.ts.net:8443 (tailnet only)\n|-- / proxy http://127.0.0.1:3000", 1, 1),
    ("operator443-then-ours8443", 8765, 443,
     "https://host.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:32400\n\n"
     "https://host.ts.net:8443 (tailnet only)\n|-- / proxy http://127.0.0.1:8765", 1, 0),
    # Review round 4: the explicit-port escape is itself occupancy-checked.
    ("target-8443-operator", 8765, 8443,
     "https://host.ts.net:8443 (tailnet only)\n|-- / proxy http://127.0.0.1:32400", 1, 0),
    ("target-8443-ours-idempotent", 8765, 8443,
     "https://host.ts.net:8443 (tailnet only)\n|-- / proxy http://127.0.0.1:8765", 1, 1),
    # Critic round: ours is an AND across every target block - an
    # operator or mixed target block is never masked by an all-ours
    # sibling, in either order, and a tcp forward on the target
    # suppresses ours at ANY position.
    ("p1-operator443-then-ours443", 8765, 443,
     "https://host.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:32400\\n\n\n"
     "https://host.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:8765", 1, 0),
    ("p2-ours443-then-operator443", 8765, 443,
     "https://host.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:8765\\n\n\n"
     "https://host.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:32400", 1, 0),
    ("p3-tcp443-before-ours443", 8765, 443,
     "|-- tcp://foo.ts.net:443 (TLS-terminated TCP, tailnet only)\n"
     "https://host.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:8765", 1, 0),
    ("p5-tcp443-after-ours443", 8765, 443,
     "https://host.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:8765\n"
     "|-- tcp://foo.ts.net:443 (TLS-terminated TCP, tailnet only)", 1, 0),
    ("p4-boundary-our-ip-prefix-port", 8765, 443,
     "https://host.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:87650", 1, 0),
    ("p2b-mixed443-then-ours443", 8765, 443,
     "https://host.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:32400\n"
     "|-- /zmem proxy http://127.0.0.1:8765\\n\n\n"
     "https://host.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:8765", 1, 0),
]


EXPECTED_RANGE = "1-65535"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _resolve_bash() -> str | None:
    candidate = os.environ.get("ZMEM_TEST_BASH") or shutil.which("bash")
    if not candidate:
        return None
    probe = subprocess.run(
        [candidate, "-c", "echo bash-ok"], capture_output=True, text=True, timeout=15)
    if probe.returncode != 0 or probe.stdout.strip() != "bash-ok":
        return None
    return candidate


def _resolve_pwsh() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def _drive_bash_classifier(bash: str, port: int, target: int, status: str) -> tuple[int, int]:
    proc = subprocess.run(
        [bash, str(CLS_SH)], input=status, text=True, capture_output=True,
        env={**os.environ, "PORT": str(port), "TARGET_PORT": str(target)},
        timeout=60)
    parts = proc.stdout.strip().split()
    if proc.returncode != 0 or len(parts) < 2:
        raise AssertionError(
            f"classifier failed rc={proc.returncode} out={proc.stdout!r} "
            f"err={proc.stderr[-400:]!r}")
    return int(parts[0]), int(parts[1])


def _drive_ps1_classifier(pwsh: str, lines: list[str], our_port: int,
                          target_port: int = 443) -> tuple[bool, bool]:
    def q(s: str) -> str:
        return s.replace("'", "''")
    script = (
        ". '" + str(LIB_PS1) + "';\n"
        "$l = @(" + ",".join("'" + q(line) + "'" for line in lines) + ");\n"
        "$r = Get-ServeStatusClassification -Lines $l -OurPort " + str(our_port) + " -TargetPort " + str(target_port) + "\n"
        "Write-Output (\"{0},{1}\" -f $r.Occupier, $r.Ours)"
    )
    proc = subprocess.run(
        [pwsh, "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=120)
    lines_out = proc.stdout.strip().splitlines()
    if proc.returncode != 0 or not lines_out:
        raise AssertionError(
            f"ps1 classifier failed rc={proc.returncode} out={proc.stdout!r} "
            f"err={proc.stderr[-400:]!r}")
    occ_s, ours_s = lines_out[-1].split(",")
    return occ_s == "True", ours_s == "True"


def _drive_ps1_classifier_lines(pwsh: str, port: int, target: int, status: str) -> tuple[int, int]:
    lines = status.splitlines() or [""]
    occ, ours = _drive_ps1_classifier(pwsh, lines, port, target)
    return (1 if occ else 0), (1 if ours else 0)


def _drivers() -> list[tuple[str, object]]:
    out = []
    bash = _resolve_bash()
    if bash:
        out.append(("bash+shipped-sh-classifier",
                    lambda p, s, t: _drive_bash_classifier(bash, p, s, t)))
    pwsh = _resolve_pwsh()
    if pwsh:
        out.append(("pwsh+shipped-ps1-classifier",
                    lambda p, s, t: _drive_ps1_classifier_lines(pwsh, p, s, t)))
    return out


class FleetInstaller443PsTest(unittest.TestCase):
    """AC1 — the Windows installer refuses an occupied :443 before ANY host\n    side effect and takes a -HttpsPort parameter."""

    def test_httpsport_parameter_exists(self):
        src = _src(PS1)
        self.assertRegex(
            src, r"\[int\]\$HttpsPort\s*=\s*443",
            "the -HttpsPort parameter (default 443) is missing — the "
            "installer cannot publish on any other port")
        self.assertRegex(
            src, r"\$HttpsPort -lt 1 -or \$HttpsPort -gt 65535",
            "the full 1-65535 validation must pin both bounds (FB-7)")
        self.assertLess(
            src.index("[string]$TaskName"), src.index("[int]$HttpsPort"),
            "-HttpsPort must be declared AFTER -TaskName so existing "
            "positional calls keep their meaning (FB-1)")

    def test_serve_call_uses_the_parameter(self):
        src = _src(PS1)
        self.assertRegex(
            src, r"tailscale\s+serve\s+--bg\s+--https=\$HttpsPort",
            "the serve call still hardcodes its https port")
        self.assertNotRegex(
            src, r"--https=443(?!\s*http)",
            "a hardcoded --https=443 remains somewhere in the serve path")

    def test_guard_precedes_every_host_side_effect(self):
        """FB-5: the serve-occupancy preflight runs BEFORE the wrapper write,\n        the task registration, and the task start — a refusal leaves the host\n        exactly as found. The serve EXECUTION stays after the task state\n        check, so serve is never configured against a dead hub."""
        src = _src(PS1)
        guard = src.index("serve status")
        self.assertLess(guard, src.index("New-Item -ItemType Directory -Force -Path $DataDir"))
        self.assertLess(guard, src.index("Register-ScheduledTask"))
        self.assertLess(guard, src.index("Start-ScheduledTask"))
        self.assertGreater(
            src.index("tailscale serve --bg"),
            src.index('if ($state -ne "Running")'),
            "the serve re-point must run after the hub task is verified up")

    def test_occupied_443_refusal_guard(self):
        src = _src(PS1)
        self.assertIn(
            "serve status", src,
            "the installer never inspects current serve state")
        self.assertRegex(
            src, r"REFUSED: tailscale serve already has a handler",
            "the occupied-:443 refusal is missing")
        self.assertRegex(
            src, r'Write-Error \("REFUSED: tailscale serve already has a handler',
            "the :443 refusal must be a TERMINATING Write-Error, not an "
            "advisory Write-Warning (review round 4)")
        self.assertRegex(
            src, r"Write-Error \(\"REFUSED: cannot verify tailscale serve occupancy",
            "the fail-closed status refusal must be a TERMINATING Write-Error")
        self.assertIn(
            "if ($cls.Occupier -and -not $cls.Ours) {", src,
            "the ps1 occupancy guard condition is missing or altered "
            "(review round 4)")
        self.assertIn(
            "-TargetPort $HttpsPort", src,
            "the classification must be scoped to the target serve port")
        self.assertRegex(src, r"-HttpsPort <n>",
                         "the refusal must name the -HttpsPort escape")
        self.assertIn(
            "REFUSED: cannot verify tailscale serve occupancy", src,
            "an unreadable status must FAIL CLOSED (FB-2)")

    def test_ours_regex_digit_boundary_is_pinned(self):
        """The ours-regex digit boundary is pinned at the PATTERN level: a
        mutant that drops the sh digit class or inverts the ps1 boundary
        changes this text and goes red (critic round)."""
        sh_src = _src(CLS_SH)
        self.assertIn(
            'proxy http://127' + chr(92) + '.0' + chr(92) + '.0' + chr(92)
            + '.1:$PORT([^0-9]|' + chr(92) + '$)', sh_src,
            "the sh ours pattern lost its digit boundary")
        lib_src = _src(LIB_PS1)
        self.assertIn(
            'proxy http://127' + chr(92) + '.0' + chr(92) + '.0' + chr(92)
            + '.1:$OurPort(' + chr(92) + 'D|$)', lib_src,
            "the ps1 ours pattern lost its digit boundary")

    def test_detection_is_line_scoped_and_port_aware(self):
        """BEHAVIORAL ORACLE — drives the shipped classifiers (sh via bash,\n        ps1 via powershell) through the shared status matrix."""
        found = _drivers()
        if not found:
            self.skipTest("no bash or pwsh/powershell runtime available")
        for name, drive in found:
            with self.subTest(driver=name):
                for case_name, port, target, status, want_occ, want_ours in CASES:
                    got_occ, got_ours = drive(port, target, status)
                    self.assertEqual(
                        (got_occ, got_ours), (want_occ, want_ours),
                        f"[{name}] {case_name}: want=({want_occ},{want_ours}) "
                        f"got=({got_occ},{got_ours})")


class FleetInstaller443ShTest(unittest.TestCase):
    """AC2 — the Linux installer applies the same refusal with the same\n    ordering and fail-closed status contract."""

    def test_https_port_arg_and_guard(self):
        src = _src(SH)
        self.assertIn(
            'HTTPS_PORT="${5:-443}"', src,
            "the HTTPS_PORT positional parameter is missing")
        self.assertRegex(
            src, r"--https=\"\$HTTPS_PORT\"",
            "the serve call still hardcodes 443")
        self.assertRegex(
            src, r"REFUSED: tailscale serve already has a handler",
            "the occupied-:443 refusal is missing")
        self.assertRegex(
            src, r"HTTPS_PORT=<n>",
            "the refusal must name the HTTPS_PORT escape")
        # FB-6: numeric normalization - a leading-zero port (0443) is
        # compared against the refusal by value, not text.
        self.assertIn('PORT=$((10#$PORT))', src)
        self.assertIn('HTTPS_PORT=$((10#$HTTPS_PORT))', src)
        # FB-7: the HTTPS_PORT validation case bounds both ends.
        self.assertIn('case "$HTTPS_PORT" in', src)
        self.assertIn('*[!0-9]*', src)
        self.assertIn('-le 65535', src)
        # FB-2: the status read is fail-closed; the occupancy refusal block
        # itself terminates with exit 2 (span-scoped - "exit 2" occurs
        # elsewhere in the file, so an unscoped pin is vacuous; round 2).
        self.assertIn("cannot verify tailscale serve occupancy", src)
        self.assertRegex(
            src,
            r"REFUSED: tailscale serve already has a handler"
            r"[\s\S]{0,450}?exit 2",
            "the occupancy refusal must terminate with exit 2 inside its "
            "own block")
        self.assertIn(
            "systemctl --no-pager --lines=5 status", src,
            "the sh installer never verifies the hub service came up")
        # Review round 4: the guard CONDITION itself is pinned - a mutant
        # that no-ops the occupancy gate (`if false; then`) must go red.
        self.assertIn(
            'if [ "$OCCUPIER" = "1" ] && [ "$OURS" = "0" ]; then', src,
            "the sh occupancy guard condition is missing or altered")
        self.assertIn(
            '[01]" "[01]) : ;;', src,
            "the sh classifier-output parse gate is missing (fail-open "
            "round: a malformed-but-exit-0 result must refuse)")

    def test_serve_call_uses_the_parameter(self):
        src = _src(SH)
        self.assertRegex(
            src, r"--https=\"\$HTTPS_PORT\"",
            "the serve call still hardcodes 443")
        self.assertNotRegex(
            src, r"--https=443(?!\s*http)",
            "a hardcoded --https=443 remains somewhere in the serve path")

    def test_guard_precedes_every_host_side_effect(self):
        """FB-5 for the sh installer: the preflight precedes the env-file\n        write, the unit write, and the service restart."""
        src = _src(SH)
        guard = src.index("serve status")
        self.assertLess(guard, src.index('cat > "$ENV_FILE"'))
        self.assertLess(guard, src.index('cat > "$UNIT"'))
        self.assertLess(guard, src.index("systemctl restart"))
        self.assertGreater(
            src.index("tailscale serve --bg"),
            src.index("systemctl --no-pager --lines=5 status"),
            "the serve re-point must run after the hub service is verified up")

    def test_detection_is_line_scoped_and_port_aware(self):
        """Same shipped-classifier behavioral oracle, sh-specific class (the\n        bash driver executes the shipped classifier file directly)."""
        found = [d for d in _drivers() if d[0].startswith("bash")]
        if not found:
            self.skipTest("no usable bash runtime available")
        for name, drive in found:
            with self.subTest(driver=name):
                for case_name, port, target, status, want_occ, want_ours in CASES:
                    got_occ, got_ours = drive(port, target, status)
                    self.assertEqual(
                        (got_occ, got_ours), (want_occ, want_ours),
                        f"[{name}] {case_name}: want=({want_occ},{want_ours}) "
                        f"got=({got_occ},{got_ours})")


class HttpsPortParameterTest(unittest.TestCase):
    """AC3 — the parameter is documented in FLEET.md, replacing the\n    hardcoded-443 prose."""

    def test_docs_name_the_parameter(self):
        src = _src(FLEET)
        self.assertRegex(
            src, r"`-HttpsPort <n>`|`install-hub\.ps1 -HttpsPort <n>`",
            "FLEET.md does not document the -HttpsPort parameter")
        self.assertIn(
            "HTTPS_PORT", src,
            "FLEET.md does not document the Linux HTTPS_PORT arg")
        self.assertNotIn(
            "`tailscale serve --bg --https=443 http://127.0.0.1:<port>`",
            src,
            "the hardcoded-443 command shape must not be documented as THE "
            "command")


if __name__ == "__main__":
    _loader = unittest.TestLoader()
    _argv = [a for a in sys.argv[1:] if not a.startswith("-")]
    if _argv:
        _suite = unittest.TestSuite()
        for _name in _argv:
            _suite.addTests(_loader.loadTestsFromName(_name, sys.modules[__name__]))
    else:
        _suite = _loader.loadTestsFromModule(sys.modules[__name__])
    _result = unittest.TextTestRunner(verbosity=2).run(_suite)
    print("CHECK-PASS" if _result.wasSuccessful() else "CHECK-FAIL")
    sys.exit(0 if _result.wasSuccessful() else 1)
