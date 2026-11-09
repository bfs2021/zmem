"""Sandbox end-to-end harness for the hub installers (issue #433).

tests/test_fleet_installer_443.py pins the serve-occupancy contract at the
SOURCE level (src.index() ordering); those gates are blind to a reorder
mutant that keeps every pinned substring but moves host mutations around
the serve-occupancy preflight or the hub health check. This file executes
the REAL scripts/fleet/install-hub.sh and scripts/fleet/install-hub.ps1
end-to-end in a sandbox with stubbed host commands, recording every host
side effect to a log, so control-flow regressions fail CI:

  * OccupancyRefusalTest - an occupied serve port is refused with the
    documented exit code and stderr anchor, with ZERO stub-recorded host
    side effects and no DATA_DIR created (issue #423's "leave the host
    exactly as found" contract, enforced behaviorally on both installers,
    default port 443 and an explicit port).
  * IdempotentRerunTest  - the healthy convergence path: registration +
    health check precede `serve --bg`, the serve argv carries the
    parameterized ports, and a re-run over the all-ours status converges
    with byte-identical serve argv and exactly one handler in the (stubbed)
    serve state.
  * ReorderMutantTest    - the standing guardrails (issue #433 DoD). TWO
    mutant families are generated at runtime from the LIVE installers and
    must both be detected: (a) data-dir mutations hoisted above the
    serve-occupancy preflight (refusal-leg predicate refusal_violations);
    (b) the serve call hoisted above the task/service health check
    (happy-path predicate happy_path_violations) - the exact mutant the
    issue DoD names ("serve call moved before the task-state check").

Sandboxing model - issue #433 FORBIDS real host mutations by these tests,
so every leg is hermetic:
  * sh:   the installer's code is sourced into a bash that has the stub dir
    FIRST on PATH (systemctl / tailscale / stat / id / chown / python3 /
    sleep are stubs that log and answer canned values). The MSYS runtime
    (Git Bash) force-prepends /usr/bin:/bin in every CHILD bash, so a plain
    `bash install-hub.sh` can never shadow coreutils names - hence the
    same-process `set -- args; . install-hub.sh` launcher, which preserves
    exit codes (`exit 2` inside a sourced script exits the shell). The real
    classifier runs because REPO_ROOT is the real repo
    (serve-status-classify.sh is invoked as
    `bash "$REPO_ROOT/scripts/fleet/serve-status-classify.sh"`).
    The sh HAPPY path (and the sh serve-before-health mutant) writes the
    HARDCODED unit file /etc/systemd/system/zmem-hub.service, so those legs
    run ONLY inside a private mount namespace:
      sudo -n unshare --mount --propagation private bash -c \
        'mount --bind <tmp_unitdir> /etc/systemd/system && <installer>'
    so the unit write lands in the temp dir while the REAL /etc stays
    byte-identical (snapshotted before/after and asserted; a change FAILS
    the test). Without passwordless sudo AND unshare those legs skipTest
    visibly. The refusal legs never reach the unit write and need no
    namespace.
  * ps1:  cmdlets can only be shadowed by same-name PowerShell functions,
    so a generated wrapper defines shadow functions (each appends a line to
    the side-effect log) and dot-sources the real installer with its real
    parameters. `tailscale` must stay a NATIVE PATH stub (tailscale.cmd ->
    a small python script) because the installer reads $LASTEXITCODE after
    it, which a function cannot set. serve-status-lib.ps1 is copied next to
    the wrapper (and next to any generated mutant) so the installer's
    $PSScriptRoot dot-source always resolves real classifier bytes. The ps1
    legs are WINDOWS-ONLY (os.name == "nt"): the only shadowable tailscale
    stub shipped here is a tailscale.cmd, and ubuntu-latest CI also has
    pwsh - without the gate the legs would run there and fail on the
    missing stub; the windows-latest CI leg keeps them non-vacuous.

Read-only calls (tailscale serve status, stat, id, Get-Acl,
Get-ScheduledTask) are allowed in the side-effect log; anything that
mutates the host (systemctl, chown, serve --bg, New-Item, Set-Content,
Register/Start-ScheduledTask) is a violation on a refusal leg.

Run: python tests/test_installer_e2e.py [TestClass[.method] ...]
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SH_INSTALLER = REPO_ROOT / "scripts" / "fleet" / "install-hub.sh"
PS1_INSTALLER = REPO_ROOT / "scripts" / "fleet" / "install-hub.ps1"
PS1_LIB = REPO_ROOT / "scripts" / "fleet" / "serve-status-lib.ps1"

LOG_VAR = "ZMEM_INSTALLER_E2E_LOG"
STATE_VAR = "ZMEM_INSTALLER_E2E_SERVE_STATE"
SERVICE_USER = "hubsvc"
TASK_NAME = "zmem-hub"
UNIT_PATH = "/etc/systemd/system/zmem-hub.service"
UNIT_NAME = "zmem-hub.service"

# Serve-status text shapes (same matrix family as the CASES table in
# tests/test_fleet_installer_443.py; each was verified against the REAL
# classifiers before being pinned here - operator/443 -> "1 0",
# operator/9443 -> "1 0", all-ours/9443 -> "1 1", "No serve config" -> "0 0").
STATUS_OPERATOR_443 = (
    "https://host.ts.net (tailnet only)\n"
    "|-- / proxy http://127.0.0.1:32400\n"
)
STATUS_OPERATOR_9443 = (
    "https://host.ts.net:9443 (tailnet only)\n"
    "|-- / proxy http://127.0.0.1:32400\n"
)
STATUS_NO_CONFIG = "No serve config\n"
EXPECTED_ALL_OURS_9443 = (
    "https://host.ts.net:9443 (tailnet only)\n"
    "|-- / proxy http://127.0.0.1:9001\n"
)
EXPECTED_SERVE_ARGV = "tailscale serve --bg --https=9443 http://127.0.0.1:9001"

SUBPROC_TIMEOUT_S = 180


# --------------------------------------------------------------------------
# runtime resolution (same convention as tests/test_fleet_installer_443.py,
# extended with probe-validated Git-for-Windows fallback candidates: on
# hosts where the WSL bash stub shadows Git Bash in PATH, shutil.which finds
# a bash that fails the probe, and the fallback still lets the legs RUN).
# --------------------------------------------------------------------------
def _resolve_bash() -> str | None:
    candidates: list[str] = []
    env_bash = os.environ.get("ZMEM_TEST_BASH")
    if env_bash:
        candidates.append(env_bash)
    which = shutil.which("bash")
    if which:
        candidates.append(which)
    for extra in (
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\usr\bin\bash.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\usr\bin\bash.exe"),
    ):
        if extra and extra not in candidates:
            candidates.append(extra)
    for cand in candidates:
        try:
            probe = subprocess.run(
                [cand, "-c", "echo bash-ok"], capture_output=True, text=True,
                timeout=15)
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and probe.stdout.strip() == "bash-ok":
            return cand
    return None


def _resolve_pwsh() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


_BASH = _resolve_bash()
_PWSH = _resolve_pwsh()


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------
def _fwd(path: Path) -> str:
    """Forward-slash form: install-hub.sh refuses backslashes in its args."""
    return str(path).replace("\\", "/")


def _write_exec(path: Path, text: str) -> None:
    path.write_bytes(text.replace("\r\n", "\n").encode("utf-8"))
    path.chmod(0o755)


def _read_log(path: Path) -> list[str]:
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln]


def _squash(text: str) -> str:
    """Collapse all whitespace runs to single spaces: PS 5.1 wraps
    Write-Error stderr at the console width, and the wrap position depends
    on the invoking script path's length, so a refusal anchor must be
    matched against whitespace-normalized text."""
    return " ".join(text.split())


def _mk_sandbox(prefix: str, case: unittest.TestCase) -> Path:
    root = Path(tempfile.mkdtemp(prefix=prefix))
    case.addCleanup(shutil.rmtree, root, True)
    return root


def _mk_tokens(root: Path) -> Path:
    tokens = root / "principal-map.json"
    tokens.write_text('{"principals": {}}', encoding="utf-8")
    return tokens


def _seed_state(root: Path, status: str) -> Path:
    state = root / "serve-state.txt"
    state.write_text(status, encoding="utf-8", newline="\n")
    return state


def _require_ps1(case: unittest.TestCase) -> None:
    """ps1 installer legs are Windows-only: the harness ships only a
    tailscale.cmd native stub, and ubuntu-latest CI also has pwsh, so
    without this gate the legs would run there and fail on the missing
    stub. The windows-latest CI leg keeps them non-vacuous."""
    if os.name != "nt":
        case.skipTest("ps1 installer legs are Windows-only; covered by the "
                      "windows-latest CI leg")
    if _PWSH is None:
        case.skipTest("no pwsh/powershell runtime available")


SH_MUTATOR_FIRST_TOKENS = {"systemctl", "chown", "python3"}
PS1_MUTATOR_FIRST_TOKENS = {
    "New-Item", "Set-Content", "Register-ScheduledTask", "Start-ScheduledTask",
}


def refusal_violations(flavour: str, log_lines: list[str],
                       data_dir: Path) -> list[str]:
    """Refusal-leg predicate (shared by OccupancyRefusalTest and the
    data-dir reorder-mutant tests): every recorded host MUTATION before the
    refusal (and DATA_DIR existing on disk) is a violation. Read-only calls
    (tailscale serve status, stat, id, Get-Acl, Get-ScheduledTask, the
    task-object constructors) are allowed."""
    mutators = (SH_MUTATOR_FIRST_TOKENS if flavour == "sh"
                else PS1_MUTATOR_FIRST_TOKENS)
    violations: list[str] = []
    for line in log_lines:
        first = line.split(" ", 1)[0]
        if first in mutators:
            violations.append(f"host mutation executed before refusal: {line}")
        if line.startswith("tailscale serve --bg"):
            violations.append(f"serve re-point executed before refusal: {line}")
    if data_dir.exists():
        violations.append(
            f"DATA_DIR was created before refusal: {data_dir}")
    return violations


def happy_path_violations(flavour: str, log_lines: list[str]) -> list[str]:
    """Happy-path ordering predicate (shared by IdempotentRerunTest on the
    REAL installers - must be [] - and by the serve-before-health reorder
    mutants - must be non-empty): every `serve --bg` re-point must be
    preceded by its run's task/service registration+start AND health check,
    so serve is never configured against a hub that is not verified up
    (install-hub.sh lines 147-158 / install-hub.ps1 lines 130-143 put the
    serve call strictly after the health check)."""
    violations: list[str] = []
    seen_reg = 0
    seen_start = 0
    seen_health = 0
    seen_serve = 0
    for line in log_lines:
        if line.startswith("tailscale serve --bg"):
            seen_serve += 1
            if seen_reg < seen_serve or seen_start < seen_serve:
                violations.append(
                    f"serve re-point #{seen_serve} executed before "
                    f"registration+start: {line}")
            if seen_health < seen_serve:
                violations.append(
                    f"serve re-point #{seen_serve} executed before the hub "
                    f"health check: {line}")
            continue
        if flavour == "sh":
            if line == "systemctl restart zmem-hub.service":
                seen_reg += 1
                seen_start += 1
            elif line == ("systemctl --no-pager --lines=5 status "
                          "zmem-hub.service"):
                seen_health += 1
        else:
            if line.startswith("Register-ScheduledTask"):
                seen_reg += 1
            elif line.startswith("Start-ScheduledTask"):
                seen_start += 1
            elif line.startswith("Get-ScheduledTask"):
                seen_health += 1
    return violations


# --------------------------------------------------------------------------
# sh sandbox
# --------------------------------------------------------------------------
def _make_sh_stubs(stub_dir: Path) -> None:
    tmpl = "#!/usr/bin/env bash\n" + 'printf \'%s\\n\' "__NAME__ $*" >> "$__LOGVAR__"\n'

    def stub(name: str, body: str) -> None:
        _write_exec(stub_dir / name,
                    tmpl.replace("__NAME__", name).replace("__LOGVAR__", LOG_VAR)
                    + body)

    stub("systemctl", '''
if [ "${1:-}" = "--no-pager" ]; then
    printf '%s\\n' "zmem-hub.service - zmem fleet hub" "   Active: active (running)"
fi
exit 0
''')
    stub("stat", '''
case "${2:-}" in
    %a) printf '600\\n' ;;
    %U) printf '__SVC__\\n' ;;
esac
exit 0
'''.replace("__SVC__", SERVICE_USER))
    stub("id", "printf '__SVC__\\n'\nexit 0\n".replace("__SVC__", SERVICE_USER))
    stub("chown", "exit 0\n")
    stub("python3", "exit 0\n")
    stub("sleep", "exit 0\n")
    # The STATEFUL tailscale stub: `serve --bg` persists the all-ours config
    # for the claimed port so the next `serve status` prints it (the
    # idempotent-rerun shape).
    stub("tailscale", '''
if [ "${1:-}" = "serve" ] && [ "${2:-}" = "status" ]; then
    cat "$''' + STATE_VAR + '''"
    exit 0
fi
if [ "${1:-}" = "serve" ] && [ "${2:-}" = "--bg" ]; then
    https_port=""; target=""
    for a in "$@"; do
        case "$a" in
            --https=*) https_port="${a#--https=}" ;;
            http://*) target="$a" ;;
        esac
    done
    printf 'https://host.ts.net:%s (tailnet only)\\n|-- / proxy %s\\n' \\
        "$https_port" "$target" > "$''' + STATE_VAR + '''"
    exit 0
fi
exit 1
''')


def _posix_dir(bash: str, path: Path) -> str:
    if os.name != "nt":
        return str(path)
    proc = subprocess.run(
        [bash, "-c", f'cygpath -u "{path}"'], capture_output=True, text=True,
        timeout=15)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(
            f"cygpath failed for {path}: rc={proc.returncode} "
            f"err={proc.stderr[-200:]!r}")
    return proc.stdout.strip()


def run_sh_installer(installer: Path, args: list[str], stub_dir: Path,
                     log: Path, state: Path):
    """Run an sh installer against the stub sandbox on a leg that never
    reaches the unit write (refusal legs and data-dir reorder mutants).

    The installer is SOURCED into a bash whose PATH starts with the stub dir
    (MSYS force-prepends /usr/bin:/bin in every child bash, so a plain
    child `bash install-hub.sh` could never shadow coreutils names on Git
    Bash). `exit N` inside the sourced script exits the shell with N, so
    the installer's exit codes are preserved."""
    bash = _BASH
    assert bash is not None
    pdir = _posix_dir(bash, stub_dir)
    quoted = " ".join(shlex.quote(a) for a in args)
    inner = (
        f"export PATH={shlex.quote(pdir)}:\"$PATH\"\n"
        f"set -- {quoted}\n"
        f". {shlex.quote(_fwd(installer))}"
    )
    env = dict(os.environ)
    env.pop("SUDO_USER", None)  # SERVICE_USER must come from the id stub
    env[LOG_VAR] = str(log)
    env[STATE_VAR] = str(state)
    return subprocess.run(
        [bash, "-c", inner], capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
        timeout=SUBPROC_TIMEOUT_S)


