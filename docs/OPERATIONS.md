# Deployment and admin-page acceptance

## Verified deployment — 2026-09-18

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
