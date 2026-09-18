# Deployment and admin-page acceptance

## Original-console deployment — 2026-09-18

The UDM SE was installed from commit `529b3a7` on UniFi OS 5.1.33. The
installation is `/data/mihomo`; source staging is `/data/mihomo-source-529b3a7`.
The earlier `/data/ax-mac-proxy` pilot expired before installation and remains
available as historical evidence, without active packet hooks.

Only the operator's wired Mac is configured. The device IPv4, MAC and LAN bridge
are in the private `routing.env`; no all-device scope was activated. Proxy
routing is **stopped** at handoff and the remembered mode is **smart/rule**.
This is acceptance state, not a promise of future uptime; read the admin page
or systemd for current status.

`mihomo-admin.service` is running on **127.0.0.1:9088 only**. It is separate from
the stopped proxy service. Neither service was enabled for reboot persistence.
A local SSH forward makes the page available at <http://127.0.0.1:9088> on the
operator Mac. Reconnect using the README command if that forward ends. The
admin token is stored privately on the UDM and in the operator credential
vault; no credentials are published in this repository.

## Actual acceptance

- All 43 automated regression checks passed, including authentication, same
  origin, request limits, action allowlists, expiry preservation, scope safety,
  installer rollback and uninstall ordering. Hosted CI passed for `529b3a7`.
- Real browser tests covered login, desktop/mobile layout, master toggle, all
  three modes, failed actions, uncertain state, escaped node labels and logout.
- A real Mihomo engine in an isolated UDM network namespace verified listener
  addresses, subscription bootstrap, rejection with missing bootstrap DNS, and
  `GLOBAL -> PROXY` selection before mode activation.
- The live browser controlled the UDM: smart mode used the CL exit for foreign
  traffic and the home connection for a China-domain IP check; global mode made
  that China-domain check use the CL exit too. Mode changes preserved the
  original automatic-stop deadline.
- Selecting direct stopped the engine and restored the original public exit.
  A reused UDP DNS socket still resolved after stopping, verifying that the
  old DNS translation was removed rather than left pointing at the stopped
  listener.
- A subsequent browser enable/disable cycle passed and left smart mode as the
  next default. The independent acceptance safety timer was stopped after
  cleanup, so it cannot unexpectedly terminate a later user-started trial.
- Final checks found no `MHM_`/`AXMAC_` packet hooks or `0x400` policy route, while
  the authenticated loopback admin page remained available.

The current native IPv4 rules were also compared with the older pre-pilot
snapshot. Differences were confined to existing UniFi `ALIEN` jump ordering
and its logging prefix. Project scripts do not edit those chains; those
native differences were left intact instead of overwriting the live firewall
with an older snapshot. A byte-for-byte restoration of that old snapshot is
therefore **not** claimed.

## Access and recovery

Start a configured trial through the admin page, or use:

```sh
/data/mihomo/20-mihomo.sh start 60 rule
/data/mihomo/20-mihomo.sh stop
```

All public internet mode changes remain within the configured scope. Native
IPv6 internet, all-LAN throughput, reboot/firmware persistence and connectivity
from mainland China have not been accepted by this deployment.

## Live telemetry — 2026-09-18

Admin telemetry files were updated from commit `5526c6f`. Only the administrator
service was restarted; proxy credentials, route scope, mode and lifecycle units
were preserved. Previous admin files are in the root-only backup directory
`/data/mihomo/backups/telemetry-5526c6f`.

The authenticated `/api/telemetry` endpoint uses one background collector and
five minutes of RAM-only history. Rates are bytes per second; CPU 100% is one
core; memory is the Mihomo process RSS. These are engine-handled counters,
including direct connections and background work, not whole-router WAN usage.

Acceptance included 62 regression tests and successful hosted CI, plus desktop
and mobile browser checks of the graph, missing/stale values and session gaps.
A live controlled transfer produced six fresh rate samples, positive transfer
counts and active connections; process memory and CPU were populated. The
browser displayed the real history. The authenticated responses contained no
connection addresses, destinations or metadata. After testing, the original
stopped/direct state was restored and live values became null.

A deterministic delayed-response regression also verified that a slow engine
response cannot produce a false high-speed spike on the next sample. Slow reads,
process restarts, counter resets and gaps discard the rate baseline until two
fresh samples are available. No traffic history is written to a database or
exported to an external monitoring service.

## Old-console cleanup before replacement — 2026-09-18