def _sh_namespace_status() -> tuple[bool, str]:
    """(available, reason) for the private-mount-namespace sh happy path.
    Issue #433 forbids real host mutations, so the sh happy path (unit write
    to the hardcoded /etc/systemd/system/zmem-hub.service) only ever runs
    behind `sudo -n unshare --mount --propagation private` with a temp dir
    bind-mounted over /etc/systemd/system."""
    sudo = shutil.which("sudo")
    if not sudo:
        return False, "no sudo on PATH"
    probe = subprocess.run([sudo, "-n", "true"], capture_output=True,
                           text=True, timeout=30)
    if probe.returncode != 0:
        return False, f"`sudo -n true` failed (rc={probe.returncode})"
    if not shutil.which("unshare"):
        return False, "no unshare on PATH"
    probe = subprocess.run(
        [sudo, "-n", "unshare", "--mount", "--propagation", "private",
         "true"], capture_output=True, text=True, timeout=30)
    if probe.returncode != 0:
        return False, ("`sudo -n unshare --mount --propagation private true` "
                       f"failed (rc={probe.returncode})")
    return True, ""


def run_sh_installer_namespaced(installer: Path, args: list[str],
                                stub_dir: Path, log: Path, state: Path,
                                unit_dir: Path):
    """Run an sh installer that performs the unit write, inside a PRIVATE
    mount namespace so the write lands in the temp `unit_dir`:

        sudo -n unshare --mount --propagation private <bash> -c '
            mount --bind <unit_dir> /etc/systemd/system || exit 99
            export PATH=<stubs>:$PATH SUDO_USER=... LOG=... STATE=...
            set -- <args>; . <installer>'

    (equivalent to the `... && exec env ... bash -c <launcher>` shape - the
    env exports and the same-process sourced launcher are folded into the
    one namespace script). The namespace is private, so the bind disappears
    when this command exits; the caller still asserts the REAL unit file is
    unchanged and that the write landed in unit_dir."""
    bash = _BASH
    assert bash is not None
    stub_posix = str(stub_dir)  # namespace legs only run where paths are POSIX
    unit_posix = str(unit_dir)

    def q(text: str) -> str:
        return shlex.quote(text)

    outer = "\n".join([
        f"mount --bind {q(unit_posix)} /etc/systemd/system || exit 99",
        f'export PATH={q(stub_posix)}:"$PATH"',
        f"export SUDO_USER={SERVICE_USER} "
        f"{LOG_VAR}={q(str(log))} {STATE_VAR}={q(str(state))}",
        f"set -- {' '.join(q(a) for a in args)}",
        f". {q(str(installer))}",
    ])
    return subprocess.run(
        ["sudo", "-n", "unshare", "--mount", "--propagation", "private",
         bash, "-c", outer],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=SUBPROC_TIMEOUT_S)


