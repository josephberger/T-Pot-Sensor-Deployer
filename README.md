# T-Pot Sensor Deployer

A control console for deploying **[T-Pot](https://github.com/telekom-security/tpotce) honeypot sensors to the cloud** and shipping their logs into your T-Pot **Hive**. It creates a machine, configures it over SSH with Ansible, checks its health with standard tools, registers it with the Hive, publishes its IP to a Palo Alto EDL, and destroys it when its lease expires. Campaigns rebuild sensors on fresh IPs on a schedule so scanners can't fingerprint them.

<p align="center"><img src="docs/images/dashboard.png" alt="Dashboard" width="100%"></p>

The cloud API is used only to **create and destroy machines** (and to list options like regions and sizes). Everything that happens on a machine afterwards is cloud-agnostic: plain SSH, Ansible playbooks and ping/TCP checks. Adding another cloud means adding a way to make and delete a machine; the sensor setup and health checks don't change. **DigitalOcean and GCP** are both fully wired up today — two providers a home-lab operator can actually get an account on in five minutes, no business verification required.

> **Status:** DigitalOcean deploys and campaigns work end to end for all 12 catalog sensor types (see [docs/HONEYPOT_CATALOG.md](docs/HONEYPOT_CATALOG.md)), each deployed and health-checked on a real droplet. GCP manual deploys and campaigns use the same create-then-Ansible pipeline as DigitalOcean and are verified the same way for **Cowrie**; the rest should work (identical shared code) but aren't each individually confirmed on a real GCP instance yet. AWS and Azure are out of scope for now.

## How it works

```
 you (browser) ──► T-Pot nginx :64297 /sensors/ ──► web (FastAPI) ──► redis ──► worker
                                                        │                        │
                                                        │              ┌─────────┴───────────┐
                                                        ▼              ▼                     ▼
                                                  SQLite (/data)   DO / GCP API         Ansible over SSH
                                                                   create / destroy      to the sensor
                                                                                              │
   Palo Alto EDL  ◄── /edl/sensors.txt        T-Pot Hive  ◄── Logstash (TLS + basic auth) ◄───┘
```

1. **Register** the sensor's own log-shipping login in the Hive, then **create** a bare machine (no user-data / startup script) with the deployer's SSH key attached.
2. **Track** it immediately (database record and TTL lease) so a failed setup can never leave an untracked, billing machine, and **publish its IP** to the EDL so the firewall can admit it while it is still being set up.
3. **Configure** it with `playbooks/sensors/<type>.yml`: swap, Docker, the T-Pot sensor stack, the Hive certificate, and admin SSH moved off port 22.
4. **Check** it with `playbooks/health/<type>.yml`: containers running and not crash-looping, Logstash pipeline up, honeypot ports listening, Hive reachable (waiting a bounded time for the firewall to admit it).
5. **Destroy** it when the TTL expires (or on demand), removing its Hive login and EDL entry.

The **worker** process does all of this in the background, including campaign scheduling (every 10 s) and TTL sweeps (every 60 s). The web UI only enqueues work and shows progress.

## Deploy to either cloud

The Deploy page is one form with a cloud tab, not two different tools — pick a sensor type once, then DigitalOcean and GCP show the same fields (name, region/zone, size/machine type, auto-destroy, count, firewall toggles) so switching providers is a tab click, not a different mental model.

<p align="center">
  <img src="docs/images/deploy-digitalocean.png" alt="Deploy - DigitalOcean" width="49%">
  <img src="docs/images/deploy-gcp.png" alt="Deploy - GCP" width="49%">
</p>

Both **Admin** panels look the same way for the same reason — DigitalOcean's token status and GCP's service account/project status side by side, plus the deployer's SSH key and the Hive's registered sensor logins:

<p align="center"><img src="docs/images/admin.png" alt="Admin" width="100%"></p>

## Campaigns

A campaign runs a sensor for a window, destroys it, and rebuilds it later on a fresh dynamic IP — weekly, on a fixed interval, or a randomized rebuild window — so scanners like Shodan or Censys can't fingerprint a stable address. Campaign machines get an independent TTL backstop lease too, the same safety net a manual deploy always had, so a stuck campaign (or one paused while its sensor is active) can never turn into a forgotten, indefinitely-running bill.

<p align="center"><img src="docs/images/campaign-new.png" alt="New campaign" width="70%"></p>

## Firewall integration

Every tracked sensor's IP is published as a plaintext [External Dynamic List](docs/PALO_ALTO_EDL.md) a Palo Alto (or any EDL-capable) firewall can poll directly — no separate sync step.

<p align="center"><img src="docs/images/edl.png" alt="EDL" width="100%"></p>

## Requirements

- Docker with Compose, on the same host as a T-Pot **[Hive](https://github.com/telekom-security/tpotce)** (the deployer writes to the Hive's `tpotce` directory to register sensors).
- A DigitalOcean API token (read and write), and/or a GCP project with a service account key (Compute Admin role) — deploy to either or both.
- An SSH key pair for the deployer. The private key configures sensors; the public key is uploaded to DigitalOcean and injected into GCP instance metadata.
- A Palo Alto (or other) firewall that admits sensor IPs to the Hive's log port `64294` (see [docs/PALO_ALTO_EDL.md](docs/PALO_ALTO_EDL.md)).

## Quick start

On the Hive, with T-Pot installed and running:

```bash
./tpot-expansion-pack.sh
```

On a first run it sets up everything the deployer needs, then hooks it into T-Pot's nginx (see [Behind T-Pot's nginx](#behind-t-pots-nginx-default)):

- **`.env`**: created from `.env.example`. It fills in what it can work out: `PUID`/`PGID` from the owner of your T-Pot directory, `TPOT_GID` from T-Pot's nginx config folder, `WEB_BIND` (the Docker bridge IP) and the defaults. It prompts for:
  - the path to your T-Pot install (defaults to `~/tpotce`);
  - `EDL_ALLOW`, your firewall's address. It suggests any address it has already seen polling `/edl/` with an empty user agent, the way a Palo Alto does;
- **`secrets/do_token`**: your DigitalOcean API token, hidden while you type and checked against the DigitalOcean API. Enter skips it if you only use GCP. If an older `.env` still has `DO_TOKEN`, the script moves it into this file and removes it from `.env`. The token can't be set from the web UI.
- **`secrets/ssh_key` + `ssh_key.pub`**: the key Ansible logs in to sensors with. It offers the passphrase-less private keys in `~/.ssh` to copy (or another path), or generates a new ed25519 key. With no usable key in `~/.ssh`, it generates one. It checks that the private and public halves match.
- **`secrets/gcp-sa.json`** (optional): asks for the path to your downloaded GCP service account key file, rather than having the JSON pasted into the terminal. It checks the file is a service account key, copies it in and sets `GCP_PROJECT_ID` from it. Enter skips it if you only use DigitalOcean.
- **Permissions**: `.env`, `secrets/`, the private key and the GCP key are made owner-only (`600`/`700`). They hold your cloud credentials.
- **Starts the deployer**: builds and starts it (`docker compose up -d --build`) if it isn't running, or restarts it when settings or secrets changed. It won't restart the worker while a deploy is running.

On later runs it only prompts for keys missing from `.env` (for example new ones added to `.env.example`), and it never replaces an existing key. Options:

| Option | What it does |
| :--- | :--- |
| `--dry-run` | Show what would change without changing or asking anything |
| `--setup` | Ask the optional questions again: replace the DigitalOcean token, `EDL_ALLOW`, GCP key |
| `--no-prompt` | Never prompt: use defaults (and `~/tpotce`), generate an SSH key if there is none, fail if something required is missing |

The console is at `https://<hive>:64297/sensors/` behind T-Pot's own login, and linked from the Hive landing page at `https://<hive>:64297/`.

Open **Admin**, click *Verify token* and *Sync options* (DigitalOcean), or set a project id and drop a service account key at `secrets/gcp-sa.json` and click *Test connection* (GCP), then use **Deploy**.

Code is baked into the image, so after any change run `docker compose up -d --build`.

## Behind T-Pot's nginx (default)

The deployer ships no web server or login of its own; it reuses the Hive's. T-Pot's nginx on port `64297` already serves Kibana, the Attack Map and the rest behind a password, and [`tpot-expansion-pack.sh`](tpot-expansion-pack.sh) adds the deployer to it:

| Route | Login | Goes to |
| :--- | :--- | :--- |
| `/sensors/` | T-Pot's web login | the deployer UI and API |
| `/edl/` | none, but only from the addresses in `EDL_ALLOW` | the Palo Alto EDL feed |
| `/` | T-Pot's web login | the Hive landing page, now with a *Sensor Deployer* link |

What the script does, each step idempotent:

1. Reads `TPOT_HOST_DIR` from `.env`, and `TPOT_DATA_PATH` from T-Pot's own `.env`.
2. Copies the **stock** `tpotweb.conf` and landing page `index.html` out of the installed T-Pot nginx image, so it always starts from the version you actually run, never from a stale saved copy.
3. Writes `$TPOT_DATA_PATH/nginx/conf/tpotweb.conf`: the stock server block plus the routes in [`nginx/tpot-location.conf`](nginx/tpot-location.conf), with the upstream set to the Docker bridge IP and `WEB_PORT`, and one `allow` line per `EDL_ALLOW` entry.
4. Writes `$TPOT_DATA_PATH/nginx/conf/index.html`: the stock landing page with a *Sensor Deployer* link added as the last entry of the tools box.
5. Adds two read-only bind mounts for those files to the `nginx` service in `$TPOT_HOST_DIR/docker-compose.yml`.
6. Sets `WEB_BIND` in `.env` to the Docker bridge IP (usually `172.17.0.1`).
7. Runs `nginx -t` on the new config in a throwaway container **before** changing anything, then recreates T-Pot's nginx (or just reloads it), restarts the deployer if `WEB_BIND` changed, and checks that nginx can reach the deployer, that `/sensors/` asks for a login, and that the landing page has the link. If the running nginx rejects the config, it restores the backups.

Every file it changes is backed up next to itself as `*.bak-<timestamp>`. `--dry-run` shows the diffs without changing anything.

**Re-run it after every T-Pot update.** `update.sh` can replace T-Pot's `docker-compose.yml`, which drops the mounts, and a new release can ship a new stock config or landing page. After a fresh T-Pot install, start T-Pot once, then run the script.

To undo it, remove the two `tpotweb.conf` / `index.html` mount lines from the `nginx` service in `$TPOT_HOST_DIR/docker-compose.yml`, then `sudo systemctl restart tpot`. nginx then falls back to the config and landing page in its image.

### Why it's done this way

These are the things that go wrong if you add the routes by hand:

- **Editing the config inside the nginx container doesn't work.** T-Pot's nginx container is `read_only`, and its `tpotweb.conf` and landing page are baked into the image; nothing mounts them from the host. Edits inside the container fail, and even when they don't, T-Pot recreates every container on each start (`docker compose down` / `up` in `tpot.service`), so they are lost. The only change that sticks is a file on the host mounted over the one in the image.
- **`proxy_pass http://127.0.0.1:8880` can't reach the deployer.** T-Pot's nginx is on its own Docker network (`tpotce_nginx_local`), not host networking, so `127.0.0.1` inside it is the nginx container itself. It reaches the host through the Docker bridge IP (docker0, usually `172.17.0.1`). That's why `WEB_BIND` publishes the web container there: the port is reachable from containers on the host but not from other machines.
- **`auth_basic off` alone doesn't open `/edl/`.** T-Pot's server block has `satisfy any; allow 127.0.0.1; deny all;` plus a password, meaning "localhost, or anyone with the password". Turning the password off in `/edl/` leaves only the `deny all`, so the firewall gets `403`. Each address that may fetch the EDL needs its own `allow` line, which `EDL_ALLOW` provides. Prefer your firewall's address over `EDL_ALLOW=all`, since the EDL lists every sensor's IP.
- **The landing page isn't a host file either.** It's `/var/lib/nginx/html/index.html` inside the image. There is no `data/nginx/conf/index.html` for `install.sh` to create or for you to edit until the script adds one.

## Standalone (without T-Pot's nginx)

To reach the web app directly at `http://<host>:8880/`, swap the `ports:` line of the `web` service in [`docker-compose.yml`](docker-compose.yml) for the commented `0.0.0.0` line under it, don't run `tpot-expansion-pack.sh` (it resets `WEB_BIND` to the bridge IP), and run `docker compose up -d`.

**The app has no login of its own.** Behind T-Pot's nginx, T-Pot's password protects it; standalone, anyone who can reach the port can create and destroy cloud machines on your account and read the EDL. Only publish it where a firewall limits the port to you, or on a trusted network. The deployer still needs `TPOT_HOST_DIR` to register sensors with a Hive either way.

## Hive landing page

T-Pot's splash page at `https://<hive>:64297/` gets a *Sensor Deployer* link in its tools box (next to Attack Map, Kibana, Spiderfoot and the rest). `tpot-expansion-pack.sh` handles it; see [hive-landing-page/README.md](hive-landing-page/README.md).

## Running a Hive without honeypots (optional)

If the Hive host is only a Hive (the sensors in the cloud do the catching), you can stop T-Pot starting its local honeypots and keep the web UI, nginx, Elastic stack, Attack Map and Spiderfoot. Add `profiles: ["honeypots"]` under each honeypot service in `$TPOT_HOST_DIR/docker-compose.yml`. Compose skips a service with a profile unless you ask for that profile, so `tpot.service` starts only the rest. Also include the `tanner*` services (snare's backend). Remove stopped honeypot containers with `docker rm`, or Docker's `restart: always` can start them again at boot.

To start the honeypots again, remove those lines, or run `docker compose --profile honeypots up -d` in `$TPOT_HOST_DIR`. Like the nginx mounts, a T-Pot update can overwrite this. The NSM tools (Suricata, p0f, Fatt) aren't honeypots and keep running either way.

## Configuration

Set in `.env` (read by Compose):

| Variable | Purpose |
| :--- | :--- |
| `TPOT_HOST_DIR` | **Required.** Host path of the T-Pot install; mounted at `/tpot` |
| `PUID` / `PGID` / `TPOT_GID` | UID/GID the containers run as; must be able to write `$TPOT_HOST_DIR/data/nginx/conf` and `.env` |
| `WEB_BIND` / `WEB_PORT` | Published address/port of the web container (default `172.17.0.1:8880`, the Docker bridge; `tpot-expansion-pack.sh` sets `WEB_BIND` to the real bridge IP) |
| `EDL_ALLOW` | Addresses/CIDRs allowed to fetch `/edl/` without a login, comma-separated (your firewall). Empty means only the Hive itself. Applied by `tpot-expansion-pack.sh` |
| `SECRETS_HOST_DIR` | Folder mounted read-only at `/secrets` (default `./secrets`) |
| `GCP_PROJECT_ID` | GCP project id (can also be saved from the UI). Auth is `secrets/gcp-sa.json`, not a token |
| `TPOT_HIVE_IP` | Public IP/FQDN of the Hive (auto-detected if blank) |
| `HIVE_ADMIT_WAIT_SECONDS` | How long a deploy waits for the firewall to admit a new sensor to the Hive (default 480) |

Secrets are files in `secrets/`, mounted read-only at `/secrets`, never `.env` variables: those end up in the containers' environment, where `docker inspect` shows them.

| File | What it is |
| :--- | :--- |
| `do_token` | DigitalOcean API token (read + write). The only place the app reads it from, on every use, so replacing the file needs no restart. It can't be set from the UI |
| `ssh_key` / `ssh_key.pub` | Deployer SSH key pair (private key without a passphrase) |
| `gcp-sa.json` | GCP service account key, Compute Admin role |

`tpot-expansion-pack.sh` creates them and sets their permissions. You can also put them there yourself: `do_token`, `ssh_key` and `gcp-sa.json` should be `600`, and readable by `PUID`. The folder is gitignored and kept out of the image.

All state (SQLite database, settings, logs, EDL file, Terraform state) lives in the `deployer_data` Docker volume at `/data`.

## Pages

| Page | What it does |
| :--- | :--- |
| **Dashboard** | Summary cards and live previews |
| **Deploy** | Pick a sensor type, provider, region/zone, size and TTL, then launch |
| **Fleet** | Active sensors, TTL controls, health, destroy |
| **Campaigns** | Recurring "run, destroy, rebuild on a new IP" schedules, either provider |
| **EDL** | The Palo Alto feed, static entries, PAN-OS notes |
| **Tasks** | Worker status, task history, replayable live logs |
| **Admin** | DigitalOcean token / GCP project status, option sync, the deployer SSH key, Hive status and registered sensor logins |
| **Help** | What each thing does, how a deploy works, firewall setup and troubleshooting (a static page, `app/static/help.html`) |

## Documentation

- [Architecture](docs/ARCHITECTURE.md): components, deploy pipeline, campaigns, state, ports
- [Playbooks](docs/PLAYBOOKS.md): how sensors are configured and checked, and how to add a sensor type
- [Operations](docs/OPERATIONS.md): running it, day-to-day tasks, troubleshooting
- [Sensor catalog](docs/HONEYPOT_CATALOG.md): the supported sensor types and their status
- [Palo Alto EDL](docs/PALO_ALTO_EDL.md): firewall integration
- [CLAUDE.md](CLAUDE.md): notes for anyone (human or AI) changing the code

## Tests

```bash
python3 tests/test_sqlite_db.py
python3 tests/test_multi_sensors.py
python3 tests/test_gcp_client.py
```

Each test file points the app at temporary data and Hive directories before importing it, so tests can never touch real state. `tests/test_worker_queue.py` needs a live Redis: `docker compose exec web python3 tests/test_worker_queue.py`.

## License

[MIT](LICENSE)
