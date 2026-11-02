#!/usr/bin/env bash
# serve-status-classify.sh - classify `tailscale serve status` output
# (issue #423, review round 3).
#
# The behavioral oracle in tests/test_fleet_installer_443.py drives THIS file,
# so a change to the shipped matcher cannot escape the test (the previous
# oracle re-implemented the patterns and drifted).
#
# stdin:  the raw serve-status text (may be empty).
# env:    PORT - the hub's localhost port the installer intends to publish
#         (default 8765).
# stdout: exactly one line "OCCUPIER OURS" with 0/1 values.
#
# Semantics (issue #423 + review rounds 1-3):
#   * An OCCUPIER of the default :443 is a web handler whose host carries no
#     explicit port or an explicit :443 (tailscale suppresses the default), or
#     a TLS-terminated TCP forward on 443. A handler on another port (e.g.
#     https://host:8443) is NOT an occupier, and neither is a plain http://
#     handler (tailscale suppresses :80 the same way).
#   * The idempotent bypass (OURS) is scoped to the default-port web host: it
#     applies ONLY when some default-port web block's handler set is non-empty
#     and entirely the hub's own localhost proxy. A mixed block (operator +
#     hub routes on the same host) is NOT a bypass, and a hub handler under a
#     non-default header never masks a :443 operator.
set -u

PORT="${PORT:-}"
case "$PORT" in
    ''|*[!0-9]*) PORT=8765 ;;
esac

OCCUPIER=0; OURS=0
IN_DEFAULT=0; BLOCK_HAS=0; BLOCK_ALL_OURS=1

flush_block() {
    if [ "$IN_DEFAULT" -eq 1 ] && [ "$BLOCK_HAS" -eq 1 ] && [ "$BLOCK_ALL_OURS" -eq 1 ]; then
        OURS=1
    fi
    IN_DEFAULT=0; BLOCK_HAS=0; BLOCK_ALL_OURS=1
}

while IFS= read -r line || [ -n "$line" ]; do
    if printf '%s' "$line" | grep -Eq \
        '^https://[^/: ]+(:443)?([[:space:]]|$)|^http://[^/: ]+:443([[:space:]]|$)'; then
        flush_block
        IN_DEFAULT=1; OCCUPIER=1; BLOCK_HAS=0; BLOCK_ALL_OURS=1
    elif printf '%s' "$line" | grep -Eq '^https?://'; then
        # A web header with an explicit non-443 port (e.g. the hub on 8443):
        # not an occupier; it ends the current block.
        flush_block
        IN_DEFAULT=0
    elif printf '%s' "$line" | grep -Eq '^\|--'; then
        if printf '%s' "$line" | grep -Eq '\|-- tcp://[^ ]+:443 '; then
            OCCUPIER=1
            if [ "$IN_DEFAULT" -eq 1 ]; then
                BLOCK_ALL_OURS=0
            fi
        elif [ "$IN_DEFAULT" -eq 1 ]; then
            BLOCK_HAS=1
            printf '%s' "$line" | grep -Eq "proxy http://127\.0\.0\.1:$PORT([^0-9]|\$)" || BLOCK_ALL_OURS=0
        fi
    fi
done

flush_block
echo "$OCCUPIER $OURS"
