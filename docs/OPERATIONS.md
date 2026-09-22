# Operations

## Running the stack

```bash
docker compose up -d --build     # start, or rebuild after any code/playbook change
docker compose ps
docker compose logs -f web worker
docker compose stop              # stop (data is kept in volumes)
docker compose down              # remove containers (volumes kept)
```

Code and playbooks are baked into the image, so **editing a file changes nothing until you rebuild**.

**Before rebuilding, check the Tasks page for running deploys.** Recreating the worker kills a deploy that is mid-flight. Its droplet keeps running and is never configured. On the next start the worker marks such tasks and half-provisioned sensors as failed, but the droplet is only removed by its TTL or by you.

`docker compose exec` skips the container's entrypoint, so SSH tried by hand through `exec` fails with "No user exists for uid" (the entrypoint is what gives the runtime UID a passwd entry). Test through the app instead.

## Deploying a sensor

1. **Admin:** *Verify token*, then *Sync options from DigitalOcean* (regions, sizes and images come from a local cache; nothing calls DigitalOcean on page load).
2. **Deploy:** choose the sensor type, region, size and TTL. Use a short TTL (1–2 h) while testing. The size defaults to the type's recommendation; see [Sensor sizing and Logstash](#sensor-sizing-and-logstash).
3. Watch the launch dialog, or close it and follow the run on **Tasks**. A deploy takes about 5 minutes: create, SSH, Docker install, sensor stack, health checks.
4. A warning that the sensor **cannot reach the Hive** means the firewall isn't admitting its IP yet (see [PALO_ALTO_EDL.md](PALO_ALTO_EDL.md)). The sensor runs and records, but cannot ship logs.

