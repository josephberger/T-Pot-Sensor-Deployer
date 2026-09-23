# Architecture

## 1. Components

The deployer is three containers defined in `docker-compose.yml`, running next to a T-Pot Hive:

| Service | Role |
| :--- | :--- |
| `web` | FastAPI app (uvicorn, port 8880): the UI, the REST API, the EDL feed, and the SSE log stream |
| `worker` | RQ worker plus a scheduler thread: runs deploys and destroys, ticks campaigns every 10 s and sweeps TTL leases every 60 s |
| `redis` | Job queue and live task logs. Not published to the host (the Hive already runs a Redis honeypot on 6379) |

Everything is baked into one image. The only host paths mounted are inputs: the Hive's `tpotce` directory at `/tpot` (read/write, to register sensors) and an optional `secrets/` folder at `/secrets` (read-only). All state lives in the `deployer_data` volume at `/data`.

```
 browser ─► T-Pot nginx :64297 ─► web :8880 ──► redis ──► worker ──► DigitalOcean / GCP API   (create / destroy / list options)
              /sensors/  /edl/       │                       │
                                     ▼                       ├──► Ansible over SSH ──► sensor
                                SQLite /data                 │                            │
                                                             └──► Hive files (/tpot)      ▼
                                                                  lswebpasswd, .env    Logstash ──► Hive :64294
```

The containers run as `PUID:PGID` (plus the `tpot` group) so the Hive's files stay writable. That UID has no `/etc/passwd` entry, and OpenSSH refuses to run without one, so `docker-entrypoint.sh` synthesizes an entry with `libnss-wrapper`.

### Code map

| Module | Responsibility |
| :--- | :--- |
| `app/api.py` | Routes, page routes, SSE stream |
| `app/models.py` | Pydantic models: `Settings`, `Sensor`, API request payloads |
| `app/config.py` | Paths (from `DATA_DIR`, `TPOT_DIR`, `SECRETS_DIR`) and settings load/save |
| `app/db.py` | SQLite access; the only state store |
| `app/jobs.py`, `app/queue_manager.py`, `worker.py` | RQ jobs, task status and logs, worker startup |
| `app/do_client.py` | DigitalOcean API: create/destroy droplets, firewall, tags, option sync |
| `app/gcp_client.py` | GCP Compute Engine API: create/destroy instances, firewall rules, zone/machine-type/image listing |
| `app/cloud.py` | `get_client(provider)`: the one place that picks a DO or GCP client for a row |
| `app/ansible_runner.py`, `app/provisioning.py`, `app/health.py` | Run playbooks, the shared configure-and-verify step, health checks |
| `app/hive_manager.py` | Hive detection, per-sensor logins, registration in `lswebpasswd` and the Hive `.env` |
| `app/scheduler_manager.py` | Campaigns (DigitalOcean only; GCP campaigns are still the legacy Terraform path, see §7) |
| `app/ttl_manager.py` | Lease expiry and destroy (both providers) |
| `app/edl_manager.py` | The Palo Alto feed |
| `app/sensor_types.py` | Sensor metadata (ports, playbook-driven catalog) |
| `app/provisioner.py`, `app/gcp_manager.py`, `terraform/gcp/` | The older cloud-init/Terraform path, still used only by GCP *campaigns* (§7) |

## 2. Ports

| Port | Where | Purpose |
| :--- | :--- | :--- |
| 64297 | Hive | T-Pot nginx: T-Pot UI, the deployer at `/sensors/` (basic auth), the EDL at `/edl/` (no auth) |
| 8880 | Deployer host, Docker bridge IP (`172.17.0.1`) | The `web` container; T-Pot's nginx proxies to it |
| 64294 | Hive | Sensor log ingestion (TLS plus per-sensor basic auth) |
| 64295 | Sensors | Admin SSH; the DigitalOcean firewall restricts it to the Hive's IP |
| honeypot ports | Sensors | Open to the internet per sensor type |

