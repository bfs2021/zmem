# install-hub.ps1 - register the zmem hub on this Windows box (issue #291).
#
# Contract (Workstream T PR 12, AC3):
#   * the hub runs as a scheduled task bound to 127.0.0.1 ONLY (never a
#     wildcard bind), started at boot as the MAP OWNER via S4U logon (no
#     password, fires without an interactive logon, and the identity can
#     READ the issuer-hardened principal map);
#   * the task action is a generated wrapper that sets the hub env
#     (principal map + data dir) before launching python - the raw task
#     action cannot carry environment variables;
#   * a principal map with a broad ACE (Users/Everyone/Authenticated
#     Users, matched by well-known SID so localized Windows cannot defeat
#     it) is REFUSED before anything is configured;
#   * idempotent: re-running replaces the wrapper, the task, and re-points
#     serve (subject to the serve occupancy preflight below), and RESTARTS
#     the task so the running hub converges onto the new configuration.
#
# Usage:
#   .\install-hub.ps1 -RepoRoot C:\path\to\zmem -DataDir D:\zmem-data `
#       -TokensFile D:\zmem-data\principal-map.json -Port 8765 [-TaskName zmem-hub] [-HttpsPort 443]
param(
    [Parameter(Mandatory = $true)][string]$RepoRoot,
    [Parameter(Mandatory = $true)][string]$DataDir,
    [Parameter(Mandatory = $true)][string]$TokensFile,
    [int]$Port = 8765,
    [string]$TaskName = "zmem-hub",
    [int]$HttpsPort = 443
)

$ErrorActionPreference = "Stop"

if ($Port -lt 1 -or $Port -gt 65535) {
    Write-Error "-Port must be 1-65535 (got: $Port)"
}
if ($HttpsPort -lt 1 -or $HttpsPort -gt 65535) {
    Write-Error "-HttpsPort must be 1-65535 (got: $HttpsPort)"
}

# Shared serve-status classifier (issue #423, review round 3): dot-sourced so
# the installer and the behavioral oracle in tests/test_fleet_installer_443.py
# execute the SAME shipped code.
. (Join-Path $PSScriptRoot "serve-status-lib.ps1")

# --- refuse a broad-ACL principal map (SID-based, locale-independent) -------
if (-not (Test-Path $TokensFile)) {
    Write-Error "principal map not found: $TokensFile (create it with issue-principal.py first)"
}
$wellKnownSids = @("S-1-1-0", "S-1-5-32-545", "S-1-5-11")
$broad = (Get-Acl $TokensFile).Access |
    Where-Object { $_.AccessControlType -eq "Allow" } |
    Where-Object {
        $sid = $null
        try {
            $sid = $_.IdentityReference.Translate(
                [System.Security.Principal.SecurityIdentifier]).Value
        } catch {
            $sid = $_.IdentityReference.Value
        }
        $wellKnownSids -contains $sid
    }
if ($broad) {
    Write-Error "REFUSED: $TokensFile grants a broad allow ACE (Everyone / Users / Authenticated Users - matched by SID). Restrict it to the hub service account first."
}

# --- tailscale serve occupancy preflight (issue #423, review round 3) -------
# The guard runs BEFORE ANY side effect (wrapper write, task registration,
# task start): a refusal must leave the host exactly as found, not in the
# half-configured "boot task live, no ingress" state issue #423 complains
# about. The status query is FAIL-CLOSED: an unreadable serve status refuses
# instead of falling through to the serve call.
$serveFound = [bool](Get-Command tailscale -ErrorAction SilentlyContinue)
if ($serveFound) {
    # PS 5.1: a native-command stderr byte under EAP=Stop throws
    # NativeCommandError BEFORE the guarded check runs (review round 4), so
    # the read is scoped to EAP=Continue and classified by EXIT CODE.
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $statusLines = @(& tailscale serve status 2>$null)
        $statusRc = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevEap
    }
    if ($statusRc -ne 0 -or $statusLines.Count -eq 0) {
        Write-Error ("REFUSED: cannot verify tailscale serve occupancy " +
            "(status query failed, exit $statusRc). Not publishing over an " +
            "unverifiable serve configuration - check tailscaled, then re-run.")
    }
    $cls = Get-ServeStatusClassification -Lines $statusLines -OurPort $Port -TargetPort $HttpsPort
    if ($cls.Occupier -and -not $cls.Ours) {
        $statusText = $statusLines -join "`n"
        Write-Error ("REFUSED: tailscale serve already has a handler on " +
            ":$HttpsPort (an operator service is live there, or the " +
            "existing handler set is not entirely ours). Re-run with an " +
            "explicit -HttpsPort <n> to publish the hub on another " +
            "port instead of replacing it. Current serve status:`n$statusText")
    }
}

# --- data dir + wrapper script (the task action cannot carry env) -----------
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
$serverPy = Join-Path $RepoRoot "hermes-plugin\server\mcp_server.py"
if (-not (Test-Path $serverPy)) { Write-Error "mcp_server.py not found under $RepoRoot" }
$pythonExe = (Get-Command python).Source
if (-not $pythonExe) { Write-Error "python not found on PATH" }

$wrapper = Join-Path $DataDir "hub-run.cmd"
@"
@echo off
set "ZMEM_MCP_TOKENS_FILE=$TokensFile"
set "ZMEM_DATA=$DataDir"
"$pythonExe" -X utf8 "$serverPy" --host 127.0.0.1 --port $Port
"@ | Set-Content -Path $wrapper -Encoding ASCII

# --- the service: startup scheduled task as the map owner (S4U) --------------
$action = New-ScheduledTaskAction -Execute "$wrapper"
$trigger = New-ScheduledTaskTrigger -AtStartup
# Run the task as the ACL OWNER of the principal map with S4U logon: no
# password prompt, fires without an interactive logon, and the identity
# can READ the issuer-hardened principal map (issue-principal strips
# inheritance and grants only the issuer - a SYSTEM principal could not
# read it and the hub would exit 2 at startup; review round 2).
$owner = (Get-Acl $TokensFile).Owner
$principal = New-ScheduledTaskPrincipal -UserId $owner -LogonType S4U
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 5
$state = (Get-ScheduledTask -TaskName $TaskName).State
if ($state -ne "Running") {
    Write-Error "REFUSED: hub task state is '$state' (expected Running) - not configuring tailscale serve against a hub that is not up (fix the task/event-log first)"
}

# --- tailscale serve: tailnet HTTPS -> localhost ONLY -----------------------
# Issue #423: the serve HTTPS port is a parameter. The OCCUPANCY PREFLIGHT
# ran before any side effect (see above); this late block only EXECUTES the
# serve re-point, after the hub task is verified up, so serve is never
# configured against a dead hub.
if (-not $serveFound) {
    Write-Warning "tailscale not found - install it, then run: tailscale serve --bg --https=$HttpsPort http://127.0.0.1:$Port"
} else {
    tailscale serve --bg --https=$HttpsPort "http://127.0.0.1:$Port"
    if ($LASTEXITCODE -ne 0) { Write-Error "tailscale serve failed (exit $LASTEXITCODE)" }
    tailscale serve status
}

Write-Host ""
Write-Host "Hub installed (task $TaskName; wrapper $wrapper)."
Write-Host "Issue one principal per broker with python scripts/fleet/issue-principal.py,"
Write-Host "then run scripts/fleet/install-broker.ps1 on each broker machine."