def _sh_unit_snapshot() -> tuple[bool, str | None]:
    """(exists, sha256-or-None) of the REAL unit file, read via sudo."""
    sudo = shutil.which("sudo")
    if not sudo:
        raise RuntimeError("_sh_unit_snapshot called without sudo available")
    exists_proc = subprocess.run(
        [sudo, "-n", "test", "-e", UNIT_PATH], capture_output=True,
        text=True, timeout=30)
    exists = exists_proc.returncode == 0
    sha: str | None = None
    if exists:
        sha_proc = subprocess.run(
            [sudo, "-n", "sha256sum", UNIT_PATH], capture_output=True,
            text=True, timeout=30)
        if sha_proc.returncode != 0 or not sha_proc.stdout.split():
            raise RuntimeError(
                f"sha256sum of the real {UNIT_PATH} failed "
                f"rc={sha_proc.returncode} err={sha_proc.stderr[-200:]!r}")
        sha = sha_proc.stdout.split()[0]
    return (exists, sha)


# --------------------------------------------------------------------------
# ps1 sandbox
# --------------------------------------------------------------------------
# Built via __PLACEHOLDER__ replacement (not str.format) to avoid escaping
# the PowerShell braces.
_PS1_WRAPPER = r"""
$ErrorActionPreference = 'Stop'
$__e2eLog = $env:__LOGVAR__
function __E2ELog([string]$Line) {
    [System.IO.File]::AppendAllText($__e2eLog, $Line + "`n")
}
function Get-Acl {
    param([Parameter(Mandatory=$true)][string]$Path)
    __E2ELog "Get-Acl $Path"
    [pscustomobject]@{ Access = @(); Owner = '__OWNER__' }
}
function New-Item {
    param([string]$ItemType, [string[]]$Path, [switch]$Force)
    __E2ELog ("New-Item -ItemType " + $ItemType + " -Path " + ($Path -join ','))
}
function Set-Content {
    param([Parameter(ValueFromPipeline=$true)][AllowEmptyString()][string]$InputObject, [string]$Path, [string]$Encoding)
    process { __E2ELog ("Set-Content -Path " + $Path) }
}
function New-ScheduledTaskAction {
    param([string]$Execute, [string]$Id)
    __E2ELog ("New-ScheduledTaskAction -Execute " + $Execute)
    [pscustomobject]@{ Dummy = 'action' }
}
function New-ScheduledTaskTrigger {
    param([switch]$AtStartup)
    __E2ELog "New-ScheduledTaskTrigger -AtStartup"
    [pscustomobject]@{ Dummy = 'trigger' }
}
function New-ScheduledTaskPrincipal {
    param([string]$UserId, [string]$LogonType)
    __E2ELog ("New-ScheduledTaskPrincipal -UserId " + $UserId + " -LogonType " + $LogonType)
    [pscustomobject]@{ Dummy = 'principal' }
}
function New-ScheduledTaskSettingsSet {
    param([int]$RestartCount, $RestartInterval, [switch]$AllowStartIfOnBatteries, [switch]$DontStopIfGoingOnBatteries, $ExecutionTimeLimit)
    __E2ELog "New-ScheduledTaskSettingsSet"
    [pscustomobject]@{ Dummy = 'settings' }
}
function Register-ScheduledTask {
    param([string]$TaskName, $Action, $Trigger, $Principal, $Settings, [switch]$Force)
    __E2ELog ("Register-ScheduledTask -TaskName " + $TaskName)
}
function Start-ScheduledTask {
    param([string]$TaskName)
    __E2ELog ("Start-ScheduledTask -TaskName " + $TaskName)
}
function Get-ScheduledTask {
    param([string]$TaskName)
    __E2ELog ("Get-ScheduledTask -TaskName " + $TaskName)
    [pscustomobject]@{ State = 'Running' }
}
function Start-Sleep {
    param([int]$Seconds)
    __E2ELog ("Start-Sleep -Seconds " + $Seconds)
}
. '__INSTALLER__' -RepoRoot '__REPO__' -DataDir '__DATA__' -TokensFile '__TOKENS__' -Port __PORT__ -TaskName '__TASK__'__HTTPSPORT__
"""

