#!/usr/bin/env bash
# Owned IPv4 rules only. The root-owned routing.env is configuration, not user input.
set -Eeuo pipefail
ROOT=${MIHOMO_DIR:-/data/mihomo}
STATE=${MIHOMO_STATE_DIR:-/run/mihomo-routing}
IPT=(iptables -w 5)
MARK=0x400/0x400
TABLE=104
PREF=10010
mkdir -p "$STATE"
chmod 700 "$STATE"
exec 9>"$STATE/lock"
flock -x 9
fail() { echo "$*" >&2; exit 1; }
load_scope() {
  SCOPE=devices; LAN_INTERFACES=(); DEVICE_IPV4=''; DEVICE_MAC=''; SOURCE_CIDRS=(); IPV6_POLICY=require-no-default; DNS_LISTEN_IPV4=''
  # shellcheck source=/dev/null
  source "$1"
  [[ "$SCOPE" = devices || "$SCOPE" = networks ]] || fail 'SCOPE must be devices or networks'
  [[ "$IPV6_POLICY" = require-no-default ]] || fail 'Only require-no-default IPv6 policy is supported'
  python3 - "$DNS_LISTEN_IPV4" <<'PYDNS'
import ipaddress,sys
ip=ipaddress.IPv4Address(sys.argv[1])
assert ip.is_private and not ip.is_loopback and not ip.is_multicast and not ip.is_unspecified
PYDNS
  ((${#LAN_INTERFACES[@]})) || fail 'Select at least one LAN bridge'
  local iface
  for iface in "${LAN_INTERFACES[@]}"; do
    [[ "$iface" =~ ^br[0-9]+$ ]] || fail 'Only explicit UniFi LAN bridges (brN) are accepted'
  done
  if [[ "$SCOPE" = devices ]]; then
    [[ "$DEVICE_MAC" =~ ^([[:xdigit:]]{2}:){5}[[:xdigit:]]{2}$ ]] || fail 'Set DEVICE_MAC'
    python3 - "$DEVICE_IPV4" <<'PY'
import ipaddress,sys
ip=ipaddress.IPv4Address(sys.argv[1])
assert ip.is_private and not ip.is_loopback and not ip.is_multicast and not ip.is_unspecified
PY
    SOURCE_CIDRS=("$DEVICE_IPV4/32")
  else
    ((${#SOURCE_CIDRS[@]})) || fail 'Set explicit SOURCE_CIDRS for network scope'
    python3 - "${SOURCE_CIDRS[@]}" <<'PY'
import ipaddress,sys
for value in sys.argv[1:]:
 net=ipaddress.IPv4Network(value,strict=True)
 assert net.prefixlen >= 8 and net.is_private and not net.is_loopback and not net.is_multicast and not net.is_unspecified
PY
  fi
}
match_scope() {
  MATCH=(-i "$1" -s "$2")
  [[ "$SCOPE" != devices ]] || MATCH+=(-m mac --mac-source "$DEVICE_MAC")
}
no_ipv6_default() {
  ip -6 -j route show table all | python3 -c 'import json,sys; sys.exit(any(r.get("dst")=="default" and r.get("type","unicast")=="unicast" for r in json.load(sys.stdin)))' ||
    fail 'Native IPv6 default route found: IPv4-only interception would leave IPv6 outside the policy'
}
collisions() {
  local table chain
  for item in 'filter MHM_PORTS' 'nat MHM_DNS' 'mangle MHM_TPROXY'; do
    read -r table chain <<<"$item"
    if "${IPT[@]}" -t "$table" -S "$chain" >/dev/null 2>&1; then fail "Unowned chain already exists: $chain"; fi
  done
  if ip rule show | grep -q "^${PREF}:"; then fail 'Routing priority 10010 is in use'; fi
  [[ -z $(ip route show table "$TABLE" 2>/dev/null) ]] || fail 'Routing table 104 is in use'
  # This bit must not overlap any existing mark operation or policy match mask.
  { iptables-save; ip rule show; } | python3 -c '
import re,sys
for line in sys.stdin:
 for flag,token in re.findall(r"(--(?:set-xmark|set-mark|mark|nfmask|ctmask)|fwmark)\s+(\S+)",line):
  parts=token.split("/")
  mask=int(token,0) if flag in ("--nfmask","--ctmask") else (int(parts[1],0) if len(parts)==2 else 0xffffffff)
  if mask & 0x400:
   sys.exit("Existing firewall/policy mark overlaps reserved bit 0x400")
'
}
preflight() {
  for tool in iptables iptables-save ip conntrack modprobe python3 ss curl systemctl flock; do command -v "$tool" >/dev/null || fail "Missing tool: $tool"; done
  load_scope "$ROOT/routing.env"
  local iface
  for iface in "${LAN_INTERFACES[@]}"; do ip link show dev "$iface" >/dev/null || fail "LAN bridge is missing: $iface"; done
  ip -4 -j addr show | python3 -c 'import json,sys; expected=sys.argv[1]; sys.exit(not any(a.get("local")==expected for i in json.load(sys.stdin) for a in i.get("addr_info",[])))' "$DNS_LISTEN_IPV4" || fail 'DNS_LISTEN_IPV4 is not an address owned by this router'
  python3 - "$ROOT/config.yaml" "$DNS_LISTEN_IPV4" <<'PYDNS'
import json,sys
inside=False;listen=None
for line in open(sys.argv[1]):
 if line.rstrip()=="dns:":inside=True;continue
 if inside and line.strip() and not line.startswith((" ","#")):break
 if inside and line.strip().startswith("listen:"):
  value=line.split(":",1)[1].strip()
  listen=json.loads(value) if value.startswith('"') else value.strip("'")
assert listen==sys.argv[2]+":1053", "DNS listener and routing scope disagree"
PYDNS
  no_ipv6_default
  if [[ -f "$STATE/scope" ]]; then
    cmp -s "$STATE/scope" "$ROOT/routing.env" || fail 'Scope changed while running; stop before editing routing.env'
  fi
  if [[ ! -f "$STATE/owned" ]]; then
    collisions
    if ss -H -lntu | awk '{print $4}' | grep -Eq ':(1053|7890|7893|9090)$'; then
      fail 'A required listener port is already in use'
    fi
  fi
  "$ROOT/mihomo" -t -d "$ROOT" >/dev/null 2>&1 || fail 'Mihomo configuration rejected'
}
remove_hook() {
  local table=$1 chain=$2; shift 2
  while "${IPT[@]}" -t "$table" -C "$chain" "$@" 2>/dev/null; do "${IPT[@]}" -t "$table" -D "$chain" "$@"; done
}
remove_chain() {
  if "${IPT[@]}" -t "$1" -S "$2" >/dev/null 2>&1; then
    "${IPT[@]}" -t "$1" -F "$2"
    "${IPT[@]}" -t "$1" -X "$2"
  fi
}
detach() {
  [[ -f "$STATE/owned" && -f "$STATE/scope" ]] || return 0
  load_scope "$STATE/scope"
  local iface cidr proto
  for iface in "${LAN_INTERFACES[@]}"; do
    for cidr in "${SOURCE_CIDRS[@]}"; do
      match_scope "$iface" "$cidr"
      remove_hook nat PREROUTING "${MATCH[@]}" -j MHM_DNS
      remove_hook mangle PREROUTING "${MATCH[@]}" -j MHM_TPROXY
      remove_hook filter INPUT "${MATCH[@]}" -m mark --mark "$MARK" -j ACCEPT
      # Remove only the DNS NAT mappings created by this deployment (reply source port 1053).
      for proto in tcp udp; do
        conntrack -D -f ipv4 -p "$proto" -s "$cidr" --dport 53 --reply-src "$DNS_LISTEN_IPV4" --reply-port-src 1053 >/dev/null 2>&1 || true
      done
    done
  done
  remove_chain nat MHM_DNS
  remove_chain mangle MHM_TPROXY
  if [[ -f "$STATE/route-owned" ]]; then
    while ip rule del priority "$PREF" fwmark "$MARK" table "$TABLE" 2>/dev/null; do :; done
    ip route del local 0.0.0.0/0 dev lo table "$TABLE" 2>/dev/null || true
    rm -f "$STATE/route-owned"
  fi
  rm -f "$STATE/attached"
}
cleanup() {
  [[ -f "$STATE/owned" ]] || return 0
  detach
  local proto
  for proto in tcp udp; do remove_hook filter INPUT -p "$proto" --dport 1053 -j MHM_PORTS; done
  remove_chain filter MHM_PORTS
  rm -f "$STATE/owned" "$STATE/scope"
}
rule() {
  local table=$1 chain=$2; shift 2
  "${IPT[@]}" -t "$table" "$RULE_OP" "$chain" "$@"
}
port_rules() {
  rule filter MHM_PORTS -i lo -j ACCEPT || return
  local iface cidr
  for iface in "${LAN_INTERFACES[@]}"; do
    for cidr in "${SOURCE_CIDRS[@]}"; do match_scope "$iface" "$cidr"; rule filter MHM_PORTS "${MATCH[@]}" -j ACCEPT || return; done
  done
  rule filter MHM_PORTS -j DROP || return
}
traffic_rules() {
  rule mangle MHM_TPROXY -m addrtype --dst-type LOCAL -j RETURN || return
  local net proto
  for net in 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.168.0.0/16 224.0.0.0/4 240.0.0.0/4; do
    rule mangle MHM_TPROXY -d "$net" -j RETURN || return
  done
  for proto in tcp udp; do
    rule mangle MHM_TPROXY -p "$proto" --dport 53 -j RETURN || return
    rule mangle MHM_TPROXY -p "$proto" -j TPROXY --on-ip 127.0.0.1 --on-port 7893 --tproxy-mark "$MARK" || return
    rule nat MHM_DNS -p "$proto" -m addrtype --dst-type LOCAL --dport 53 -j DNAT --to-destination "$DNS_LISTEN_IPV4:1053" || return
  done
}
guard() {
  preflight
  if [[ -f "$STATE/owned" ]]; then check_guard || fail 'Listener guard changed; stop before restarting'; return 0; fi
  cp "$ROOT/routing.env" "$STATE/scope"; chmod 600 "$STATE/scope"
  touch "$STATE/owned"
  trap 'cleanup' ERR
  "${IPT[@]}" -t filter -N MHM_PORTS
  RULE_OP=-A; port_rules
  local proto
  for proto in tcp udp; do "${IPT[@]}" -t filter -I INPUT 1 -p "$proto" --dport 1053 -j MHM_PORTS; done
  trap - ERR
}
health() {
  [[ -f "$STATE/scope" ]] || return 1
  load_scope "$STATE/scope"
  systemctl is-active --quiet mihomo.service || return 1
  # A process alone is not proof that DNS and both transparent transports work.
  local socket
  for socket in '127.0.0.1:7890' '127.0.0.1:7893' "$DNS_LISTEN_IPV4:1053"; do
    ss -H -lnt | awk '{print $4}' | grep -Fxq "$socket" || return 1
  done
  for socket in '127.0.0.1:7893' "$DNS_LISTEN_IPV4:1053"; do
    ss -H -lnu | awk '{print $4}' | grep -Fxq "$socket" || return 1
  done
  curl --noproxy '' --proxy http://127.0.0.1:7890 --connect-timeout 3 --max-time 8 --fail --silent --output /dev/null https://www.gstatic.com/generate_204
}
attach() {
  [[ -f "$STATE/owned" ]] || fail 'Run guard before attach'
  [[ -f "$STATE/wanted" ]] || fail 'Activation must be requested through 20-mihomo.sh start'
  systemctl is-active --quiet mihomo-rollback.timer || fail 'Automatic rollback timer is required'
  preflight
  check_guard || fail 'Listener guard is missing or reordered'
  health || fail 'Proxy readiness probe failed; keeping original routing'
  if [[ -f "$STATE/attached" ]]; then detach; fi
  # Never overwrite a table/priority created by something else since guard.
  if ip rule show | grep -q "^${PREF}:"; then fail 'Routing priority collision'; fi
  [[ -z $(ip route show table "$TABLE" 2>/dev/null) ]] || fail 'Routing table collision'
  local chain table iface cidr
  for item in 'nat MHM_DNS' 'mangle MHM_TPROXY'; do
    read -r table chain <<<"$item"
    if "${IPT[@]}" -t "$table" -S "$chain" >/dev/null 2>&1; then fail "Unexpected chain: $chain"; fi
  done
  trap 'detach' ERR
  modprobe xt_TPROXY
  "${IPT[@]}" -t mangle -N MHM_TPROXY
  "${IPT[@]}" -t nat -N MHM_DNS
  RULE_OP=-A; traffic_rules
  touch "$STATE/route-owned"
  ip route add local 0.0.0.0/0 dev lo table "$TABLE"
  ip rule add priority "$PREF" fwmark "$MARK" table "$TABLE"
  for iface in "${LAN_INTERFACES[@]}"; do
    for cidr in "${SOURCE_CIDRS[@]}"; do
      match_scope "$iface" "$cidr"
      "${IPT[@]}" -t filter -I INPUT 3 "${MATCH[@]}" -m mark --mark "$MARK" -j ACCEPT
      "${IPT[@]}" -t nat -I PREROUTING 1 "${MATCH[@]}" -j MHM_DNS
      "${IPT[@]}" -t mangle -I PREROUTING 1 "${MATCH[@]}" -j MHM_TPROXY
    done
  done
  touch "$STATE/attached"
  trap - ERR
}
check_guard() {
  [[ -f "$STATE/owned" && -f "$STATE/scope" ]] || return 1
  load_scope "$STATE/scope"
  RULE_OP=-C; port_rules || return 1
  local expected=$((2 + ${#LAN_INTERFACES[@]} * ${#SOURCE_CIDRS[@]}))
  [[ $("${IPT[@]}" -t filter -S MHM_PORTS | grep -c '^-A ') -eq "$expected" ]] || return 1
  # Presence is insufficient if a UniFi rewrite inserts ACCEPT ahead of a guard.
  "${IPT[@]}" -t filter -S INPUT | python3 -c '
import shlex,sys
rules=[shlex.split(x) for x in sys.stdin if x.startswith("-A ")][:2]
seen=set()
for r in rules:
 for proto in ("tcp","udp"):
  expected=["-A","INPUT","-p",proto,"-m",proto,"--dport","1053","-j","MHM_PORTS"]
  short=["-A","INPUT","-p",proto,"--dport","1053","-j","MHM_PORTS"]
  if r in (expected,short):seen.add(proto)
sys.exit(seen != {"tcp","udp"})
' || return 1
  local proto
  for proto in tcp udp; do "${IPT[@]}" -t filter -C INPUT -p "$proto" --dport 1053 -j MHM_PORTS || return 1; done
}
check_rules() {
  [[ -f "$STATE/attached" ]] || return 1
  load_scope "$STATE/scope"
  RULE_OP=-C; port_rules || return 1; traffic_rules || return 1
  local iface cidr proto
  for proto in tcp udp; do "${IPT[@]}" -t filter -C INPUT -p "$proto" --dport 1053 -j MHM_PORTS || return 1; done
  for iface in "${LAN_INTERFACES[@]}"; do
    for cidr in "${SOURCE_CIDRS[@]}"; do
      match_scope "$iface" "$cidr"
      "${IPT[@]}" -t filter -C INPUT "${MATCH[@]}" -m mark --mark "$MARK" -j ACCEPT || return 1
      "${IPT[@]}" -t nat -C PREROUTING "${MATCH[@]}" -j MHM_DNS || return 1
      "${IPT[@]}" -t mangle -C PREROUTING "${MATCH[@]}" -j MHM_TPROXY || return 1
    done
  done
  ip rule show | grep -Eq '^10010:.*fwmark 0x400/0x400.*lookup 104$' || return 1
  ip route show table "$TABLE" | grep -Eq '^local default dev lo' || return 1
}
case "${1:-status}" in
 preflight) preflight ;;
 guard) guard ;;
 attach) attach ;;
 detach) detach ;;
 cleanup) cleanup ;;
 health) health ;;
 check) check_rules ;;
 check-guard) check_guard ;;
 status) systemctl is-active mihomo.service || true; systemctl list-timers mihomo-rollback.timer --no-pager; [[ ! -f "$STATE/scope" ]] || { load_scope "$STATE/scope"; printf 'scope=%s bridges=%s\n' "$SCOPE" "${LAN_INTERFACES[*]}"; }; if [[ -f "$STATE/attached" ]]; then echo 'routing=attached'; else echo 'routing=direct'; fi ;;
 *) fail 'Usage: mihomo-routing.sh preflight|guard|attach|detach|cleanup|health|check|status' ;;
esac
