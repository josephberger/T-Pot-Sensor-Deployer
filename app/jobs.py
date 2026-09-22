"""
Background RQ Worker Jobs for T-Pot Sensor Deployer.
Influenced by FragForge's dedicated worker tasks and jobs pattern.
These jobs run in a separate worker process to avoid clogging the web app.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from app.queue_manager import publish_task_log, set_task_status
from app.sensor_types import get_sensor_type
from app.ansible_runner import ansible_available, playbook_for, private_key_path, supported_sensor_types
from app.provisioning import configure_and_verify
from app.models import Sensor, SensorStatus
from app.do_client import DOClient
from app.gcp_client import GCPClient, GCP_SSH_USER
from app.ttl_manager import TTLManager, parse_duration
from app.edl_manager import EDLManager
from app.hive_manager import HiveManager
from app.config import (
    get_active_token, get_config, get_local_ssh_pubkey,
    get_gcp_project_id, gcp_credentials_available,
)
from app.db import db_save_active_droplet, db_delete_active_droplet

logger = logging.getLogger("tpot.jobs")

# DeployPayload.region/size/image default to DigitalOcean-shaped values ("nyc1", "s-1vcpu-2gb",
# "ubuntu-24-04-x64") since the same payload model serves both providers. The GCP deploy form does send
# real region(zone)/size(machine type) values, but it never sends `image` at all, and Pydantic fills in
# that DO default rather than leaving the field empty - found live: a plain
# `payload_dict.get("image") or <gcp default>` never fell through, so DO's "ubuntu-24-04-x64" went
# straight to the GCP API as a disk image reference and failed with a "malformed URL" 400. Treat a
# DO-default value as "not provided" for all three fields, not just the one with no matching UI field.
_DO_PAYLOAD_DEFAULTS = {"region": "nyc1", "size": "s-1vcpu-2gb", "image": "ubuntu-24-04-x64"}


def resolve_gcp_deploy_field(payload_dict: dict, cfg: dict, key: str, cfg_key: str, hard_default: str) -> str:
    """Pick payload_dict[key] unless it's missing or is DeployPayload's DO-shaped default, in which
    case fall back to the GCP Settings default (cfg[cfg_key]), then a hard-coded default."""
    val = payload_dict.get(key)
    if not val or val == _DO_PAYLOAD_DEFAULTS[key]:
        return cfg.get(cfg_key) or hard_default
    return val


def run_do_deployment_job(task_id: str, payload_dict: dict):
    """
    Execute a DigitalOcean sensor droplet deployment inside an RQ worker process.
    Publishes live status and log events to Redis Pub/Sub and lists.
    """
    set_task_status(task_id, "running")
    publish_task_log(task_id, f"🚀 Initializing DigitalOcean deployment worker task [{task_id[:8]}]...", step=1, percent=5)

    try:
        token = get_active_token()
        if not token:
            raise ValueError("DigitalOcean API token is not configured.")

        client = DOClient(token=token)
        ttl_mgr = TTLManager()
        edl_mgr = EDLManager()
        hive_mgr = HiveManager()

        sensor_type = payload_dict.get("sensor_type") or "cowrie"
        st_info = get_sensor_type(sensor_type)
        if not st_info:
            raise ValueError(f"Unknown sensor type: '{sensor_type}'")

        # Fail before creating anything (and spending money) if we can't configure the machine afterwards.
        deploy_playbook = playbook_for("sensors", sensor_type)
        if not deploy_playbook:
            raise ValueError(
                f"No Ansible playbook for sensor type '{sensor_type}' yet. "
                f"Supported: {', '.join(supported_sensor_types()) or 'none'}"
            )
        if not ansible_available():
            raise RuntimeError("ansible-playbook is not installed in this container")
        if not private_key_path().exists():
            raise RuntimeError(f"SSH private key not found at {private_key_path()} (put it in secrets/ssh_key)")
        _, local_pubkey = get_local_ssh_pubkey()
        if not local_pubkey:
            raise RuntimeError("SSH public key not found (put it in secrets/ssh_key.pub)")

        sensor_name_base = payload_dict.get("name") or f"tpot-{sensor_type}-sensor"
        region = payload_dict.get("region", "nyc1")
        size = payload_dict.get("size") or st_info.get("recommended_size_do", "s-1vcpu-2gb")
        image = payload_dict.get("image", "ubuntu-24-04-x64")
        count = max(1, int(payload_dict.get("count", 1)))
        ttl = payload_dict.get("ttl", "2h")
        attach_firewall = payload_dict.get("attach_firewall", True)
        restrict_ssh = payload_dict.get("restrict_ssh", True)
        auto_register_hive = payload_dict.get("auto_register_hive", True)
        hive_ip = payload_dict.get("hive_ip") or hive_mgr.detect_hive_ip()
        hive_port = int(payload_dict.get("hive_port", 64294))
        hive_cert = hive_mgr.get_hive_certificate()

        publish_task_log(task_id, f"📋 Sensor Type: {st_info['name']} ({st_info['short_name']}) - {st_info['tagline']}", step=1, percent=10)

        # 1. SSH key: always the worker's own key (the one Ansible connects with), never an arbitrary DO-account key.
        publish_task_log(task_id, "🔑 Ensuring the deployer SSH key is registered on DigitalOcean...", step=2, percent=15)
        ssh_key_ids = [client.ensure_ssh_key("tpot-deployer-key", local_pubkey)]

        # 2. Dedicated Sensor Cloud Firewall
        if attach_firewall:
            publish_task_log(task_id, f"🛡️ Ensuring DigitalOcean firewall for {st_info['short_name']}...", step=2, percent=20)
            try:
                client.ensure_sensor_firewall(
                    sensor_type=sensor_type,
                    name=f"tpot-sensor-firewall",
                    tag="tpot-sensor",
                    hive_ip=hive_ip if restrict_ssh else None,
                    restrict_ssh=restrict_ssh,
                    admin_ssh_port=64295,
                    open_all_ports=True
                )
                publish_task_log(task_id, f"✅ Cloud Firewall configured: attack ports open, SSH port 64295 restricted to Hive ({hive_ip})", step=2, percent=25)
            except Exception as fw_err:
                publish_task_log(task_id, f"⚠️ Firewall configuration note: {str(fw_err)}", level="warn")

        # 3. Deploy droplets
        deployed_droplets = []
        for idx in range(count):
            creds = hive_mgr.generate_sensor_credentials(name_prefix=f"sensor-{sensor_type}")
            sensor_user = creds["username"]
            sensor_name = f"{sensor_name_base}" if count == 1 else f"{sensor_name_base}-{idx + 1}"

            if auto_register_hive:
                publish_task_log(task_id, f"📝 Registering credentials '{sensor_user}' into Hive lswebpasswd & .env...", step=3, percent=35)
                ok, reg_msg = hive_mgr.register_sensor(creds)
                if ok:
                    publish_task_log(task_id, f"✅ Sensor '{sensor_user}' registered in Hive", step=3, percent=40)
                else:
                    publish_task_log(task_id, f"⚠️ Hive registration note: {reg_msg}", level="warn")

            publish_task_log(task_id, f"🌐 Creating DigitalOcean Droplet '{sensor_name}' in {region} ({size})...", step=4, percent=55)

            def on_poll(d):
                publish_task_log(task_id, f"⏳ Waiting for droplet '{sensor_name}' (status: {d.get('status')})...", level="debug")

            # Bare machine only: no user_data. Configuration happens over SSH via Ansible.
            try:
                droplet = client.create_droplet(
                    name=sensor_name,
                    region=region,
                    size=size,
                    image=image,
                    ssh_keys=ssh_key_ids,
                    tags=["tpot-sensor", f"tpot-{sensor_type}-sensor", f"type-{sensor_type}", "tpot"],
                    wait_active=True,
                    on_poll=on_poll
                )
            except Exception:
                if auto_register_hive:
                    hive_mgr.deregister_sensor(sensor_user)  # don't leave an orphaned login behind
                raise

            pub_ip = droplet.get("public_ip", "Pending")
            publish_task_log(task_id, f"✅ Droplet '{sensor_name}' ACTIVE! Public IP: {pub_ip}", step=5, percent=65)

            # Persist and lease immediately so a failed provision can never leave an untracked, billing droplet.
            lease = {}
            if ttl and ttl.strip():
                lease = ttl_mgr.schedule_lease(
                    droplet_id=droplet["id"],
                    droplet_name=sensor_name,
                    ttl_text=ttl,
                    token=client.token,
                    sensor_user=sensor_user,
                    sensor_type=sensor_type
                )
                publish_task_log(task_id, f"🔒 TTL lease: auto-destroys at {lease.get('expires_at')}", step=6, percent=68)

            record = Sensor(
                id=droplet["id"],
                name=sensor_name,
                public_ip=pub_ip,
                private_ip=droplet.get("private_ip"),
                sensor_type=sensor_type,
                sensor_user=sensor_user,
                region=region,
                size=size,
                image=image,
                tags=["tpot-sensor", f"tpot-{sensor_type}-sensor", f"type-{sensor_type}", "tpot"],
                status=SensorStatus.PROVISIONING.value,
                ttl=ttl,
                ttl_seconds=parse_duration(ttl) if ttl else None,
                created_at=datetime.now(timezone.utc).isoformat(),
                expires_at=lease.get("expires_at"),
                data=droplet,
            )
            db_save_active_droplet(record)

            # Configure over SSH with Ansible, then verify health (shared with campaigns).
            try:
                warnings = configure_and_verify(
                    pub_ip, sensor_type,
                    sensor_name=sensor_name,
                    hive_ip=hive_ip,
                    hive_port=hive_port,
                    hive_cert=hive_cert,
                    hive_token=creds["tpot_hive_user"],
                    swap_size_gb=st_info.get("swap_size_gb", 2),
                    on_log=lambda line: publish_task_log(task_id, line, level="debug"),
                    on_step=lambda msg, pct: publish_task_log(task_id, msg, step=6, percent=pct),
                )
            except Exception as cfg_err:
                record.status = SensorStatus.PROVISION_FAILED.value
                db_save_active_droplet(record)
                raise RuntimeError(f"{cfg_err} (droplet {sensor_name} kept for debugging; TTL lease still applies)")

            record.status = SensorStatus.ACTIVE.value
            db_save_active_droplet(record)
            publish_task_log(task_id, f"✅ Sensor '{sensor_name}' configured and healthy", step=6, percent=90)
            for w in warnings:
                publish_task_log(task_id, f"⚠️ {w}", level="warn")

            deployed_droplets.append({
                "id": droplet["id"],
                "name": sensor_name,
                "ip": pub_ip,
                "user": sensor_user,
                "sensor_type": sensor_type
            })

            try:
                edl_mgr.write_edl_file(do_client=client)
                publish_task_log(task_id, f"📋 Palo Alto Networks EDL updated with sensor IP: {pub_ip}", step=6, percent=92)
            except Exception as edl_err:
                publish_task_log(task_id, f"⚠️ EDL update note: {str(edl_err)}", level="debug")

        publish_task_log(task_id, f"🎉 All {count} {st_info['short_name']} sensor droplets deployed successfully!", step=7, percent=100)
        if deployed_droplets and st_info.get("test_commands"):
            publish_task_log(task_id, f"👉 Honeypot Test: {st_info['test_commands'][0]['cmd'].format(ip=deployed_droplets[0]['ip'])}")
        if deployed_droplets:
            publish_task_log(task_id, f"👉 Admin Management: ssh -p 64295 root@{deployed_droplets[0]['ip']}")

        set_task_status(task_id, "completed", deployed=deployed_droplets)
        logger.info(f"Task {task_id} completed successfully ({len(deployed_droplets)} droplets).")
        return {"task_id": task_id, "status": "completed", "deployed": deployed_droplets}

    except Exception as e:
        logger.exception(f"Deployment worker task {task_id} failed: {e}")
        publish_task_log(task_id, f"❌ Deployment failed: {str(e)}", level="error")
        set_task_status(task_id, "failed", error=str(e))
        raise


def run_gcp_deployment_job(task_id: str, payload_dict: dict):
    """
    Execute a GCP Compute Engine sensor deployment inside an RQ worker process. Mirrors
    run_do_deployment_job step-for-step: preflight, Hive login, firewall, create a bare instance (no
    startup script), record it + a TTL lease immediately, then the same cloud-agnostic
    configure_and_verify used by DO (Ansible over SSH), with remote_ssh_user=GCP_SSH_USER since GCP
    images don't allow root SSH the way DO's do (see provisioning.configure_and_verify's docstring).
    """
    set_task_status(task_id, "running")
    publish_task_log(task_id, f"🚀 Initializing GCP deployment worker task [{task_id[:8]}]...", step=1, percent=5)

    try:
        project_id = get_gcp_project_id()
        if not project_id:
            raise ValueError("GCP project id is not configured.")
        if not gcp_credentials_available():
            raise ValueError("GCP service account key not found (put it in secrets/gcp-sa.json).")

        client = GCPClient(project_id=project_id)
        ttl_mgr = TTLManager()
        edl_mgr = EDLManager()
        hive_mgr = HiveManager()

        sensor_type = payload_dict.get("sensor_type") or "cowrie"
        st_info = get_sensor_type(sensor_type)
        if not st_info:
            raise ValueError(f"Unknown sensor type: '{sensor_type}'")

        # Fail before creating anything (and spending money) if we can't configure the machine afterwards.
        deploy_playbook = playbook_for("sensors", sensor_type)
        if not deploy_playbook:
            raise ValueError(
                f"No Ansible playbook for sensor type '{sensor_type}' yet. "
                f"Supported: {', '.join(supported_sensor_types()) or 'none'}"
            )
        if not ansible_available():
            raise RuntimeError("ansible-playbook is not installed in this container")
        if not private_key_path().exists():
            raise RuntimeError(f"SSH private key not found at {private_key_path()} (put it in secrets/ssh_key)")
        _, local_pubkey = get_local_ssh_pubkey()
        if not local_pubkey:
            raise RuntimeError("SSH public key not found (put it in secrets/ssh_key.pub)")

        sensor_name_base = payload_dict.get("name") or f"tpot-{sensor_type}-sensor"
        cfg = get_config()
        zone = resolve_gcp_deploy_field(payload_dict, cfg, "region", "gcp_zone", "us-central1-a")
        machine_type = resolve_gcp_deploy_field(
            payload_dict, cfg, "size", "gcp_machine_type", st_info.get("recommended_size_gcp", "e2-small")
        )
        image = resolve_gcp_deploy_field(
            payload_dict, cfg, "image", "gcp_image",
            "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
        )
        count = max(1, int(payload_dict.get("count", 1)))  # DeployPayload already caps this at 10 server-side
        ttl = payload_dict.get("ttl", "2h")
        attach_firewall = payload_dict.get("attach_firewall", True)
        restrict_ssh = payload_dict.get("restrict_ssh", True)
        auto_register_hive = payload_dict.get("auto_register_hive", True)
        hive_ip = payload_dict.get("hive_ip") or hive_mgr.detect_hive_ip()
        hive_port = int(payload_dict.get("hive_port", 64294))
        hive_cert = hive_mgr.get_hive_certificate()

        publish_task_log(task_id, f"📋 Sensor Type: {st_info['name']} ({st_info['short_name']}) - {st_info['tagline']}", step=1, percent=10)

        if attach_firewall:
            publish_task_log(task_id, "🛡️ Ensuring GCP firewall rules for the sensor fleet...", step=2, percent=20)
            try:
                client.ensure_sensor_firewall(hive_ip=hive_ip if restrict_ssh else None, restrict_ssh=restrict_ssh, admin_ssh_port=64295)
                publish_task_log(task_id, f"✅ Firewall configured: attack ports open, SSH port 64295 restricted to Hive ({hive_ip})", step=2, percent=25)
            except Exception as fw_err:
                publish_task_log(task_id, f"⚠️ Firewall configuration note: {str(fw_err)}", level="warn")

        deployed_instances = []
        for idx in range(count):
            creds = hive_mgr.generate_sensor_credentials(name_prefix=f"sensor-{sensor_type}")
            sensor_user = creds["username"]
            sensor_name = sensor_name_base if count == 1 else f"{sensor_name_base}-{idx + 1}"

            if auto_register_hive:
                publish_task_log(task_id, f"📝 Registering credentials '{sensor_user}' into Hive lswebpasswd & .env...", step=3, percent=35)
                ok, reg_msg = hive_mgr.register_sensor(creds)
                if ok:
                    publish_task_log(task_id, f"✅ Sensor '{sensor_user}' registered in Hive", step=3, percent=40)
                else:
                    publish_task_log(task_id, f"⚠️ Hive registration note: {reg_msg}", level="warn")

            publish_task_log(task_id, f"🌐 Creating GCP instance '{sensor_name}' in {zone} ({machine_type})...", step=4, percent=55)

            def on_poll(d):
                publish_task_log(task_id, f"⏳ Waiting for instance '{sensor_name}' (status: {d.get('status')})...", level="debug")

            # Bare machine only: no startup script. Configuration happens over SSH via Ansible.
            try:
                instance = client.create_instance(
                    name=sensor_name,
                    zone=zone,
                    machine_type=machine_type,
                    image=image,
                    ssh_pubkey=local_pubkey,
                    network_tags=["tpot-sensor", f"tpot-{sensor_type}-sensor"],
                    wait_active=True,
                    on_poll=on_poll,
                )
            except Exception:
                if auto_register_hive:
                    hive_mgr.deregister_sensor(sensor_user)  # don't leave an orphaned login behind
                raise

            pub_ip = instance.get("public_ip", "Pending")
            publish_task_log(task_id, f"✅ Instance '{sensor_name}' ACTIVE! Public IP: {pub_ip}", step=5, percent=65)

            # Persist and lease immediately so a failed provision can never leave an untracked, billing instance.
            lease = {}
            if ttl and ttl.strip():
                lease = ttl_mgr.schedule_lease(
                    droplet_id=instance["id"],
                    droplet_name=sensor_name,
                    ttl_text=ttl,
                    sensor_user=sensor_user,
                    sensor_type=sensor_type,
                    provider="gcp",
                    region=zone,
                )
                publish_task_log(task_id, f"🔒 TTL lease: auto-destroys at {lease.get('expires_at')}", step=6, percent=68)

            record = Sensor(
                id=instance["id"],
                name=sensor_name,
                public_ip=pub_ip,
                private_ip=instance.get("private_ip"),
                sensor_type=sensor_type,
                sensor_user=sensor_user,
                provider="gcp",
                region=zone,
                size=machine_type,
                image=image,
                tags=["tpot-sensor", f"tpot-{sensor_type}-sensor", f"type-{sensor_type}"],
                status=SensorStatus.PROVISIONING.value,
                ttl=ttl,
                ttl_seconds=parse_duration(ttl) if ttl else None,
                created_at=datetime.now(timezone.utc).isoformat(),
                expires_at=lease.get("expires_at"),
                data=instance,
            )
            db_save_active_droplet(record)

            # Configure over SSH with Ansible, then verify health (shared with DO and campaigns).
            try:
                warnings = configure_and_verify(
                    pub_ip, sensor_type,
                    sensor_name=sensor_name,
                    hive_ip=hive_ip,
                    hive_port=hive_port,
                    hive_cert=hive_cert,
                    hive_token=creds["tpot_hive_user"],
                    swap_size_gb=st_info.get("swap_size_gb", 2),
                    remote_ssh_user=GCP_SSH_USER,
                    on_log=lambda line: publish_task_log(task_id, line, level="debug"),
                    on_step=lambda msg, pct: publish_task_log(task_id, msg, step=6, percent=pct),
                )
            except Exception as cfg_err:
                record.status = SensorStatus.PROVISION_FAILED.value
                db_save_active_droplet(record)
                raise RuntimeError(f"{cfg_err} (instance {sensor_name} kept for debugging; TTL lease still applies)")

            record.status = SensorStatus.ACTIVE.value
            db_save_active_droplet(record)
            publish_task_log(task_id, f"✅ Sensor '{sensor_name}' configured and healthy", step=6, percent=90)
            for w in warnings:
                publish_task_log(task_id, f"⚠️ {w}", level="warn")

            deployed_instances.append({
                "id": instance["id"],
                "name": sensor_name,
                "ip": pub_ip,
                "user": sensor_user,
                "sensor_type": sensor_type,
                "provider": "gcp",
            })

            try:
                edl_mgr.write_edl_file()
                publish_task_log(task_id, f"📋 Palo Alto Networks EDL updated with sensor IP: {pub_ip}", step=6, percent=92)
            except Exception as edl_err:
                publish_task_log(task_id, f"⚠️ EDL update note: {str(edl_err)}", level="debug")

        publish_task_log(task_id, f"🎉 All {count} {st_info['short_name']} GCP sensor(s) deployed successfully!", step=7, percent=100)
        if deployed_instances and st_info.get("test_commands"):
            publish_task_log(task_id, f"👉 Honeypot Test: {st_info['test_commands'][0]['cmd'].format(ip=deployed_instances[0]['ip'])}")
        if deployed_instances:
            publish_task_log(task_id, f"👉 Admin Management: ssh -p 64295 {GCP_SSH_USER}@{deployed_instances[0]['ip']}")

        set_task_status(task_id, "completed", deployed=deployed_instances)
        logger.info(f"Task {task_id} completed successfully ({len(deployed_instances)} GCP instances).")
        return {"task_id": task_id, "status": "completed", "deployed": deployed_instances}

    except Exception as e:
        logger.exception(f"GCP deployment task {task_id} failed: {e}")
        publish_task_log(task_id, f"❌ GCP deployment failed: {str(e)}", level="error")
        set_task_status(task_id, "failed", error=str(e))
        raise


def campaign_scheduler_tick_job():
    """
    Periodic job executed by the dedicated worker:
    Performs a reconciliation tick across all configured honeypot campaigns.
    """
    try:
        from app.scheduler_manager import SchedulerManager
        mgr = SchedulerManager()
        mgr.reconcile_tick()
    except Exception as e:
        logger.warning(f"Campaign scheduler tick failed: {e}", exc_info=True)


def ttl_sweep_job():
    """
    Periodic job executed by the dedicated worker (worker.py's SchedulerThread, every 60s):
    checks for expired TTL leases and tears down the machines whose lifespan elapsed, DO or GCP.

    This used to call ttl_mgr.get_expired_leases()/release_lease(), methods that have never existed on
    TTLManager (only sweep_expired_leases() does - present since SQLite-first fleet tracking landed).
    Every call raised AttributeError, silently swallowed by the except below at debug level: TTL
    auto-destroy has been dead code from the worker loop since the beginning, for every provider. Fixed
    by calling the real (and tested) method, which also deregisters the Hive login and refreshes the EDL.
    """
    try:
        ttl_mgr = TTLManager()
        destroyed = ttl_mgr.sweep_expired_leases()
        for d_id in destroyed:
            db_delete_active_droplet(int(d_id))
    except Exception as e:
        logger.debug(f"TTL sweep error: {e}")