_TAILSCALE_CMD = (
    '@python -X utf8 "%~dp0tailscale_stub.py" %*\r\n'
    '@exit /b %ERRORLEVEL%\r\n'
)

_TAILSCALE_PY = '''
import os
import sys

LOG = os.environ["__LOGVAR__"]
STATE = os.environ["__STATEVAR__"]
argv = sys.argv[1:]
with open(LOG, "a", encoding="utf-8") as f:
    f.write("tailscale " + " ".join(argv) + "\\n")
if argv[:2] == ["serve", "status"]:
    with open(STATE, encoding="utf-8") as f:
        sys.stdout.write(f.read())
    sys.exit(0)
if argv[:2] == ["serve", "--bg"]:
    https_port = next(
        (a.split("=", 1)[1] for a in argv if a.startswith("--https=")), "443")
    target = next((a for a in argv if a.startswith("http://")), "")
    with open(STATE, "w", encoding="utf-8", newline="\\n") as f:
        f.write("https://host.ts.net:%s (tailnet only)\\n"
                "|-- / proxy %s\\n" % (https_port, target))
    sys.exit(0)
sys.exit(1)
'''


def _ps_sq(text: str) -> str:
    return text.replace("'", "''")


def _make_ps1_sandbox(root: Path) -> Path:
    stub_dir = root / "stubs"
    stub_dir.mkdir()
    (stub_dir / "tailscale.cmd").write_bytes(
        _TAILSCALE_CMD.encode("ascii"))
    (stub_dir / "tailscale_stub.py").write_text(
        _TAILSCALE_PY.replace("__LOGVAR__", LOG_VAR).replace(
            "__STATEVAR__", STATE_VAR), encoding="utf-8")
    # Belt-and-braces: the installer dot-sources serve-status-lib.ps1 via
    # $PSScriptRoot; when the dot-sourced installer is a generated mutant in
    # this temp dir, this copy keeps the REAL classifier bytes resolvable.
    shutil.copyfile(PS1_LIB, root / "serve-status-lib.ps1")
    shutil.copyfile(PS1_LIB, stub_dir / "serve-status-lib.ps1")
    return stub_dir


