#!/usr/bin/env bash
# Run from a reviewed local checkout on the UDM-SE. This only prepares files.
set -euo pipefail
umask 077
MIHOMO_DIR=/data/mihomo
UNIT_DIR=/etc/systemd/system
SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
VERSION=v1.19.31
SHA256=9e0f11afbf38426b8bd88fdc594678f8161c57eccb4e1b77acb12b493904f1d4
OWNER=udmse-mihomo-v2
UNITS=(mihomo.service mihomo-watchdog.service mihomo-watchdog.timer mihomo-admin.service)
FILES=(telemetry.py admin/theme.js admin/telemetry.js admin-server.py admin/index.html admin/app.js admin/style.css admin/favicon.svg config.yaml routing.env.example install-config.py mihomo-routing.sh 20-mihomo.sh mihomo-watchdog.sh uninstall.sh "${UNITS[@]}")
SUBSCRIPTION_FILE=''
ROUTING_FILE=''
fail() { printf '%s\n' "$*" >&2; exit 1; }
while (($#)); do
    case "$1" in
        --subscription-file|--routing-file)
            (($# >= 2)) || fail "Missing file argument for $1"
            if [[ $1 == --subscription-file ]]; then SUBSCRIPTION_FILE=$2; else ROUTING_FILE=$2; fi
            shift 2 ;;
        -h|--help)
            echo 'Usage: sudo bash install.sh [--subscription-file FILE] [--routing-file FILE]'
            echo 'Prepares /data/mihomo only. Does not start services or install a boot hook.'
            exit 0 ;;
        *) fail "Unknown option: $1" ;;
    esac
done
[[ $(id -u) == 0 ]] || fail 'Run as root on the UDM-SE.'
[[ $(uname -s) == Linux && $(uname -m) == aarch64 ]] || fail 'Only Linux ARM64 (UDM-SE) is supported.'
for command in curl gzip sha256sum python3 systemctl flock ip iptables; do
    command -v "$command" >/dev/null || fail "Required command missing: $command"
done
exec 8>/run/lock/udmse-mihomo-install.lock
flock -n 8 || fail 'Another install/uninstall operation is running.'
# Never stop, overwrite or upgrade a pre-existing installation implicitly.
[[ ! -e $MIHOMO_DIR && ! -L $MIHOMO_DIR ]] || fail 'Existing /data/mihomo found. No files changed; migration/upgrade requires a separate reviewed procedure.'
[[ ! -e /data/on_boot.d/20-mihomo.sh && ! -L /data/on_boot.d/20-mihomo.sh ]] || fail 'Existing boot hook found; no changes made.'
for unit in "${UNITS[@]}"; do
    for directory in "$UNIT_DIR" /run/systemd/system /lib/systemd/system /usr/lib/systemd/system; do
        [[ ! -e $directory/$unit && ! -L $directory/$unit && ! -e $directory/$unit.d ]] || fail "Existing unit or overrides found: $unit"
    done
done
for file in "${FILES[@]}"; do
    [[ -f $SOURCE_DIR/$file ]] || fail "Incomplete checkout: missing $file"
done
[[ -z $SUBSCRIPTION_FILE || -r $SUBSCRIPTION_FILE ]] || fail 'Cannot read subscription file.'
[[ -z $ROUTING_FILE || -r $ROUTING_FILE ]] || fail 'Cannot read routing file.'
[[ -d /data && -d $UNIT_DIR ]] || fail 'Expected UDM data/systemd directories are missing.'
STAGE=$(mktemp -d /data/.mihomo-install.XXXXXX)
PUBLISHED=0
COMPLETE=0
COPIED_UNITS=()
cleanup() {
    local status=$?
    trap - EXIT
    if (( ! COMPLETE )); then
        for unit in "${COPIED_UNITS[@]}"; do rm -f -- "$UNIT_DIR/$unit"; done
        if ((PUBLISHED)); then rm -rf -- "$MIHOMO_DIR"; fi
        if ((${#COPIED_UNITS[@]})); then systemctl daemon-reload >/dev/null 2>&1 || true; fi
    fi
    [[ -z $STAGE ]] || rm -rf -- "$STAGE"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir "$STAGE/providers" "$STAGE/rules" "$STAGE/admin"
for file in "${FILES[@]}"; do cp -- "$SOURCE_DIR/$file" "$STAGE/$file"; done
chmod 700 "$STAGE" "$STAGE/providers" "$STAGE/rules" "$STAGE/admin"
chmod 600 "$STAGE/admin/"*
chmod 600 "$STAGE"/*.*
chmod 700 "$STAGE"/*.sh
if [[ -n $SUBSCRIPTION_FILE ]]; then
    cp -- "$SUBSCRIPTION_FILE" "$STAGE/.subscription"
else
    [[ -t 0 ]] || fail 'Noninteractive installation requires --subscription-file.'
    read -r -s -p 'HTTPS subscription URL (hidden): ' subscription
    printf '\n' >&2
    printf '%s' "$subscription" > "$STAGE/.subscription"
    unset subscription
fi
if [[ -n $ROUTING_FILE ]]; then
    cp -- "$ROUTING_FILE" "$STAGE/routing.env"
else
    cp -- "$STAGE/routing.env.example" "$STAGE/routing.env"
fi
chmod 600 "$STAGE/routing.env"
bash -n "$STAGE/routing.env" || fail 'Routing configuration has invalid shell syntax.'
python3 "$STAGE/install-config.py" "$SOURCE_DIR/config.yaml" "$STAGE/.subscription" "$STAGE/config.yaml" "$STAGE/routing.env"
rm -- "$STAGE/.subscription"
download() { curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --connect-timeout 15 --max-time 180 --output "$2" "$1"; }
echo "Downloading and verifying Mihomo $VERSION (ARM64)."
download "https://github.com/MetaCubeX/mihomo/releases/download/$VERSION/mihomo-linux-arm64-$VERSION.gz" "$STAGE/mihomo.gz"
printf '%s  %s\n' "$SHA256" "$STAGE/mihomo.gz" | sha256sum --check --status || fail 'Mihomo checksum mismatch; installation cancelled.'
gzip -dc "$STAGE/mihomo.gz" > "$STAGE/mihomo"
chmod 700 "$STAGE/mihomo"
rm -- "$STAGE/mihomo.gz"
download https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/meta/geo/geosite/cn.mrs "$STAGE/rules/cn-domain.mrs"
download https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/meta/geo/geoip/cn.mrs "$STAGE/rules/cn-ip.mrs"
# Mihomo diagnostics can contain subscription credentials; never echo them.
if ! "$STAGE/mihomo" -t -d "$STAGE" -f "$STAGE/config.yaml" > "$STAGE/.config-check.log" 2>&1; then
    fail 'Mihomo configuration check failed; staged files removed (diagnostics suppressed to protect subscription secrets).'
fi
rm -- "$STAGE/.config-check.log"
python3 -c 'import secrets,sys; from pathlib import Path; p=Path(sys.argv[1]); p.write_text(secrets.token_hex(32)+"\n"); p.chmod(0o600)' "$STAGE/admin-token"
printf '%s\n' "$OWNER" > "$STAGE/.managed-by"
mv -- "$STAGE" "$MIHOMO_DIR"
STAGE=''
PUBLISHED=1
for unit in "${UNITS[@]}"; do
    COPIED_UNITS+=("$unit")
    install -m 644 "$MIHOMO_DIR/$unit" "$UNIT_DIR/$unit"
done
systemctl daemon-reload
COMPLETE=1
printf '%s\n' 'Prepared /data/mihomo. Nothing is started or enabled.'
printf '%s\n' 'Set the explicit source scope in /data/mihomo/routing.env, then run: /data/mihomo/20-mihomo.sh start'
printf '%s\n' 'API: 127.0.0.1:9090; its generated secret is private in config.yaml. No boot hook was installed.'
printf '%s\n' 'Admin page: start mihomo-admin.service, then SSH-forward localhost:9088. Its separate login token is in /data/mihomo/admin-token.'
