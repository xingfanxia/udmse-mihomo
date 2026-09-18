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
