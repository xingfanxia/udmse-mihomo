#!/usr/bin/env bash
# Only remove installations created by this installer; keep private data unless
# --purge was explicitly requested. Never alter the router's own DNS/firewall.
set -euo pipefail
umask 077
MIHOMO_DIR=/data/mihomo
UNIT_DIR=/etc/systemd/system
STATE=/run/mihomo-routing
OWNER=udmse-mihomo-v2
UNITS=(mihomo.service mihomo-watchdog.service mihomo-watchdog.timer)
PURGE=0
fail() { printf '%s\n' "$*" >&2; exit 1; }
case "${1:-}" in
    '') ;;
    --purge) PURGE=1; shift ;;
    -h|--help) echo 'Usage: sudo bash uninstall.sh [--purge]'; exit 0 ;;
    *) fail 'Only --purge is supported.' ;;
esac
(($# == 0)) || fail 'Unexpected arguments.'
[[ $(id -u) == 0 ]] || fail 'Run as root.'
for command in systemctl flock cmp stat; do command -v "$command" >/dev/null || fail "Missing command: $command"; done
exec 8>/run/lock/udmse-mihomo-install.lock
flock -n 8 || fail 'Another install/uninstall operation is running.'
[[ -d $MIHOMO_DIR && ! -L $MIHOMO_DIR && $(stat -c %u "$MIHOMO_DIR") == 0 ]] || fail 'Installation root is absent, symlinked, or not root-owned; no changes made.'
[[ -f $MIHOMO_DIR/.managed-by && ! -L $MIHOMO_DIR/.managed-by && $(cat "$MIHOMO_DIR/.managed-by") == "$OWNER" ]] || fail 'Unsupported installation ownership; no changes made.'
# Validate every artifact before stopping anything. User modifications require a
# deliberate manual resolution; they are never overwritten or guessed at.
for unit in "${UNITS[@]}"; do
    [[ -f $MIHOMO_DIR/$unit && ! -L $MIHOMO_DIR/$unit ]] || fail "Missing installed ownership reference: $unit"
    if [[ -e $UNIT_DIR/$unit || -L $UNIT_DIR/$unit ]]; then
        if [[ ! -f $UNIT_DIR/$unit || -L $UNIT_DIR/$unit ]] || ! cmp -s "$MIHOMO_DIR/$unit" "$UNIT_DIR/$unit"; then
            fail "Unit ownership mismatch: $unit"
        fi
    fi
    for directory in "$UNIT_DIR" /run/systemd/system /lib/systemd/system /usr/lib/systemd/system; do
        [[ ! -e $directory/$unit.d ]] || fail "Unit overrides found: $unit"
        if [[ $directory != "$UNIT_DIR" ]]; then
            [[ ! -e $directory/$unit && ! -L $directory/$unit ]] || fail "Another unit definition exists: $unit"
        fi
    done
done
BOOT=/data/on_boot.d/20-mihomo.sh
if [[ -e $BOOT || -L $BOOT ]]; then
    if [[ ! -f $BOOT || -L $BOOT ]] || ! cmp -s "$MIHOMO_DIR/20-mihomo.sh" "$BOOT"; then
        fail 'Boot hook ownership mismatch.'
    fi
fi
[[ -x $MIHOMO_DIR/mihomo-routing.sh && ! -L $MIHOMO_DIR/mihomo-routing.sh ]] || fail 'Owned routing cleanup script is missing; no changes made.'
# Prevent recovery/restart races, then detach traffic BEFORE stopping listeners.
rm -f -- "$STATE/wanted"
stop_if_present() {
    if [[ -e $UNIT_DIR/$1 ]] || systemctl is-active --quiet "$1"; then
        systemctl stop "$1"
    fi
}
stop_if_present mihomo-watchdog.timer
stop_if_present mihomo-watchdog.service
# The bootstrap rollback is transient and may already have expired.
if systemctl is-active --quiet mihomo-rollback.timer; then systemctl stop mihomo-rollback.timer; fi
if systemctl is-active --quiet mihomo-rollback.service; then systemctl stop mihomo-rollback.service; fi
"$MIHOMO_DIR/mihomo-routing.sh" detach
stop_if_present mihomo.service
"$MIHOMO_DIR/mihomo-routing.sh" cleanup
for unit in mihomo.service mihomo-watchdog.timer; do
    [[ ! -e $UNIT_DIR/$unit ]] || systemctl disable "$unit"
done
for unit in "${UNITS[@]}"; do rm -f -- "$UNIT_DIR/$unit"; done
[[ ! -e $BOOT ]] || rm -- "$BOOT"
systemctl daemon-reload
if ((PURGE)); then
    # Refuse mounted content; --purge is only for files owned by this install.
    command -v findmnt >/dev/null || fail 'findmnt is required for --purge; private files were preserved.'
    if findmnt -rn -o TARGET | awk -v root="$MIHOMO_DIR" '$0 == root || index($0, root "/") == 1 {found=1} END {exit !found}'; then
        fail 'Mounted content exists under /data/mihomo; private files were preserved.'
    fi
    rm -rf --one-file-system -- "$MIHOMO_DIR"
    echo 'Routing and services removed; /data/mihomo purged.'
else
    # Retain runtime files too: the cleanup helper and exact unit references
    # make later inspection or an explicit --purge possible without guessing.
    echo 'Routing and services removed. Private configuration and providers remain in /data/mihomo (mode 0700).'
    echo 'Use uninstall.sh --purge only when you intend to delete all retained private data.'
fi
