# Playbooks: configuring and checking sensors

Sensors are configured and checked by Ansible playbooks in [`playbooks/`](../playbooks). They know nothing about the cloud: they run against an IP over SSH. The worker runs them through `app/ansible_runner.py`, using the shared step in `app/provisioning.py`, for both manual deploys and campaigns.

## Layout

```
playbooks/
  ansible.cfg              host-key checking off, pipelining, no retry files
  defaults.yml             defaults shared by every playbook (ports, T-Pot version, Logstash sizing)
  sensors/<type>.yml       deploy a sensor            (one small file per sensor type)
  health/<type>.yml        check a sensor             (one small file per sensor type)
  tasks/sensor_base.yml    shared deploy steps
  tasks/health_base.yml    shared health steps
  templates/
    tpot.env.j2                    the sensor's T-Pot .env
    docker-compose.yml.j2          tpotinit + logstash + the type's honeypot service(s)
    logstash_http_output.conf.j2   the sensor's trimmed Logstash pipeline
```

A sensor type is **supported** when it has both `sensors/<type>.yml` and `health/<type>.yml`. The deployer refuses to create a droplet for a type without them, and refuses to create a DigitalOcean campaign for it.

All 12 catalog types exist: `cowrie`, `dionaea`, `conpot`, `elasticpot`, `mailoney`, `heralding`, `ciscoasa`, `citrixhoneypot`, `redishoneypot`, `sentrypeer`, `adbhoney`, `multi_sensor` (Cowrie + Dionaea + ElasticPot + Mailoney on one machine). Each has been deployed on a real droplet and confirmed to ship events to the Hive. `multi_sensor` needs `s-2vcpu-4gb`; the rest run on the `s-1vcpu-2gb` default. `conpot` deploys only the `conpot_default` template (Modbus, S7, EtherNet/IP, BACnet, SNMP) — T-Pot's compose also ships `IEC104`, `guardian_ast` and `kamstrup_382` as separate containers, not included here.

## What a deploy does (`tasks/sensor_base.yml`)

1. **Validate** the required variables (`hive_ip`, `tpot_hive_user`, `sensor_name`).
2. **Swap** file (size from `swap_size_gb`).
3. **Docker** Engine and the Compose plugin from Docker's apt repository.
4. **T-Pot user and directories**: group and user `tpot` (uid/gid 2000, plus the `docker` group), `/opt/tpotce`, `/data` and the type's directories.
5. **Hive certificate** to `/data/hive.crt` (the sensor's Logstash verifies the Hive with it).
6. **Render** the `.env`, the trimmed Logstash pipeline and `docker-compose.yml`.
7. **Move admin SSH off port 22** (the honeypot needs 22), in two phases so a mistake can't lock the playbook out:
   1. sshd listens on **both** 22 and the admin port (64295); the play reconnects on the admin port and proves it works;
   2. port 22 is dropped and the play checks it is free.

   This works whether sshd is socket-activated (`ssh.socket` active) or a plain service. The mode is detected from the socket's *active* state, because the unit file exists on every Ubuntu 24.04 image. The socket override lists `0.0.0.0:PORT` and `[::]:PORT` explicitly, because the packaged unit sets `BindIPv6Only=ipv6-only` and a bare `ListenStream=PORT` would be IPv6-only.
8. **Start** the stack: `docker compose pull && docker compose up -d`.

The playbooks are idempotent. The runner connects on the admin port if the machine is already relocated, otherwise on 22.

## What a health check does (`tasks/health_base.yml`)

- **Logstash pipeline is running.** Waits up to 4 minutes for the pipeline to be up rather than trusting the container's own healthcheck, which reports "healthy" for a JVM that has crashed with an `OutOfMemoryError`.
- **No heap crash.** Fails if Logstash's log contains `OutOfMemoryError: Java heap space`.
- **Containers stable.** `tpotinit`, `logstash` and the honeypot containers must be `running` with fewer than 2 restarts.
- **Honeypot ports listening** on the sensor.
- **Hive reachable from the sensor** (`hive_ip:hive_port`). This is a **warning only**: whether a sensor may reach the Hive is decided by a firewall the deployer does not control (see [PALO_ALTO_EDL.md](PALO_ALTO_EDL.md)). The sensor is still marked active, and the warning is shown in the task log or campaign log. Until the firewall admits the sensor's IP, it records attacks but cannot ship them.
- **Disk** under 90 %.

`GET /api/droplets/{id}/health` runs a cheap ping and TCP check; add `?deep=true` to run the health playbook too.

## Variables

Provided by the runner as extra vars: `hive_ip`, `hive_port`, `hive_cert_content`, `ssl_verification`, `tpot_hive_user` (the base64 `user:password` for this sensor's Hive login), `sensor_name`, `swap_size_gb`, `admin_ssh_port`, `ansible_port`. Defaults live in `defaults.yml`; secrets go through a temporary 0600 vars file, never the command line.

Set by each `sensors/<type>.yml`:

| Variable | Meaning |
| :--- | :--- |
| `sensor_type` | The type id (`cowrie`) |
| `honeypot_dirs` | Directories to create under `/data` |
| `honeypot_networks` | Extra compose networks |
| `honeypot_services` | The honeypot's compose service block(s) |
| `logstash_inputs` | The log files this sensor ships (`path`, `type`) |
| `logstash_filter` | The type's Logstash filter block |

Set by each `health/<type>.yml`: `honeypot_containers` and `honeypot_ports`.

## The trimmed Logstash pipeline

T-Pot's stock sensor pipeline (`http_output.conf`) handles **every** honeypot: 28 file inputs and dozens of filters, because the Hive runs them all. A single sensor only needs its own inputs and filters, so `logstash_http_output.conf.j2` renders just those, plus T-Pot's common steps (drop parse failures, add the sensor's hostname and IPs, GeoIP for the attack map, integer conversions) and the HTTP output to the Hive. It leaves out the **IP-reputation lookup** (`ip_rep`): a sensor only records what happens, and the Hive is the SIEM.

## Adding a sensor type

1. Find the honeypot's service definition in T-Pot's own `compose/tpot_services.yml` (mounted on the deployer host at `/tpot`). Copy its service block, volumes and networks.
2. Find its input and filter blocks in the Hive's Logstash `http_output.conf`.
3. Copy `sensors/cowrie.yml` to `sensors/<type>.yml` and change `sensor_type`, `honeypot_dirs`, `honeypot_networks`, `honeypot_services`, `logstash_inputs`, `logstash_filter`.
4. Copy `health/cowrie.yml` to `health/<type>.yml` and set `honeypot_containers` and `honeypot_ports`.
5. Add the type to the tests (see `test_trimmed_logstash_pipeline`) and run `ansible-playbook --syntax-check`.
6. Deploy one for real with a short TTL before trusting it. The failures so far (missing `tpot` group, missing `ssh.socket.d` directory, IPv6-only socket, missing `TPOT_PERSISTENCE`, undersized Logstash) were all found only on real machines.

## Testing playbook changes without a droplet

- `ansible-playbook --syntax-check -i x, playbooks/sensors/<type>.yml`.
- Render the templates with Jinja and parse the compose YAML (see `tests/test_multi_sensors.py`).
- Run individual sections against a throwaway Ubuntu SSH container, or against a systemd container (`jrei/systemd-ubuntu:24.04`) to exercise the SSH port move in both socket and service mode.
- Then deploy a real test sensor with a short TTL.
