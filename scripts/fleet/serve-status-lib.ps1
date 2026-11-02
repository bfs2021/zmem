# serve-status-lib.ps1 - shared serve-status classifier for the hub installer
# (issue #423, review rounds 3-4).
#
# Dot-sourced by install-hub.ps1; executed directly by
# tests/test_fleet_installer_443.py so the behavioral oracle drives the
# SHIPPED classifier, not a re-implementation.
#
# Get-ServeStatusClassification -Lines <serve-status lines> -OurPort <port>
#   [-TargetPort <serve-port>] returns Occupier / Ours with the SAME
#   semantics as the sh classifier (scripts/fleet/serve-status-classify.sh):
#   * Occupier: the web block of TARGET_PORT (default 443) - a host with no
#     explicit port or an explicit :443 when TARGET_PORT is 443, or a host
#     with an explicit :TARGET_PORT - or a TLS-terminated TCP forward on
#     TARGET_PORT. Blocks on other ports are ignored; a plain http://
#     handler is a :80 handler, not a :443 occupier.
#   * Ours: scoped to the TARGET_PORT web host - true ONLY when some
#     TARGET_PORT web block's handler set is non-empty and entirely the
#     hub's own localhost proxy. A mixed block (operator + hub routes on the
#     same host) is NOT a bypass, a hub handler under a different port never
#     masks the target, and EVERY target-port web block is considered (the
#     flush runs per block, not only at EOF - a later non-target block must
#     not erase an earlier all-ours one).
function Get-ServeStatusClassification {
    param(
        [string[]]$Lines = @(),
        [int]$OurPort = 8765,
        [int]$TargetPort = 443
    )
    $occupier = $false
    $ours = $false
    $inTarget = $false
    $blockHas = $false
    $blockAllOurs = $true
    # Ours is an AND across every target-port block (critic round): at
    # least one target block must carry handlers AND every one of them
    # must be entirely ours - a sticky per-block OR let an operator or
    # mixed target block hide behind an all-ours sibling.
    $sawTargetBlock = $false
    $allTargetBlocksOurs = $true
    foreach ($line in $Lines) {
        if ($line -match '^https?://') {
            if ($inTarget -and $blockHas) {
                $sawTargetBlock = $true
                if (-not $blockAllOurs) { $allTargetBlocksOurs = $false }
            }
            if ($line -match '^(https://[^/: ]+(:443)?|http://[^/: ]+:443)(\s|$)') {
                if ($TargetPort -eq 443) {
                    $occupier = $true
                    $inTarget = $true
                } else {
                    $inTarget = $false
                }
            } elseif ($line -match ('^https?://[^/: ]+:' + $TargetPort + '(\s|$)')) {
                $occupier = $true
                $inTarget = $true
            } else {
                $inTarget = $false
            }
            $blockHas = $false
            $blockAllOurs = $true
            continue
        }
        if ($line -notmatch '^\|--') { continue }
        if ($line -match ('\|-- tcp://[^ ]+:' + $TargetPort + ' ')) {
            $occupier = $true
            # A tcp forward on the target port suppresses ours at ANY position.
            $allTargetBlocksOurs = $false
            if ($inTarget) { $blockAllOurs = $false }
            continue
        }
        if (-not $inTarget) { continue }
        $blockHas = $true
        if ($line -notmatch "proxy http://127\.0\.0\.1:$OurPort(\D|$)") {
            $blockAllOurs = $false
        }
    }
    if ($inTarget -and $blockHas) {
        $sawTargetBlock = $true
        if (-not $blockAllOurs) { $allTargetBlocksOurs = $false }
    }
    if ($sawTargetBlock -and $allTargetBlocksOurs) { $ours = $true }
    [pscustomobject]@{ Occupier = $occupier; Ours = $ours }
}
