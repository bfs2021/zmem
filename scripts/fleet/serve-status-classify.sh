#!/usr/bin/env bash
# serve-status-classify.sh - classify `tailscale serve status` output
# (issue #423, review rounds 3-4).
#
# The behavioral oracle in tests/test_fleet_installer_443.py drives THIS file,
# so a change to the shipped matcher cannot escape the test (the previous
# oracle re-implemented the patterns and drifted).
#
# stdin:  the raw serve-status text (may be empty).
# env:    PORT        - the hub's localhost port the installer publishes
#                       (default 8765).
#         TARGET_PORT - the serve HTTPS port the installer intends to claim
#                       (default 443).
# stdout: exactly one line "OCCUPIER OURS" with 0/1 values.
#
# Semantics (issue #423 + review rounds 1-4):
#   * An OCCUPIER of TARGET_PORT is the web block whose host carries no
#     explicit port or an explicit :443 (when TARGET_PORT is 443 - tailscale
#     suppresses the default), or a host with an explicit :TARGET_PORT, or a
#     TLS-terminated TCP forward on TARGET_PORT. Blocks on other ports are
#     ignored; a plain http:// handler is a :80 handler, not a :443 occupier.
#   * The idempotent bypass (OURS) is scoped to the TARGET_PORT web host: it
#     applies ONLY when some TARGET_PORT web block's handler set is non-empty
#     and entirely the hub's own localhost proxy. A mixed block (operator +
#     hub routes on the same host) is NOT a bypass, a hub handler under a
#     different port never masks the target, and EVERY default-port web block
#     is considered (flush happens per block, not just at EOF).
set -u

PORT="${PORT:-}"
case "$PORT" in
    ''|*[!0-9]*) PORT=8765 ;;
esac
TARGET_PORT="${TARGET_PORT:-443}"
case "$TARGET_PORT" in
    ''|*[!0-9]*) TARGET_PORT=443 ;;
esac

OCCUPIER=0; OURS=0
IN_TARGET=0; BLOCK_HAS=0; BLOCK_ALL_OURS=1

flush_block() {
    if [ "$IN_TARGET" -eq 1 ] && [ "$BLOCK_HAS" -eq 1 ] && [ "$BLOCK_ALL_OURS" -eq 1 ]; then
        OURS=1
    fi
    IN_TARGET=0; BLOCK_HAS=0; BLOCK_ALL_OURS=1
}

while IFS= read -r line || [ -n "$line" ]; do
    if printf '%s' "$line" | grep -Eq '^https?://'; then
        flush_block
        if printf '%s' "$line" | grep -Eq \
            '^https://[^/: ]+(:443)?([[:space:]]|$)|^http://[^/: ]+:443([[:space:]]|$)'; then
            if [ "$TARGET_PORT" -eq 443 ]; then
                OCCUPIER=1; IN_TARGET=1
            fi
        elif printf '%s' "$line" | grep -Eq \
            "^https?://[^/: ]+:$TARGET_PORT([[:space:]]|\$)"; then
            OCCUPIER=1; IN_TARGET=1
        fi
    elif printf '%s' "$line" | grep -Eq '^\|--'; then
        if printf '%s' "$line" | grep -Eq "\|-- tcp://[^ ]+:$TARGET_PORT "; then
            OCCUPIER=1
            if [ "$IN_TARGET" -eq 1 ]; then
                BLOCK_ALL_OURS=0
            fi
        elif [ "$IN_TARGET" -eq 1 ]; then
            BLOCK_HAS=1
            printf '%s' "$line" | grep -Eq "proxy http://127\.0\.0\.1:$PORT([^0-9]|\$)" || BLOCK_ALL_OURS=0
        fi
    fi
done

flush_block
echo "$OCCUPIER $OURS"
