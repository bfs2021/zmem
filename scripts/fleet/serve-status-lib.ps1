# serve-status-lib.ps1 - shared serve-status classifier for the hub installer
# (issue #423, review round 3).
#
# Dot-sourced by install-hub.ps1; executed directly by
# tests/test_fleet_installer_443.py so the behavioral oracle drives the
# SHIPPED classifier, not a re-implementation (the previous oracle
# re-implemented the patterns and drifted).
#
# Get-ServeStatusClassification -Lines <serve-status lines> -OurPort <port>
#   returns Occupier / Ours with the SAME semantics as the sh classifier
#   (scripts/fleet/serve-status-classify.sh):
#   * Occupier: a web handler whose host carries no explicit port or an
#     explicit :443 (tailscale suppresses the default), or a TLS-terminated
#     TCP forward on 443. A handler on another port (e.g. https://host:8443)
#     is NOT an occupier; neither is a plain http:// handler (:80).
#   * Ours: scoped to the default-port web host - true ONLY when some
#     default-port web block's handler set is non-empty and entirely the
#     hub's own localhost proxy. A mixed block (operator + hub routes on the
#     same host) is NOT a bypass, and a hub handler under a non-default
#     header never masks a :443 operator.
function Get-ServeStatusClassification {
    param(
        [string[]]$Lines = @(),
        [int]$OurPort = 8765
    )
    $occupier = $false
    $ours = $false
    $inDefault = $false
    $blockHas = $false
    $blockAllOurs = $true
    foreach ($line in $Lines) {
        if ($line -match '^(https://[^/: ]+(:443)?|http://[^/: ]+:443)(\s|$)') {
            # Default-:443 web header (no explicit port, or explicit :443).
            $inDefault = $true
            $occupier = $true
            $blockHas = $false
            $blockAllOurs = $true
            continue
        }
        if ($line -match '^https?://') {
            # A web header with an explicit non-443 port (e.g. hub on 8443):
            # not an occupier; it ends the current block.
            $inDefault = $false
            $blockHas = $false
            $blockAllOurs = $true
            continue
        }
        if ($line -notmatch '^\|--') { continue }
        if ($line -match '\|-- tcp://[^ ]+:443 ') {
            $occupier = $true
            if ($inDefault) { $blockAllOurs = $false }
            continue
        }
        if (-not $inDefault) { continue }
        $blockHas = $true
        if ($line -notmatch "proxy http://127\.0\.0\.1:$OurPort(\D|$)") {
            $blockAllOurs = $false
        }
    }
    if ($inDefault -and $blockHas -and $blockAllOurs) { $ours = $true }
    [pscustomobject]@{ Occupier = $occupier; Ours = $ours }
}