AX confirmed this was still the old UDM SE and requested cleanup before powering
it down for replacement and eventual resale. The full System Config Backup,
Network-only backup and private custom-service archive were verified locally;
the explicitly unwanted InnerSpace exclusion was accepted. A fresh archive also
captured the retired pilot and source staging before deletion.

The owned installer marker, uninstall-script checksum and installed systemd unit
references were checked. The supported uninstaller removed the active fork;
the known retired pilot, source/telemetry staging and runtime state were then
removed. The old Mac SSH forward was closed. Local credential-vault backups
were retained for restoring the new console.

Post-cleanup verification confirmed native IPv4/IPv6 firewall rules, policy
routes and DNS configuration matched the immediate pre-cleanup baseline.
Custom listener ports, unit definitions, packet hooks and owned data paths were
absent. UniFi Network and the console service remained active; ordinary DNS and
internet access worked.

**Old-console boundary:** custom cleanup did not factory-reset the console.
AX subsequently powered it off and moved the connections to the replacement.
Its native UniFi configuration still needs the final factory reset and
account-association check before resale. This is scoped application cleanup, not a claim of
forensic secure erasure of the device.


## Replacement-console acceptance — 2026-09-18

AX restored the System Config Backup onto the replacement UDM SE, running
UniFi OS 5.1.33. A changed console MAC and SSH host key confirmed the hardware
change; the new key was pinned after the physical cutover was confirmed. AX
confirmed both switches, all three APs and the power strip were online. The
three original LAN networks, native DNS and internet access were present.

The private custom-service archive passed its recorded SHA-256 check. Active
scripts, administrator assets and unit definitions matched this repository.
Only the owned installation was restored: retired pilot files, source staging,
old runtime state and native firewall files were excluded. Mihomo's configuration
check passed before the administrator was started. The existing administrator
token, subscription, device scope and selection cache were preserved.

Live acceptance on the operator Mac verified smart routing, global mode, direct
mode and DNS after stopping. The original China IP-query endpoint returned HTTP
500 even with the proxy stopped; a second endpoint successfully verified the
same routing behavior. A controlled transfer populated live rates, connection
counts, process CPU/RSS and the browser history chart. Stopping returned live
metrics to null. The final check found no custom packet hooks or policy route.

The administrator is available through the Mac's loopback SSH forward at
<http://127.0.0.1:9088>. The final proxy state is **direct/stopped**, with smart
mode remembered for the next trial. Scope remains **only the configured Mac**;
neither the engine nor administrator was enabled for reboot persistence. UniFi
Network, the console service and the loopback administrator were active at
acceptance. The original console remains powered off pending its final reset.


### Mac dashboard tunnel recovery

The original background SSH forward later exited while the UDM administrator
remained healthy. On 2026-09-18 it was replaced by the operator Mac's user
LaunchAgent `ai.ax.udm-mihomo-dashboard`, listening only on `127.0.0.1:9088`.
It starts at login and launchd reconnects it after exit, with a 30-second retry
throttle and SSH keepalives. Authentication reads the existing credential vault
through an owner-only SSH askpass helper; no password is stored in the plist.
Both the dashboard HTML and authenticated status API returned HTTP 200 after
recovery. This manages only the Mac tunnel; it does not enable UDM service boot
persistence or change proxy routing scope.


### Mac automatic login and theme — 2026-09-18

The operator selected automatic login on this Mac and explicitly chose a manual
administrator password. The UDM credential was updated privately; its authenticated
loopback API and separate engine secret remain in place. The Mac SSH LaunchAgent
now forwards local port 9089 to UDM port 9088. A second user LaunchAgent,
`ai.ax.udm-mihomo-dashboard-bridge`, runs the repository's `local-dashboard.py`
on local port 9088. Both restart through launchd. The bridge reads the vault
credential and exchanges a per-process local bearer with the browser; it does
not disclose the UDM password. Other local users/processes are within the trusted
Mac boundary. Strict Host, Origin, fetch-metadata, route and request checks reject
cross-site operation and arbitrary forwarding.

The dashboard automatically connects on this Mac. Direct UDM access retains
manual login. Theme defaults to the operating system, with an accessible
light/dark toggle that remembers only the theme in browser storage. The bridge
and UDM versions passed focused authentication/boundary tests, installer checks,
and real-browser automatic/manual login and control tests. Live acceptance
verified automatic login, the new manual password, dark-theme persistence and
rejection of unauthenticated/cross-origin requests. Routing remained direct.
Private credential and prior deployment snapshots were retained for rollback.
