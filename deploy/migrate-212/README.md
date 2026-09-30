# One-off: moving VSCC off VM 242 onto .212 (2026-09-30)

Chris's decisions (2026-09-30): copy the TimescaleDB history; move the capture too;
give the database its own thin disk on `.212`, mounted at `/srv/vscc-data`. Homelab
owns every VM, disk and power step; this repo only asks for them.

## Why VM 242 was never booted

The worker installs TimescaleDB retention policies on `patient_numerics` and
`patient_waveforms`. The default is 12 h, stored in `vscc_settings.retention_hours`,
and the worker re-applies them on every start (`vscc_mqtt_timescale_worker.py`,
`SCHEMA` and `lifespan`). On VM 242 the database ran in Docker with
`restart: unless-stopped`. A normal boot would start it, and the TimescaleDB
scheduler runs an overdue job immediately, so `drop_chunks` would have removed
every chunk older than the window within seconds. The VM had been off since at
least 2026-09-22, so that meant the history this migration exists to copy.

So homelab cloned VM 242's only disk while the VM was off (`vmkfstools -i`, thin,
on `DS2_R740_1TB_NVME`). The clone was attached as a LINKED disk to the `.135`
helper, so every write lands in a throwaway delta and the clone stays pristine;
242's own disk was never touched. The same trap applies on `.212`, which is why
the restore runs with background workers off and sets the retention before the
worker ever starts.

## The steps

| # | Where | What |
|---|---|---|
| 1 | homelab | cold clone of VM 242's disk; attach to `.135` as a linked disk |
| 2 | `.135`, root | `sudo bash extract-from-clone.sh /dev/sdX` (Chris types the sudo password): positive ID of the clone, `vgimportclone`, read-only mount, cold PGDATA tarball + capture config + inventory + `SHA256SUMS` |
| 3 | `.135`, chris | `bash stage-dump.sh ~/vscc-extract`: scratch TimescaleDB on the cold copy with `timescaledb.max_background_workers=0` and no network. Writes the sizes/rows/min-max manifest and `pg_dump -Fc` |
| 4 | Mac | copy the extract and dump to the Mac staging folder (LAN only, not a cloud-synced path), verify the checksums, delete the copy on `.135`; homelab detaches the clone |
| 5 | homelab | thin VMDK of about 2x the measured size on `DS2_R740_1TB_NVME`, ext4, `/srv/vscc-data`, fstab by UUID with `nofail` |
| 6 | Mac -> `.212` | `RETENTION_HOURS=<n> restore-to-212.sh <staging>`: fresh initdb into `/srv/vscc-data/timescaledb` with bgworkers off, `timescaledb_pre_restore()`, streamed `pg_restore`, `timescaledb_post_restore()`, retention set (it refuses a value that would purge restored rows), per-table rows and min/max time compared with the source manifest |
| 7 | Mac | `deploy/vscc-deploy.sh`: the full stack comes up |
| 8 | homelab | caddy route, Authelia rule, DNS repoint, restic `pg_dump`, `ports.md`, `vmmap.json` |
| 9 | Chris | MP50 plugged in: new rows in TimescaleDB, live values in the dashboard |

The capture needs nothing from the VM except its settings. The same VSCaptureCLI
arguments (`-waveset 12 -scale 2 -interval 1 -export 4 -devid mp50`) are the
capture image's defaults, and the inventory from step 2 records the VM's units and
binaries so they can be compared.

## Patient data rules

The tarballs, the dump and the manifest's time ranges are PHI or derived from it.
They stay on the LAN (the `.135` helper only transiently, the Mac staging folder
until Chris has seen the history on `.212`). Never paste rows or dumps into a chat,
and never send them to a cloud model.