def run_ps1_installer(installer: Path, data_dir: Path, tokens: Path,
                      port: int, https_port: int | None, log: Path,
                      state: Path, stub_dir: Path, wrapper: Path):
    assert _PWSH is not None
    wrapper.write_bytes(
        _PS1_WRAPPER
        .replace("__LOGVAR__", LOG_VAR)
        .replace("__OWNER__", "E2E-" + SERVICE_USER.upper())
        .replace("__INSTALLER__", _ps_sq(str(installer)))
        .replace("__REPO__", _ps_sq(str(REPO_ROOT)))
        .replace("__DATA__", _ps_sq(str(data_dir)))
        .replace("__TOKENS__", _ps_sq(str(tokens)))
        .replace("__PORT__", str(port))
        .replace("__TASK__", TASK_NAME)
        .replace(
            "__HTTPSPORT__",
            "" if https_port is None else f" -HttpsPort {https_port}")
        .replace("\r\n", "\n").encode("utf-8"))
    env = dict(os.environ)
    env["PATH"] = str(stub_dir) + os.pathsep + env.get("PATH", "")
    env[LOG_VAR] = str(log)
    env[STATE_VAR] = str(state)
    return subprocess.run(
        [_PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(wrapper)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=SUBPROC_TIMEOUT_S)


# --------------------------------------------------------------------------
# reorder-mutant generation (at runtime, from the LIVE installers)
# --------------------------------------------------------------------------
# Data-dir family: mutations hoisted above the serve-occupancy preflight.
SH_ANCHOR_MKDIR = 'mkdir -p "$DATA_DIR"'
SH_ANCHOR_CHOWN = 'chown "$SERVICE_USER" "$DATA_DIR"'
SH_ANCHOR_SERVE_READ = 'SERVE_STATUS="$(tailscale serve status'
PS1_ANCHOR_NEWITEM = "New-Item -ItemType Directory -Force -Path $DataDir | Out-Null"
PS1_ANCHOR_PREFLIGHT = "$serveFound = [bool](Get-Command tailscale -ErrorAction SilentlyContinue)"

# Serve-before-health family (the issue #433 DoD mutant: "serve call moved
# before the task-state check").
SH_ANCHOR_SERVE_BG = '    tailscale serve --bg --https="$HTTPS_PORT" "http://127.0.0.1:$PORT"'
SH_ANCHOR_HEALTH = "if ! systemctl --no-pager --lines=5 status zmem-hub.service"
PS1_ANCHOR_SERVE_BG = '    tailscale serve --bg --https=$HttpsPort "http://127.0.0.1:$Port"'
PS1_ANCHOR_HEALTH = "$state = (Get-ScheduledTask -TaskName $TaskName).State"


def _hoist_lines(text: str, move_exact: list[str], before_prefix: str,
                 case: unittest.TestCase, flavour: str) -> bytes:
    """Remove the lines named by `move_exact` (each must occur EXACTLY once)
    and reinsert them immediately before the line starting with
    `before_prefix` (also exactly once). A missing/duplicated anchor FAILS
    the test: the reorder guardrail must be re-anchored, not silently
    disarmed."""
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)

    def exact_hits(anchor: str) -> list[int]:
        return [i for i, ln in enumerate(lines)
                if ln.rstrip("\r\n") == anchor]

    moved: list[str] = []
    for anchor in move_exact:
        hits = exact_hits(anchor)
        if len(hits) != 1:
            case.fail(
                f"the {flavour} reorder guardrail must be re-anchored: "
                f"anchor {anchor!r} occurs {len(hits)} times (want exactly 1) "
                f"in the live installer - update the mutant generator in "
                f"tests/test_installer_e2e.py")
        moved.append(lines[hits[0]])
    pre_hits = [i for i, ln in enumerate(lines)
                if ln.lstrip().startswith(before_prefix)]
    if len(pre_hits) != 1:
        case.fail(
            f"the {flavour} reorder guardrail must be re-anchored: "
            f"insertion anchor {before_prefix!r} occurs {len(pre_hits)} times "
            f"(want exactly 1) in the live installer - update the mutant "
            f"generator in tests/test_installer_e2e.py")

    kept = [ln for ln in lines if ln.rstrip("\r\n") not in move_exact]
    insert_at = next(i for i, ln in enumerate(kept)
                     if ln.lstrip().startswith(before_prefix))
    kept[insert_at:insert_at] = [m if m.endswith(("\n", "\r\n"))
                                 else m + eol for m in moved]
    return "".join(kept).encode("utf-8")


# --------------------------------------------------------------------------
# shared namespace-leg scaffolding (sh happy path + sh serve mutant)
# --------------------------------------------------------------------------
def _skip_unless_namespace(case: unittest.TestCase) -> None:
    available, reason = _sh_namespace_status()
    if not available:
        case.skipTest(
            "sh happy-path legs run only inside a private mount namespace "
            "(issue #433 forbids real host mutations: the unit write to the "
            "hardcoded /etc/systemd/system/zmem-hub.service is bind-mounted "
            f"onto a temp dir) - unavailable on this host: {reason}")


def _setup_namespaced_leg(case: unittest.TestCase, prefix: str):
    """Gate + sandbox + real-unit snapshot + root-file cleanup for an sh leg
    that performs the unit write. Returns (root, stubs, log, state, tokens,
    data_dir, unit_dir)."""
    _skip_unless_namespace(case)
    root = _mk_sandbox(prefix, case)
    stubs = root / "stubs"
    stubs.mkdir()
    _make_sh_stubs(stubs)
    unit_dir = root / "unitdir"
    unit_dir.mkdir()

    def sudo_rm_rf_checked(path: Path) -> None:
        proc = subprocess.run(
            ["sudo", "-n", "rm", "-rf", str(path)], capture_output=True,
            text=True, timeout=60)
        if proc.returncode != 0:
            raise case.failureException(
                f"sudo rm -rf cleanup of root-owned {path} failed "
                f"rc={proc.returncode}: {proc.stderr[-300:]}")

    # LIFO: root-owned files are removed (checked, failing loudly) before
    # the runner-owned rmtree sweep.
    case.addCleanup(sudo_rm_rf_checked, root)
    # Assert the REAL unit file never changed - runs even if the test body
    # already failed (a change is a host mutation and must FAIL, not skip).
    snapshot_before = _sh_unit_snapshot()

    def assert_real_unit_unchanged() -> None:
        snapshot_after = _sh_unit_snapshot()
        if snapshot_after != snapshot_before:
            raise case.failureException(
                f"the REAL {UNIT_PATH} changed during the test "
                f"(before={snapshot_before}, after={snapshot_after}) - "
                f"issue #433 forbids real host mutations; the namespace "
                f"sandbox failed")

    case.addCleanup(assert_real_unit_unchanged)
    log = root / "log.txt"
    state = _seed_state(root, STATUS_NO_CONFIG)
    tokens = _mk_tokens(root)
    data_dir = root / "hubdata"
    return root, stubs, log, state, tokens, data_dir, unit_dir