The deployer ships no proxy of its own. T-Pot's nginx routes `/sensors/` to the web container and passes `/edl/` through without a login to the addresses in `EDL_ALLOW`, so a firewall can poll it (see `nginx/tpot-location.conf`). T-Pot's nginx is on its own Docker network, so it reaches the web container through the host's Docker bridge IP, which is where `WEB_BIND` publishes it. Its config and landing page are baked into its read-only image, so `tpot-expansion-pack.sh` generates both from the image's stock copies, writes them to `$TPOT_DATA_PATH/nginx/conf/`, and bind-mounts them into T-Pot's nginx service. The frontend adapts to the `/sensors/` prefix at runtime (`getAppPath` in the templates, `apiUrl` in `app.js`).

## 3. Deploy pipeline (manual: DigitalOcean and GCP)

The cloud API creates and destroys machines. Everything on the machine is cloud-agnostic. The same steps
run for both providers' manual deploys (`run_do_deployment_job` / `run_gcp_deployment_job` in `app/jobs.py`)
and for DigitalOcean campaign cycles (§4; GCP campaigns are not on this pipeline yet, see §7).

1. **Preflight**, before anything is created: the sensor type has playbooks, `ansible-playbook` exists, the deployer key pair is present, and (GCP) a project id and service account key are configured.
2. **Deployer key**: `secrets/ssh_key.pub` is uploaded to DO if missing and always attached (or, for GCP, put directly into the new instance's `ssh-keys` metadata as `tpotadmin:<key>` - GCP images don't allow direct root SSH the way DO's do). A droplet created without a key makes DigitalOcean email a root password, and could not be configured anyway.
3. **Tags and firewall**: DO tags are created if missing; `tpot-sensor-firewall` (DO) or the `tpot-sensor-*` rules (GCP, on a dedicated `tpot-sensor-vpc` network created on demand - see §7) open the honeypot ports and restrict admin SSH (64295) to the Hive IP.
4. **Hive login**: a per-sensor login (`sensor-<type>-<adjective>-<noun>`, 32-character password) is written to the Hive's `lswebpasswd` and `.env` (`LS_WEB_USER`). One login per sensor means one sensor can be revoked without touching the others. It is removed again if the cycle fails.
5. **Create a bare machine** (no user-data / no startup script), then record it (status `provisioning`, `provider` set) and its TTL lease immediately, so a failure can never leave an untracked, billing machine. Recording it also **publishes its IP to the EDL right away**: the firewall can only admit the sensor to the Hive after it has fetched a feed that lists it, and configuring takes minutes.
6. **Configure** with `playbooks/sensors/<type>.yml` and **verify** with `playbooks/health/<type>.yml` (see [PLAYBOOKS.md](PLAYBOOKS.md)) - as `root` for DO, as `tpotadmin` for GCP (every playbook already runs with `become: true`, so this is just which account logs in). If the sensor is healthy but cannot reach the Hive yet, wait up to 8 minutes (`HIVE_ADMIT_WAIT_SECONDS`) for the firewall's next refresh; after that it is a warning, not a failure.
7. Mark the sensor `active`.

A sensor whose setup fails is kept for debugging with status `provision_failed`; its TTL still destroys it.

**Statuses:** `provisioning`, `active`, `provision_failed`.

**TTL:** a lease destroys the machine at expiry (DO by droplet id, GCP by instance name + zone), removes its Hive login and its EDL entry.

**Worker restarts:** on startup the worker marks any task left `running` by a previous worker, and any half-provisioned manual sensor, as failed. This assumes a single worker, as in `docker-compose.yml`.

## 4. Campaigns

Campaigns recurring "run for a window, destroy, rebuild later on a fresh IP" so scanners (Shodan, Censys) can't fingerprint a stable address. Sensors always get a fresh dynamic IP; reserved or floating IPs are never used.

- **Timing modes:** weekly, interval, and random window (a random cooldown between a minimum and maximum). Presets: weekly 24 h, 8 h shift with a 48–72 h random rebuild, daily 6 h, Dionaea 48 h, Conpot weekend, Elasticpot. Only sensor types with playbooks can be used in a campaign, for either provider.
- **Region rotation** cycles DO rebuilds through a pool of regions. There's no GCP zone-rotation equivalent yet - the campaign modal hides "Rotate regions" when GCP is selected.
- **State machine**, ticked every 10 s by the worker: `cooling_down → provisioning → active → tearing_down → cooling_down`. Both providers share it; only how `provisioning` gets from "dispatched" to "has an IP" differs (below).

**A DigitalOcean cycle:**
1. Dispatch runs the same preflight, key, firewall and Hive-login steps, then creates a bare droplet (`stage: waiting_ip`).
2. When the droplet has an IP, the tick hands it to a background thread (`stage: configuring`) that runs the same configure-and-verify step, so the tick never blocks on a multi-minute playbook.
3. On success the campaign goes `active` and the teardown is scheduled (the IP was already published when the droplet got its IP). On failure the droplet is destroyed, its Hive login removed, and the failure feeds the circuit breaker. A configure interrupted by a worker restart is detected and cleaned up.

**A GCP cycle** (on the same create-then-Ansible pipeline as manual GCP deploys, `app/gcp_client.py`):
1. Dispatch runs the same preflight/firewall/Hive-login steps inline, then hands the whole rest of the
   cycle to one background thread (`_async_gcp_provision_worker`) and goes straight to `stage: configuring`
   - there's no DO-style non-blocking create + poll-for-IP step, because `GCPClient.create_instance`
   already blocks until the instance exists with an IP, and that can't happen inside the 10 s tick.
2. The thread creates the instance, records it (publishing its IP the same way DO does), then runs the
   same configure-and-verify step DO uses, as `tpotadmin` instead of `root` (GCP images don't allow direct
   root SSH), then joins the same "ready" wait-loop DO's configure thread uses to hand off to
   `_finalize_active`/`_abort_provisioning` - shared code, not a parallel implementation.
3. Teardown (`_execute_destroy`) calls `GCPClient.destroy_instance(name, zone)` directly and synchronously
   - a GCP delete is a single ~10-20 s API call, not a multi-minute `terraform destroy`, so unlike
   create/configure it doesn't need its own background thread.

**Safeguards:** at most 3 cycles provisioning at once; a 30-minute provisioning timeout; a circuit breaker after 3 consecutive failures (campaign suspended); exponential backoff between retries (5, 10, 20 minutes). The tick writes back only the schedules it changed, and all scheduler instances in a process share one lock, because background threads write state too.

**TTL backstop lease.** A campaign's only lifecycle control is normally its own `next_action_at` timer; if
the tick ever stops advancing it (a crashed worker, a bug, a campaign paused while its sensor is active),
nothing else would destroy the droplet - unlike a manual deploy, which always gets an independent TTL
lease. Campaign droplets get one too now, at five points in `app/scheduler_manager.py`: scheduled for
30-45 minutes the moment a droplet/instance exists (covers "worker dies mid-configure"); extended to
`active_duration + 24h` once active; cancelled on any real teardown; extended further to 30 days if the
campaign is paused while active (so a brief pause can't get caught by the 24h buffer); and reality-checked
on resume against the actual `active_droplets` table, so a campaign whose sensor turned out to be gone
while paused drops into `cooling_down` instead of resuming into a phantom active state with no destroy
timer ever set again.

## 5. Tasks and live logs

Deploys and destroys run as RQ jobs. Each publishes log lines to a Redis list (kept 24 h, replayable) and a pub/sub channel. `GET /api/deploy/stream/{id}` streams them as Server-Sent Events: it replays history, then follows live. Only a **terminal** status (`completed` or `failed`) ends the stream; intermediate updates such as `running` are progress. The launch dialog can be closed at any time; the job continues and its log stays available on the Tasks page. Raw Ansible output is `debug` level and shown only in the terminal pane, never as the status headline.

## 6. State

SQLite in WAL mode (`/data/tpot.db`) is the only state store; there are no JSON state files.

| Table | Holds |
| :--- | :--- |
| `active_droplets` | Tracked sensors (status, IP, TTL, the sensor's Hive login, `provider`) |
| `leases` | TTL leases (`provider`, and `region` carrying the GCP zone when relevant) |
| `schedules` | Campaigns (timing, config and runtime state as JSON) |
| `static_ips` | Static EDL entries |
| `fleet_history` | Sensor lifecycle events |
| `settings` | UI-editable settings (validated by `models.Settings`) |
| `cloud_regions`, `cloud_sizes`, `cloud_images`, `cloud_ssh_keys`, `cloud_tags`, `cloud_metadata_sync` | Cached DigitalOcean options; refreshed only on demand |

Fleet listings (`/api/droplets`) read SQLite only and never call a cloud API, for either provider. `/api/sensors` is a separate, live-queried view (DO + GCP APIs) used where up-to-the-second state matters more than avoiding a call. Terraform's own `tfvars.json` and `tfstate` (used only by the legacy GCP campaign path, §7) are the only JSON on disk, because Terraform requires them.

## 6.1 Security notes

- Cloud credentials are files in the read-only `/secrets` mount, never settings, env vars or database rows: the DO token is `secrets/do_token` (`config.get_do_token()` reads it on every call) and GCP auth is `secrets/gcp-sa.json`. Neither can be set from the UI or sent to the browser; `config.public_settings()` exposes only a `do_token_set` flag and the file path. A token stored in the database by older versions is deleted at web startup (`purge_stored_do_token`).
- Each sensor has its own Hive login. The password is held only long enough to configure the sensor and is stripped from API output.
- The `/edl/` feed has no login by design (so a firewall can poll it), but T-Pot's nginx serves it only to the addresses in `EDL_ALLOW`. It lists sensor IPs only.
- The deployer's private key is mounted read-only from `secrets/` and copied to a 0600 temp file per run.
- Tests point at temporary data and Hive directories so they can never modify the real Hive.

## 7. GCP

**Manual deploys** (`/api/deploy` with `provider: "gcp"`, or the GCP tab on the Deploy page) are on the
same create-then-Ansible pipeline as DigitalOcean (§3): `app/gcp_client.py` only creates/destroys/lists
Compute Engine instances, a dedicated network, and firewall rules (official `google-cloud-compute` SDK,
auth via the service account at `secrets/gcp-sa.json` through `GOOGLE_APPLICATION_CREDENTIALS`), and
everything after that - Ansible, health checks, TTL leases, Hive registration, the EDL - is the exact
code DO already uses. GCP instances address by *name* (not the numeric id DO uses), so
`active_droplets`/`leases` rows carry a `provider` column and `region` doubles as the zone for GCP rows.

**Network**: sensors get a dedicated custom-mode VPC (`tpot-sensor-vpc`) and one subnet per region
(`tpot-sensor-vpc-<region>`), created on demand by `ensure_sensor_network`/`ensure_network` the first
time they're needed - never the project's auto-created "default" network, which plenty of real projects
don't have at all (deleted, or blocked by org policy; this is exactly what broke the first real deploy
attempt against this pipeline). Firewall rules are network-scoped and get created first
(`ensure_sensor_firewall` calls `ensure_network`), the region's subnet is created lazily in
`create_instance` right before the instance itself. The network is shared infrastructure for the whole
GCP fleet, so nothing destroys it automatically when a single sensor is destroyed; `DELETE
/api/gcp/network` (Admin page) tears it down by hand, refusing while any GCP sensor is still tracked.

One real difference from DO, handled in `app/provisioning.py`/`app/health.py`/`app/ansible_runner.py`:
GCP images don't allow direct root SSH. The instance is created with a fixed login user
(`tpotadmin`, `app/gcp_client.py`'s `GCP_SSH_USER`) via instance metadata instead, and every playbook
already runs with `become: true`, so no playbook content needed to change - only which account Ansible
connects as (`remote_ssh_user`, threaded through `configure_and_verify`/`deep_check`/`run_playbook`).
A short post-boot wait (`wait_for_ssh_auth`) covers the GCP guest agent needing a few seconds after boot
to actually install that user's key, which a plain "is the port open" check doesn't catch.

**Campaigns are not on this pipeline yet.** `app/scheduler_manager.py`'s `provider == "gcp"` branch
still uses the older Terraform + startup-script path (`terraform/gcp/`, `app/gcp_manager.py`,
`app/provisioner.py` - kept in the repo for exactly this reason, and still exercised by
`tests/test_multi_sensors.py`). It has the same undersized Logstash settings the old manual path had
(see [OPERATIONS.md](OPERATIONS.md#sensor-sizing-and-logstash)) and the gaps CLAUDE.md's "Known gaps"
section already documents (no TTL backstop, `_abort_provisioning` is a no-op for a GCP cycle). Moving
campaigns onto the new pipeline is future work.