Reach a sensor from the Hive host (the DigitalOcean firewall allows admin SSH only from the Hive's IP):

```bash
ssh -i secrets/ssh_key -p 64295 root@<sensor-ip>
docker ps; docker logs logstash --tail 50
```

## Health

```bash
curl -s http://127.0.0.1:8880/api/droplets/<id>/health              # ping + TCP checks
curl -s 'http://127.0.0.1:8880/api/droplets/<id>/health?deep=true'  # also runs the health playbook
```

## Destroying sensors

Use **Fleet**, or `DELETE /api/droplets/<id>?deregister_hive=true&sensor_name=<login>`. Destroying removes the droplet, its lease, its EDL entry and (when asked) its Hive login. A sensor is also destroyed automatically when its TTL expires.

## Hive logins

Every sensor has its own login in the Hive's `lswebpasswd` and `.env` (`LS_WEB_USER`). **Admin → T-Pot Hive** lists them and marks each *in use* or *not in fleet*. Logins that are not in the fleet are leftovers (for example from droplets deleted in the DigitalOcean console) and can be removed with the *Remove* button.

To reset the T-Pot web password (`/sensors/` uses T-Pot's login):

```bash
ENTRY=$(printf '%s\n' 'NewPassword' | htpasswd -i -m -n <user>)
echo "$ENTRY" > "$TPOT_HOST_DIR/data/nginx/conf/nginxpasswd"
docker exec nginx nginx -s reload
```

## Where things live

| What | Where |
| :--- | :--- |
| Database (fleet, leases, campaigns, settings, option cache) | `deployer_data` volume, `/data/tpot.db` |
| Campaign log | `/data/logs/scheduler.log` |
| EDL file | `/data/sensors.txt` (served at `/edl/sensors.txt`) |
| Terraform working dir (GCP) | `/data/terraform/gcp` |
| Hive login files | `$TPOT_HOST_DIR/data/nginx/conf/lswebpasswd` and `$TPOT_HOST_DIR/.env` |
| Deployer keys | `secrets/ssh_key`, `secrets/ssh_key.pub` (host, gitignored) |

Back up the volume:

```bash
docker run --rm -v tpot-sensor-deployer_deployer_data:/data -v "$PWD":/backup alpine \
  tar czf /backup/deployer-data.tgz -C /data .
```

## Troubleshooting

| Symptom | Likely cause and fix |
| :--- | :--- |
| Deploy fails immediately with `No user exists for uid 1000` | The container is not running through its entrypoint. Rebuild and start with `docker compose up -d --build`. |
| `Timeout when waiting for <ip>:64295` | The worker can't reach the admin port. The DigitalOcean firewall allows it only from the Hive's IP, so the worker's outbound IP must match `hive_ip`. Compare `curl https://api.ipify.org` on the host with the app's Hive IP. |
| `tpotinit` restarts: `TPOT_PERSISTENCE is not set` | The sensor `.env` is missing a variable. The template is `playbooks/templates/tpot.env.j2`; a test checks it. |
| Deploy stops at *Start sensor stack* | Read the container logs on the sensor (`docker logs tpotinit`, `docker logs logstash`). |
| Sensor is `active` but no logs reach the Hive | See [No logs in the Hive](#no-logs-in-the-hive). |
| Task stuck on *running* after a restart | Fixed on the next worker start (see above). |
| A campaign keeps failing and is suspended | The circuit breaker tripped after 3 failures. Read `/data/logs/scheduler.log`, fix the cause, then resume it. |
| Dropdowns on Deploy are empty | Reload the page; if still empty, *Sync options* on Admin. |

### No logs in the Hive

Work from the sensor towards the Hive:

1. **Is the sensor's Logstash running?** `docker ps` should show it `Up` without repeated restarts, and `docker logs logstash | grep "Pipeline started"` should show the `http_output` pipeline. A container can report "healthy" while its JVM is stuck after an out-of-memory error, so read the log.
2. **Can the sensor reach the Hive?** From the sensor: `timeout 6 bash -c '</dev/tcp/<hive-ip>/64294' && echo reachable`.
3. **Is the firewall admitting the sensor?** The Palo Alto only admits IPs it has fetched from the EDL. Check that it is polling: `grep '"request_uri": "/edl/sensors.txt"' "$TPOT_HOST_DIR/data/nginx/log/access.log" | tail -5` should show a fetch about every 5 to 7 minutes from the firewall's address (`src_ip`, empty user agent; browsers and `curl` show one). A new sensor's IP is admitted after the first fetch that follows its creation.
4. **Does the sensor's login exist on the Hive?** It should be listed on **Admin → T-Pot Hive**.

## Sensor sizing and Logstash

Every sensor runs T-Pot's Logstash shipper next to the honeypot. Logstash is a JVM application and by far the largest process on a sensor; the honeypot itself uses under 100 MB.

**The stock pipeline does not fit.** T-Pot's sensor pipeline (`http_output.conf`) is the Hive's: one file for every honeypot (28 file inputs, dozens of filters). Started on a 4 GB test droplet at T-Pot's own 1 GB heap it died with `java.lang.OutOfMemoryError: Java heap space` after about two minutes and never shipped a log, while Docker still reported the container healthy.

**The trimmed pipeline does.** The deployer renders a per-sensor pipeline with only the sensor's own inputs and filters (see [PLAYBOOKS.md](PLAYBOOKS.md#the-trimmed-logstash-pipeline)) and no IP-reputation lookup. Measured with the same image:

| Pipeline | Heap | Result | Heap in use | Container RAM |
| :--- | :--- | :--- | :--- | :--- |
| Stock | 1024m | out of memory after ~2 min | n/a | 1.3 GB |
| Cowrie-only | 512m | starts in ~50 s, events flowing | 33 % | 785 MB |
| Cowrie-only | 384m | starts | 67 % | 688 MB |
| Cowrie-only | 256m | starts | 72 % | 549 MB |

The default is a **512m heap and a 1 GB limit** (`logstash_heap`, `logstash_mem_limit` in `playbooks/defaults.yml`). On a real 2 GB droplet a Cowrie sensor ran stable with Logstash at ~660 MB and about 880 MB of RAM free, and its events reached the Hive's Elasticsearch within seconds. The multi-sensor runs several honeypots and needs a 4 GB droplet.

The health check waits for the pipeline to be running, and fails on a heap error or a restart loop.

The GCP path still builds Logstash with the old, undersized settings and needs the same change.
