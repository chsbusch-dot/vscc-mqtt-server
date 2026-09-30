# vscc on the .212 app host

Production home of the VSCC patient-monitor charting stack since the 2026-09-30
migration off the parked VM 242 (`.188`). Classified in homelab-infra ADR-0002 as
bucket A: containerized, GHCR images, LAN only, patient data stays local.

| | |
|---|---|
| URL | `https://vscc.lan.synviron.com`, via caddy-lan and **Authelia** `one_factor` (the app has no login of its own) |
| Host | `prd-ubu-apphost` `192.168.1.212`, compose project `vscc` |
| Stack dir on the box | `/opt/apphost/vscc` (a `git archive` of this repo, `.deployed-commit`) |
| Compose | `docker-compose.yml` (base, the public install) + `compose.production.yml` (overlay) |
| Host ports | **none**. Only `vscc-dashboard` joins the external `edge` network |
| Database | TimescaleDB 2.19.3-pg14, data dir bind-mounted from `/srv/vscc-data/timescaledb` (its own thin VMDK, homelab-owned, fstab by UUID, `nofail`) |
| Session exports | `/srv/vscc-data/sessions` (same disk) |
| On the root disk | the capture export files (`vscc-data` volume, trimmed hourly by the capture container) and EMQX state (`vscc-emqx-data`) |
| Monitor | Philips MP50 at `192.168.1.215`, captured by `vscc-capture` (VSCaptureCLI, LAN mode) |
| Secret | BWS `VSCC_POSTGRES_PASSWORD`, piped at deploy, never on disk |

## How a request flows

```
browser --https--> caddy-lan --forward_auth--> Authelia
                      |
                      +--edge--> vscc-dashboard (nginx, deploy/dashboard-nginx.conf)
                                   /             static app
                                   /api/*        -> vscc-worker:8000   (REST: sessions, settings, exports)
                                   /mqtt         -> vscc-emqx:8083     (MQTT over WebSocket, live data)
                                   /DataExportVSC.json, /ws/stream -> vscc-streamer:8000

MP50 --UDP--> vscc-capture --export files (vscc-data)--> vscc-worker --> EMQX + TimescaleDB
```

The dashboard builds same-origin URLs whenever it is served over https
(vscc-dashboard-client `src/utils/backendUrls.ts`). That keeps every request
behind the one SSO gate and avoids mixed content.

**Capture networking.** Bridge networking is enough. VSCapture's LAN mode is
client-initiated UDP: the association goes to the monitor's port 24105 and the
data comes back on the same flow, so Docker's NAT/conntrack carries the replies.
The monitor is on-link from `.212` (same `/24` on `ens32`, no router hop, so no
inter-VLAN firewall in the path). Host networking is not needed.

**MQTT 1883 is not published.** The worker reaches EMQX on the stack network and
the dashboard uses the WebSocket through the gate. Nothing on the LAN needs raw
MQTT, and the broker is anonymous, so publishing `1883` would expose live vital
signs to every LAN host past the SSO gate and past ufw (Docker-published ports
bypass it). If a LAN consumer ever needs it, publish it deliberately with
authentication and register it in homelab-infra `network/ports.md`.

## Deploy / update (from the Mac)

```bash
~/VSCode/vscc-mqtt-server/deploy/vscc-deploy.sh            # origin/main
```

It ships the tree, then `deploy/vscc-up.sh` runs on the box. The pre-flight
refuses (exit 3) when `/srv/vscc-data` is not mounted, a data dir is missing, or
no database exists yet. Then it runs `pull` and `up -d`, and waits until
TimescaleDB and the worker report healthy (exit 4 otherwise).

**Images** are pinned by digest in `compose.production.yml`. To roll one
forward, replace the digest (the CI `Publish images` workflow pushes `latest`;
read the new digest from GHCR), open a PR, merge, redeploy.

**Rollback:** `/opt/apphost/vscc.prev` holds the previous tree:
`ssh chris@192.168.1.212 'cd /opt/apphost && mv vscc vscc.bad && mv vscc.prev vscc'`,
then run `deploy/vscc-up.sh` there with the password piped as in `vscc-deploy.sh`.

## Retention and disk

The worker re-applies `vscc_settings.retention_hours` as TimescaleDB retention
policies on every start. The dashboard's Settings panel changes it. A
retention job drops whole chunks older than the window. The data disk was sized
at about 2x the restored history, so watch it:
`ssh chris@192.168.1.212 df -h /srv/vscc-data`. If it fills, only vscc stops.

## Backups

homelab's restic job on `.212` takes a `pg_dump` of the running database
(consistent) instead of copying the live data directory; see homelab-infra
`backups/apphost-212.md`.

## Checks

```bash
ssh chris@192.168.1.212 'docker compose -p vscc ps; df -h /srv/vscc-data; docker inspect vscc-timescaledb --format "{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}"'
curl -sI https://vscc.lan.synviron.com/ | head -1      # 302 to auth.lan.synviron.com without a session
```
