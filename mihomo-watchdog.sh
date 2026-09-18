#!/usr/bin/env bash
# Reconcile only an explicitly requested trial. Never modify system DNS.
set -Eeuo pipefail
ROOT=${MIHOMO_DIR:-/data/mihomo}
STATE=${MIHOMO_STATE_DIR:-/run/mihomo-routing}
CONTROL="$ROOT/mihomo-routing.sh"
[[ -f "$STATE/wanted" && -f "$STATE/owned" ]] || exit 0
if ! systemctl is-active --quiet mihomo-rollback.timer; then
  exec "$ROOT/20-mihomo.sh" stop
fi
if ! "$CONTROL" check-guard; then
  # A removed/reordered UniFi INPUT guard must not leave the DNS listener exposed.
  echo 'Listener guard changed; stopping trial.' >&2
  exec "$ROOT/20-mihomo.sh" stop
fi
if ! "$CONTROL" preflight; then
  echo 'Runtime prerequisites or scope changed; stopping trial.' >&2
  exec "$ROOT/20-mihomo.sh" stop
fi
if ! "$CONTROL" health; then
  "$CONTROL" detach
  echo 'Proxy probe failed; selected clients restored to normal routing.' >&2
  exit 0
fi
if ! "$CONTROL" check; then
  "$CONTROL" attach
fi