# --------------------------------------------------------------------------
# AC1: occupancy refusal leaves the host exactly as found
# --------------------------------------------------------------------------
class OccupancyRefusalTest(unittest.TestCase):
    """An occupied serve port is refused (sh exit 2 / ps1 exit 1) with the
    stderr anchor naming the port, and ZERO host side effects are recorded:
    no systemctl call, no serve --bg, no chown, no task registration - and
    DATA_DIR (passed non-existent) stays non-existent."""

    def _run_sh_leg(self, status: str, extra_args: list[str],
                    anchor_port: int) -> tuple:
        root = _mk_sandbox("e2e-sh-refusal-", self)
        stubs = root / "stubs"
        stubs.mkdir()
        _make_sh_stubs(stubs)
        log = root / "log.txt"
        state = _seed_state(root, status)
        tokens = _mk_tokens(root)
        data_dir = root / "hubdata"
        proc = run_sh_installer(
            SH_INSTALLER,
            [_fwd(REPO_ROOT), _fwd(data_dir), _fwd(tokens)] + extra_args,
            stubs, log, state)
        return proc, log, data_dir, anchor_port

    def _assert_sh_refusal(self, proc, log, data_dir, anchor_port) -> None:
        self.assertEqual(
            proc.returncode, 2,
            f"sh installer must refuse with exit 2 (stdout={proc.stdout!r} "
            f"stderr={proc.stderr!r})")
        anchor = (f"install-hub: REFUSED: tailscale serve already has a "
                  f"handler on :{anchor_port}")
        self.assertIn(anchor, proc.stderr,
                      "the sh refusal stderr anchor naming the port is missing")
        self.assertTrue(log.exists(),
                        "the side-effect log was never written - the stub "
                        "environment plumbing is broken")
        violations = refusal_violations("sh", _read_log(log), data_dir)
        self.assertEqual(
            violations, [],
            "the sh installer performed host mutations before the refusal")

    def _run_ps1_leg(self, status: str, https_port: int | None,
                     anchor_port: int) -> tuple:
        root = _mk_sandbox("e2e-ps1-refusal-", self)
        stubs = _make_ps1_sandbox(root)
        log = root / "log.txt"
        state = _seed_state(root, status)
        tokens = _mk_tokens(root)
        data_dir = root / "hubdata"
        proc = run_ps1_installer(
            PS1_INSTALLER, data_dir, tokens, 8765, https_port, log, state,
            stubs, root / "wrap.ps1")
        return proc, log, data_dir, anchor_port

    def _assert_ps1_refusal(self, proc, log, data_dir, anchor_port) -> None:
        self.assertEqual(
            proc.returncode, 1,
            f"ps1 installer must refuse with exit 1 (stdout={proc.stdout!r} "
            f"stderr={proc.stderr!r})")
        anchor = f"REFUSED: tailscale serve already has a handler on :{anchor_port}"
        self.assertIn(anchor, _squash(proc.stderr),
                      "the ps1 refusal stderr anchor naming the port is missing")
        self.assertTrue(log.exists(),
                        "the side-effect log was never written - the stub "
                        "environment plumbing is broken")
        violations = refusal_violations("ps1", _read_log(log), data_dir)
        self.assertEqual(
            violations, [],
            "the ps1 installer performed host mutations before the refusal")

    def test_sh_refuses_occupied_default_443_before_any_side_effect(self):
        if _BASH is None:
            self.skipTest("no usable bash runtime available "
                          "(ZMEM_TEST_BASH unset and the bash probe failed)")
        proc, log, data_dir, port = self._run_sh_leg(
            STATUS_OPERATOR_443, ["8765"], 443)
        self._assert_sh_refusal(proc, log, data_dir, port)

    def test_sh_refuses_occupied_explicit_9443_before_any_side_effect(self):
        if _BASH is None:
            self.skipTest("no usable bash runtime available "
                          "(ZMEM_TEST_BASH unset and the bash probe failed)")
        proc, log, data_dir, port = self._run_sh_leg(
            STATUS_OPERATOR_9443, ["8765", "9443"], 9443)
        self._assert_sh_refusal(proc, log, data_dir, port)

    def test_ps1_refuses_occupied_default_443_before_any_side_effect(self):
        _require_ps1(self)
        proc, log, data_dir, port = self._run_ps1_leg(
            STATUS_OPERATOR_443, None, 443)
        self._assert_ps1_refusal(proc, log, data_dir, port)

    def test_ps1_refuses_occupied_explicit_9443_before_any_side_effect(self):
        _require_ps1(self)
        proc, log, data_dir, port = self._run_ps1_leg(
            STATUS_OPERATOR_9443, 9443, 9443)
        self._assert_ps1_refusal(proc, log, data_dir, port)


