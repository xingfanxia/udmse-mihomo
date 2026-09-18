#!/usr/bin/env bash
# Explicit lifecycle entrypoint. Installation alone never starts routing.
set -Eeuo pipefail
ROOT=${MIHOMO_DIR:-/data/mihomo}
STATE=${MIHOMO_STATE_DIR:-/run/mihomo-routing}
CONTROL="$ROOT/mihomo-routing.sh"
exec 8>"${MIHOMO_LIFECYCLE_LOCK:-/run/lock/udmse-mihomo-install.lock}"
flock -x 8
stop() {
  rm -f "$STATE/wanted"
  systemctl stop mihomo-watchdog.timer
  "$CONTROL" detach
  systemctl stop mihomo.service
  "$CONTROL" cleanup
  systemctl stop mihomo-rollback.timer 2>/dev/null || true
  echo 'Mihomo routing stopped; original UniFi routing retained.'
}
case "${1:-status}" in
 start)
   minutes=${2:-15}
   if [[ ! "$minutes" =~ ^[1-9][0-9]{0,3}$ ]] || ((minutes>1440)); then echo 'Choose 1–1440 trial minutes' >&2; exit 1; fi
   [[ ! -f "$STATE/wanted" ]] || { echo 'A trial is already requested; stop it before starting another.' >&2; exit 1; }
   "$CONTROL" preflight
   trap 'stop' ERR
   systemd-run --unit=mihomo-rollback --on-active="${minutes}m" "$ROOT/20-mihomo.sh" stop
   "$CONTROL" guard
   systemctl start mihomo.service
   ready=false
   for _ in 1 2 3; do
     if "$CONTROL" health; then ready=true; break; fi
     sleep 1
   done
   "$ready" || { stop; exit 1; }
   touch "$STATE/wanted"
   "$CONTROL" attach
   systemctl start mihomo-watchdog.timer
   trap - ERR
   echo "Scoped routing active; automatic stop in $minutes minutes."
   ;;
 stop) stop ;;
 status) "$CONTROL" status ;;
 *) echo 'Usage: 20-mihomo.sh start [minutes=15] | stop | status' >&2; exit 2 ;;
esac