# --------------------------------------------------------------------------
# AC2: healthy install + idempotent re-run convergence
# --------------------------------------------------------------------------
class IdempotentRerunTest(unittest.TestCase):
    """The convergence contract on the parameterized ports (PORT=9001,
    HTTPS_PORT=9443), pinned against the SHIPPED semantics:

    NOTE (deviation from the issue text, which said "an idempotent re-run
    records no second serve call"): the shipped installers re-execute
    `tailscale serve --bg` UNCONDITIONALLY on every run - install-hub.sh
    lines 157-159 (the `if command -v tailscale` block runs serve and
    `serve status` on every invocation) and install-hub.ps1 lines 140-146
    (same shape). The idempotency mechanism is the preflight's ours-bypass,
    not a skipped re-point. These tests therefore pin the SHIPPED
    convergence semantics: the re-run must not be refused, registration
    repeats, the serve re-point repeats with byte-identical argv, and the
    (stubbed) serve state ends with exactly ONE handler for the target
    port. The ordering half is the SHARED happy_path_violations predicate,
    which ReorderMutantTest's serve-before-health mutants must trip."""

    def _assert_sh_convergence(self, procs, log: Path, state: Path) -> None:
        for i, proc in enumerate(procs, 1):
            self.assertEqual(
                proc.returncode, 0,
                f"sh installer run {i} failed (stdout={proc.stdout!r} "
                f"stderr={proc.stderr!r})")
            self.assertNotIn("REFUSED", proc.stderr,
                             f"sh run {i} was refused")
        lines = _read_log(log)
        self.assertEqual(
            happy_path_violations("sh", lines), [],
            "the real sh installer violated the happy-path ordering "
            "contract (registration/health before serve)")
        daemon = [ln for ln in lines if ln == "systemctl daemon-reload"]
        restart = [ln for ln in lines
                   if ln == "systemctl restart zmem-hub.service"]
        health = [ln for ln in lines
                  if ln == ("systemctl --no-pager --lines=5 status "
                            "zmem-hub.service")]
        serve = [ln for ln in lines
                 if ln.startswith("tailscale serve --bg")]
        self.assertEqual(len(daemon), 2, "daemon-reload must run once per run")
        self.assertEqual(len(restart), 2,
                         "the service registration+restart must repeat on re-run")
        self.assertEqual(len(health), 2, "the health check must run once per run")
        self.assertEqual(len(serve), 2,
                         "the shipped installers re-execute serve on re-run "
                         "(install-hub.sh lines 157-159)")
        self.assertEqual(
            serve[0], EXPECTED_SERVE_ARGV,
            "run 1 serve argv must carry the explicit parameterized ports")
        self.assertEqual(
            serve[0], serve[1],
            "the re-run serve argv must be byte-identical to run 1")
        self.assertEqual(
            state.read_text(encoding="utf-8"), EXPECTED_ALL_OURS_9443,
            "the stubbed serve state must hold exactly ONE handler for "
            "the target port after convergence")

    def test_sh_rerun_repoints_serve_idempotently(self):
        if _BASH is None:
            self.skipTest("no usable bash runtime available "
                          "(ZMEM_TEST_BASH unset and the bash probe failed)")
        # Issue #433 forbids real host mutations: the sh happy path's unit
        # write runs ONLY inside a private mount namespace with the temp
        # unit_dir bind-mounted over /etc/systemd/system (setup also
        # snapshots the real unit file and asserts it unchanged).
        (_root, stubs, log, state, tokens, data_dir,
         unit_dir) = _setup_namespaced_leg(self, "e2e-sh-rerun-")
        procs = []
        for _ in range(2):
            procs.append(run_sh_installer_namespaced(
                SH_INSTALLER,
                [str(REPO_ROOT), str(data_dir), str(tokens), "9001", "9443"],
                stubs, log, state, unit_dir))
        self._assert_sh_convergence(procs, log, state)
        self.assertTrue(
            (unit_dir / UNIT_NAME).exists(),
            "the unit write did not land in the bind-mounted temp dir - "
            "the namespace bind mount did not take effect")

    def _assert_ps1_convergence(self, procs, log: Path, state: Path) -> None:
        for i, proc in enumerate(procs, 1):
            self.assertEqual(
                proc.returncode, 0,
                f"ps1 installer run {i} failed (stdout={proc.stdout!r} "
                f"stderr={proc.stderr!r})")
            self.assertNotIn("REFUSED", proc.stderr,
                             f"ps1 run {i} was refused")
        lines = _read_log(log)
        self.assertEqual(
            happy_path_violations("ps1", lines), [],
            "the real ps1 installer violated the happy-path ordering "
            "contract (registration/health before serve)")
        register = [ln for ln in lines
                    if ln.startswith("Register-ScheduledTask")]
        start = [ln for ln in lines if ln.startswith("Start-ScheduledTask")]
        health = [ln for ln in lines if ln.startswith("Get-ScheduledTask")]
        serve = [ln for ln in lines
                 if ln.startswith("tailscale serve --bg")]
        self.assertEqual(len(register), 2,
                         "task registration must repeat on re-run")
        self.assertEqual(len(start), 2, "task start must repeat on re-run")
        self.assertEqual(len(health), 2, "the health check must run once per run")
        self.assertEqual(len(serve), 2,
                         "the shipped installer re-executes serve on re-run "
                         "(install-hub.ps1 lines 140-146)")
        self.assertEqual(
            serve[0], EXPECTED_SERVE_ARGV,
            "run 1 serve argv must carry the explicit parameterized ports")
        self.assertEqual(
            serve[0], serve[1],
            "the re-run serve argv must be byte-identical to run 1")
        self.assertEqual(
            state.read_text(encoding="utf-8"), EXPECTED_ALL_OURS_9443,
            "the stubbed serve state must hold exactly ONE handler for "
            "the target port after convergence")

    def test_ps1_rerun_repoints_serve_idempotently(self):
        _require_ps1(self)
        root = _mk_sandbox("e2e-ps1-rerun-", self)
        stubs = _make_ps1_sandbox(root)
        log = root / "log.txt"
        state = _seed_state(root, STATUS_NO_CONFIG)
        tokens = _mk_tokens(root)
        data_dir = root / "hubdata"
        procs = []
        for _ in range(2):
            procs.append(run_ps1_installer(
                PS1_INSTALLER, data_dir, tokens, 9001, 9443, log, state,
                stubs, root / "wrap.ps1"))
        self._assert_ps1_convergence(procs, log, state)


# --------------------------------------------------------------------------
# AC3: the standing reorder-mutant guardrails
# --------------------------------------------------------------------------
class ReorderMutantTest(unittest.TestCase):
    """Issue #433 DoD: reorder mutants demonstrated RED under the harness.
    The mutants are generated AT RUNTIME from the LIVE installer (never
    written into scripts/fleet/), and every anchor must occur EXACTLY once
    in the live source - a drifted anchor FAILS (the guardrail must be
    re-anchored), never silently skips.

    Family (a) - data-dir mutations hoisted above the serve-occupancy
    preflight (install-hub.sh lines 108-109: mkdir -p + chown;
    install-hub.ps1 line 101: New-Item). These mutants still refuse with
    the SAME exit code and stderr anchor - exactly why the source-index
    gates in test_fleet_installer_443.py cannot see them - but the
    refusal-leg predicate (refusal_violations, the same helper
    OccupancyRefusalTest asserts is empty) reports the pre-refusal side
    effects.

    Family (b) - the issue #433 DoD mutant verbatim: the serve call moved
    before the task-state check (install-hub.sh: the `tailscale serve --bg`
    line hoisted above the `if ! systemctl --no-pager ... status` health
    check; install-hub.ps1: the `tailscale serve --bg` invocation hoisted
    above the `$state = (Get-ScheduledTask ...).State` check). On an
    unoccupied status with a healthy hub these mutants EXIT 0 with no
    refusal - a silent reorder - and only the shared happy-path predicate
    (happy_path_violations, the same helper IdempotentRerunTest asserts is
    empty) catches them."""

    # ---- family (a): data-dir hoist above the occupancy preflight --------

    def test_sh_reorder_mutant_is_detected(self):
        if _BASH is None:
            self.skipTest("no usable bash runtime available "
                          "(ZMEM_TEST_BASH unset and the bash probe failed)")
        root = _mk_sandbox("e2e-sh-mutant-", self)
        mutant = root / "install-hub.sh"
        mutant.write_bytes(_hoist_lines(
            SH_INSTALLER.read_text(encoding="utf-8"),
            [SH_ANCHOR_MKDIR, SH_ANCHOR_CHOWN], SH_ANCHOR_SERVE_READ,
            self, "sh"))
        mutant.chmod(0o755)
        stubs = root / "stubs"
        stubs.mkdir()
        _make_sh_stubs(stubs)
        log = root / "log.txt"
        state = _seed_state(root, STATUS_OPERATOR_443)
        tokens = _mk_tokens(root)
        data_dir = root / "hubdata"
        # REPO_ROOT stays the REAL repo so the REAL classifier runs inside
        # the mutant; only the installer bytes are mutant.
        proc = run_sh_installer(
            mutant, [_fwd(REPO_ROOT), _fwd(data_dir), _fwd(tokens), "8765"],
            stubs, log, state)
        # The mutant still refuses identically - proving the exit code and
        # stderr anchor alone cannot discriminate the reorder...
        self.assertEqual(proc.returncode, 2,
                         "the reorder mutant must still reach the refusal")
        self.assertIn(
            "install-hub: REFUSED: tailscale serve already has a handler on :443",
            proc.stderr)
        # ...only the side-effect predicate can (and must).
        violations = refusal_violations("sh", _read_log(log), data_dir)
        self.assertTrue(
            violations,
            "the reorder mutant (mkdir/chown hoisted above the serve "
            "preflight) was NOT detected - the guardrail predicate is "
            "vacuous; re-anchor it on the live installer")
        self.assertTrue(
            any("chown" in v for v in violations),
            f"expected the hoisted chown among the violations: {violations}")
        self.assertTrue(data_dir.exists(),
                        "the hoisted mkdir must have created DATA_DIR")

    def test_ps1_reorder_mutant_is_detected(self):
        _require_ps1(self)
        root = _mk_sandbox("e2e-ps1-mutant-", self)
        stubs = _make_ps1_sandbox(root)
        mutant = root / "install-hub.ps1"
        # utf-8-sig read strips the installer's BOM; re-emit it so the
        # mutant keeps the original's exact encoding shape.
        mutant.write_bytes(
            b"\xef\xbb\xbf" + _hoist_lines(
                PS1_INSTALLER.read_text(encoding="utf-8-sig"),
                [PS1_ANCHOR_NEWITEM], PS1_ANCHOR_PREFLIGHT, self, "ps1"))
        # serve-status-lib.ps1 is already copied next to the mutant by
        # _make_ps1_sandbox, so the mutant's $PSScriptRoot dot-source still
        # resolves the REAL classifier bytes.
        log = root / "log.txt"
        state = _seed_state(root, STATUS_OPERATOR_443)
        tokens = _mk_tokens(root)
        data_dir = root / "hubdata"
        proc = run_ps1_installer(
            mutant, data_dir, tokens, 8765, None, log, state, stubs,
            root / "wrap.ps1")
        self.assertEqual(proc.returncode, 1,
                         "the reorder mutant must still reach the refusal")
        self.assertIn(
            "REFUSED: tailscale serve already has a handler on :443",
            _squash(proc.stderr))
        violations = refusal_violations("ps1", _read_log(log), data_dir)
        self.assertTrue(
            violations,
            "the ps1 reorder mutant (New-Item hoisted above the serve "
            "preflight) was NOT detected - the guardrail predicate is "
            "vacuous; re-anchor it on the live installer")
        self.assertTrue(
            any(ln.startswith("New-Item") for ln in _read_log(log)),
            f"expected the hoisted New-Item in the log: {_read_log(log)}")

    # ---- family (b): serve call hoisted above the health check -----------

    def test_sh_serve_before_health_mutant_is_detected(self):
        if _BASH is None:
            self.skipTest("no usable bash runtime available "
                          "(ZMEM_TEST_BASH unset and the bash probe failed)")
        # Same gating as the sh happy path: the mutant performs the unit
        # write, so it must run inside the private mount namespace.
        (_root, stubs, log, state, tokens, data_dir,
         unit_dir) = _setup_namespaced_leg(self, "e2e-sh-serve-mutant-")
        mutant = unit_dir.parent / "install-hub.sh"
        mutant.write_bytes(_hoist_lines(
            SH_INSTALLER.read_text(encoding="utf-8"),
            [SH_ANCHOR_SERVE_BG], SH_ANCHOR_HEALTH, self, "sh"))
        mutant.chmod(0o755)
        proc = run_sh_installer_namespaced(
            mutant, [str(REPO_ROOT), str(data_dir), str(tokens), "9001", "9443"],
            stubs, log, state, unit_dir)
        # A SILENT reorder: the mutant still succeeds...
        self.assertEqual(
            proc.returncode, 0,
            f"the serve-before-health mutant must still complete the happy "
            f"path (stdout={proc.stdout!r} stderr={proc.stderr!r})")
        self.assertNotIn("REFUSED", proc.stderr)
        self.assertTrue(
            (unit_dir / UNIT_NAME).exists(),
            "the unit write did not land in the bind-mounted temp dir - "
            "the namespace bind mount did not take effect")
        # ...only the shared happy-path predicate can see the reorder.
        violations = happy_path_violations("sh", _read_log(log))
        self.assertTrue(
            violations,
            "the sh serve-before-health mutant was NOT detected - the "
            "happy-path predicate is vacuous; re-anchor it on the live "
            "installer")
        self.assertTrue(
            any("health check" in v for v in violations),
            f"expected a before-health-check violation: {violations}")

    def test_ps1_serve_before_health_mutant_is_detected(self):
        _require_ps1(self)
        root = _mk_sandbox("e2e-ps1-serve-mutant-", self)
        stubs = _make_ps1_sandbox(root)
        mutant = root / "install-hub.ps1"
        mutant.write_bytes(
            b"\xef\xbb\xbf" + _hoist_lines(
                PS1_INSTALLER.read_text(encoding="utf-8-sig"),
                [PS1_ANCHOR_SERVE_BG], PS1_ANCHOR_HEALTH, self, "ps1"))
        log = root / "log.txt"
        state = _seed_state(root, STATUS_NO_CONFIG)
        tokens = _mk_tokens(root)
        data_dir = root / "hubdata"
        proc = run_ps1_installer(
            mutant, data_dir, tokens, 9001, 9443, log, state, stubs,
            root / "wrap.ps1")
        # A SILENT reorder: unoccupied status + healthy task -> exit 0, no
        # refusal; only the shared happy-path predicate sees it.
        self.assertEqual(
            proc.returncode, 0,
            f"the serve-before-health mutant must still complete the happy "
            f"path (stdout={proc.stdout!r} stderr={proc.stderr!r})")
        self.assertNotIn("REFUSED", proc.stderr)
        violations = happy_path_violations("ps1", _read_log(log))
        self.assertTrue(
            violations,
            "the ps1 serve-before-health mutant was NOT detected - the "
            "happy-path predicate is vacuous; re-anchor it on the live "
            "installer")
        self.assertTrue(
            any("health check" in v for v in violations),
            f"expected a before-health-check violation: {violations}")


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
