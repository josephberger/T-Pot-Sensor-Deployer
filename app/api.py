import asyncio
import os
import json
import uuid
import queue
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Annotated
from contextlib import asynccontextmanager

# fastapi.Path (a request-parameter validator) shadows pathlib.Path here on purpose - nothing in this
# file uses the filesystem Path type, only fastapi's.
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, Path
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import (
    load_config, save_config, get_do_token, get_local_ssh_pubkey, public_settings, ssh_fingerprint,
    get_gcp_project_id, gcp_credentials_available, gcp_sa_key_path, APP_DIR, PROJECT_ROOT
)
from app.do_client import DOClient, DOAPIError
from app.gcp_client import GCPClient, GCPAPIError
from app.cloud import get_client
from app.models import (
    SettingsPayload, DeployPayload, ActionPayload, TTLPayload, StaticIPPayload, StaticIPUpdatePayload, ScheduleCreatePayload, ScheduleActionPayload
)
from app.hive_manager import HiveManager
from app.ttl_manager import TTLManager, parse_duration, format_remaining
from app.edl_manager import EDLManager, EDL_FILE_PATH
from app.health import quick_check, deep_check
from app.ansible_runner import supported_sensor_types
from app.scheduler_manager import SchedulerManager, PRESETS
from app.sensor_types import SENSOR_TYPES, list_sensor_types, get_sensor_type, get_sensor_ports
from app.db import (
    db_get_fleet_history,
    db_get_all_active_droplets,
    db_get_active_droplet,
    db_save_active_droplet,
    db_delete_active_droplet,
    db_sync_active_droplets,
    db_get_cloud_options,
    db_save_cloud_metadata,
    db_save_account_verification,
    db_get_cached_account
)

import logging
import time

from app.queue_manager import (
    redis_conn,
    task_queue,
    publish_task_log,
    set_task_status,
    get_task_logs,
    get_task_status,
    is_redis_available,
    list_active_workers,
    get_queue_stats,
    get_redis_info,
    record_task,
    get_recent_tasks,
    REDIS_URL
)
from app.jobs import (
    run_do_deployment_job,
    run_gcp_deployment_job,
)

logger = logging.getLogger("tpot.api")

# A path int has no upper bound by default (Python ints are arbitrary precision), so a huge value in a
# URL - "/api/droplets/99999999999999999999999999/health" - reached sqlite3's bind step and crashed with
# an unhandled OverflowError (SQLite integers are 64-bit) instead of a clean 4xx. Real droplet IDs are
# nowhere near this large; the bound is SQLite's actual ceiling, not an arbitrary guess.
DropletId = Annotated[int, Path(gt=0, le=9223372036854775807)]

# Initialize managers
hive_mgr = HiveManager()
ttl_mgr = TTLManager()
edl_mgr = EDLManager()
scheduler_mgr = SchedulerManager()

# Deployment task logging bus (compatibility reference)
DEPLOY_TASKS: Dict[str, Dict[str, Any]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # TTL sweeps and campaign reconciliation run only in worker.py's dedicated SchedulerThread (one
    # process, one thread). The web container used to fall back to running its own copy of both loops
    # whenever no RQ worker was registered yet at startup - but `docker compose up` starts web and worker
    # concurrently with no ordering between them, so web routinely won the race and started its fallback
    # loops before the worker registered. Once started they never stopped, so both containers ticked
    # campaigns and TTL sweeps for the life of the stack: every dispatch, teardown and TTL destroy could
    # run twice from two processes sharing no real lock (each process's threading.RLock only protects
    # itself). See CLAUDE.md; worker is a required Compose service, not an optional fallback target.
    try:
        edl_mgr.write_edl_file(do_client=get_do_client())
    except Exception:
        pass
    yield


app = FastAPI(title="T-Pot Sensor Deployer", lifespan=lifespan)

# Mount static files and templates
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


def get_do_client(token_override: Optional[str] = None) -> DOClient:
    token = token_override or get_do_token()
    return DOClient(token=token)


def get_gcp_client() -> GCPClient:
    return GCPClient(project_id=get_gcp_project_id())


# ---------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------
@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
@app.api_route("/dashboard", methods=["GET", "HEAD"], response_class=HTMLResponse)
def serve_dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="dashboard.html", context={"active_page": "dashboard"})


@app.api_route("/deploy", methods=["GET", "HEAD"], response_class=HTMLResponse)
def serve_deploy(request: Request):
    return templates.TemplateResponse(request=request, name="deploy.html", context={"active_page": "deploy"})


@app.api_route("/fleet", methods=["GET", "HEAD"], response_class=HTMLResponse)
def serve_fleet(request: Request):
    return templates.TemplateResponse(request=request, name="fleet.html", context={"active_page": "fleet"})


@app.api_route("/campaigns", methods=["GET", "HEAD"], response_class=HTMLResponse)
@app.api_route("/schedules", methods=["GET", "HEAD"], response_class=HTMLResponse)
def serve_campaigns(request: Request):
    return templates.TemplateResponse(request=request, name="campaigns.html", context={"active_page": "campaigns"})


@app.api_route("/edl", methods=["GET", "HEAD"], response_class=HTMLResponse)
def serve_edl(request: Request):
    return templates.TemplateResponse(request=request, name="edl.html", context={"active_page": "edl"})


@app.api_route("/admin", methods=["GET", "HEAD"], response_class=HTMLResponse)
def serve_admin(request: Request):
    return templates.TemplateResponse(request=request, name="admin.html", context={"active_page": "admin"})


@app.api_route("/tasks", methods=["GET", "HEAD"], response_class=HTMLResponse)
@app.api_route("/workers", methods=["GET", "HEAD"], response_class=HTMLResponse)
def serve_tasks(request: Request):
    return templates.TemplateResponse(request=request, name="tasks.html", context={"active_page": "tasks"})


@app.get("/api/status")
def get_status():
    cfg = load_config()
    is_hive = hive_mgr.is_hive_installed()
    hive_ip = cfg.get("hive_ip") or hive_mgr.detect_hive_ip()
    cert = hive_mgr.get_hive_certificate()
    registered_sensors = hive_mgr.list_registered_sensors()

    client = get_do_client()
    cached_account = db_get_cached_account()
    do_status = {
        "configured": client.is_configured(),
        "account": cached_account,
        "verified": bool(cached_account),
        "error": None
    }

    # Local SSH key detection
    pub_key_path, local_ssh_key_content = get_local_ssh_pubkey()
    has_local_ssh_key = bool(local_ssh_key_content)

    return {
        "hive": {
            "installed": is_hive,
            "ip": hive_ip,
            "port": cfg.get("hive_port", 64294),
            "cert_loaded": bool(cert),
            "registered_sensors_count": len(registered_sensors),
            "registered_sensors": registered_sensors
        },
        "digitalocean": do_status,
        "config": public_settings(cfg),
        "local_ssh_key": {
            "found": has_local_ssh_key,
            "path": str(pub_key_path),
            "preview": (local_ssh_key_content[:40] + "...") if local_ssh_key_content else "",
            "fingerprint": ssh_fingerprint(local_ssh_key_content)
        },
        "gcp": {
            "configured": GCPClient(project_id=get_gcp_project_id()).is_configured(),
            "project_id": get_gcp_project_id(),
            "service_account_present": gcp_credentials_available(),
        },
        "schedules": {
            "total": len(scheduler_mgr.list_schedules()),
            "active": len([s for s in scheduler_mgr.list_schedules() if s.get("state", {}).get("status") == "active"])
        }
    }


@app.get("/api/options")
def get_options():
    cfg = load_config()
    opts = db_get_cloud_options()

    regions = opts.get("regions", [])
    sizes = opts.get("sizes", [])
    images = opts.get("images", [])
    ssh_keys = opts.get("ssh_keys", [])

    # Pre-generate sensor credentials & name
    creds = hive_mgr.generate_sensor_credentials(name_prefix="cowrie-sensor")

    # Local SSH key
    _, local_key = get_local_ssh_pubkey()

    gcp_opts = {"zones": [], "machine_types": [], "images": [], "configured": False}
    if gcp_credentials_available() and cfg.get("gcp_project_id"):
        gcp_client = get_gcp_client()
        gcp_opts["configured"] = gcp_client.is_configured()
        if gcp_opts["configured"]:
            try:
                gcp_opts["zones"] = gcp_client.list_zones()
                gcp_opts["images"] = gcp_client.list_images()
                default_zone = cfg.get("gcp_zone") or "us-central1-a"
                gcp_opts["machine_types"] = gcp_client.list_machine_types(default_zone)
            except Exception as e:
                gcp_opts["error"] = str(e)

    return {
        "regions": regions,
        "sizes": sizes,
        "images": images,
        "ssh_keys": ssh_keys,
        "tags": opts.get("tags", []),
        "last_synced_at": opts.get("last_synced_at"),
        "sync_stats": opts.get("sync_stats", {}),
        "generated_creds": {"username": creds["username"]},  # name suggestion only; real credentials are minted at deploy time
        "local_ssh_key": local_key,
        "local_ssh_fingerprint": ssh_fingerprint(local_key),
        "hive_ip": cfg.get("hive_ip") or hive_mgr.detect_hive_ip(),
        "hive_cert": hive_mgr.get_hive_certificate(),
        "defaults": public_settings(cfg),
        "gcp": gcp_opts,
    }


@app.post("/api/settings")
def update_settings(payload: SettingsPayload):
    updates = {k: v for k, v in payload.dict().items() if v is not None}
    saved = save_config(updates)
    return {"status": "success", "config": public_settings(saved)}


@app.post("/api/test/token")
def test_token(payload: Dict[str, str]):
    token = payload.get("token", "").strip() or get_do_token()
    if not token:
        raise HTTPException(status_code=400, detail="Token cannot be empty")
    client = DOClient(token=token)
    try:
        acc = client.test_connection()
        db_save_account_verification(acc)
        return {"status": "success", "account": acc}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/test/gcp")
def test_gcp(payload: Dict[str, str]):
    project_id = (payload.get("project_id") or "").strip() or get_gcp_project_id()
    if not project_id:
        raise HTTPException(status_code=400, detail="GCP project id is not configured")
    if not gcp_credentials_available():
        raise HTTPException(status_code=400, detail=f"No GCP service account key at {gcp_sa_key_path()}")
    client = GCPClient(project_id=project_id)
    try:
        return {"status": "success", "account": client.test_connection()}
    except GCPAPIError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/gcp/network")
def teardown_gcp_network():
    """
    Manual teardown of the dedicated tpot-sensor-vpc network + its subnets (app.gcp_client.GCPClient.
    ensure_sensor_network creates them on demand on the next deploy). Never called automatically on a
    sensor's own destroy - the network is shared infrastructure for the whole GCP fleet, not
    per-instance - so this refuses while any GCP sensor is still tracked, rather than pulling the
    network out from under a running one.
    """
    still_active = [
        d for d in db_get_all_active_droplets() if d.get("provider") == "gcp"
    ]
    if still_active:
        names = ", ".join(d["name"] for d in still_active[:5])
        raise HTTPException(
            status_code=409,
            detail=f"{len(still_active)} GCP sensor(s) still tracked ({names}) - destroy them first.",
        )
    project_id = get_gcp_project_id()
    if not project_id:
        raise HTTPException(status_code=400, detail="GCP project id is not configured")
    client = GCPClient(project_id=project_id)
    try:
        client.destroy_sensor_network()
        return {"status": "success", "message": "Dedicated GCP network and subnets removed."}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/test/hive")
def test_hive(payload: Dict[str, Any]):
    ip = payload.get("ip") or hive_mgr.detect_hive_ip()
    port = int(payload.get("port", 64294))
    res = hive_mgr.test_hive_port(hive_ip=ip, port=port)
    return res


@app.get("/api/sensor-types")
def get_sensor_types_endpoint():
    """Returns all available honeypot sensor types, port mappings, and metadata.
    `deployable` is true only for types that have Ansible playbooks (see playbooks/)."""
    deployable = set(supported_sensor_types())
    types_list = [dict(s, deployable=s["id"] in deployable) for s in list_sensor_types()]
    return {
        "sensor_types": {s["id"]: s for s in types_list},
        "sensor_types_list": types_list
    }


def _extract_sensor_type(tags: list) -> str:
    for t in tags:
        if t.startswith("type-"):
            return t.replace("type-", "")
        elif t.startswith("tpot-") and t.endswith("-sensor") and t != "tpot-sensor":
            return t.replace("tpot-", "").replace("-sensor", "")
    return "cowrie"


@app.get("/api/droplets")
def list_droplets():
    raw_droplets = db_get_all_active_droplets()
    droplets = []
    leases = ttl_mgr.get_all_leases()
    now = datetime.now(timezone.utc)

    for d in raw_droplets:
        sid = str(d["id"])
        tags = d.get("tags") or []
        sched_tags = [t for t in tags if t.startswith("sched-")]
        stype = d.get("sensor_type") or _extract_sensor_type(tags)
        st_info = get_sensor_type(stype)

        ttl_info = leases.get(sid)
        if not ttl_info and d.get("expires_at"):
            try:
                exp_dt = datetime.fromisoformat(d["expires_at"])
                rem_sec = max(0, (exp_dt - now).total_seconds())
                ttl_info = {
                    "droplet_id": d["id"],
                    "droplet_name": d["name"],
                    "sensor_user": d.get("sensor_user"),
                    "ttl": d.get("ttl", ""),
                    "ttl_seconds": d.get("ttl_seconds", 0),
                    "created_at": d.get("created_at"),
                    "expires_at": d.get("expires_at"),
                    "remaining_seconds": rem_sec,
                    "remaining_formatted": format_remaining(rem_sec),
                    "is_expired": rem_sec <= 0
                }
            except Exception:
                pass

        pub_ip = d.get("public_ip")
        provider = d.get("provider", "digitalocean")
        test_cmd = None
        if pub_ip and st_info.get("test_commands"):
            test_cmd = st_info["test_commands"][0]["cmd"].format(ip=pub_ip)
        elif pub_ip:
            ssh_user = "tpotadmin" if provider == "gcp" else "root"
            test_cmd = f"ssh {ssh_user}@{pub_ip} -p 22"

        reg_slug = d.get("region", "nyc1")
        size_slug = d.get("size", "s-1vcpu-2gb")
        droplets.append({
            "id": d["id"],
            "name": d["name"],
            "public_ip": pub_ip,
            "private_ip": d.get("private_ip"),
            "provider": provider,
            "sensor_type": stype,
            "sensor_user": d.get("sensor_user"),  # the sensor's Hive login (used by the Admin page)
            "sensor_name": st_info.get("short_name", stype.capitalize()),
            "sensor_icon": st_info.get("icon", "🛡️"),
            "region": {"slug": reg_slug, "name": reg_slug.upper()},
            "size": {"slug": size_slug, "memory": 2048, "vcpus": 1},
            "status": d.get("status", "active"),
            "tags": tags,
            "ttl_info": ttl_info,
            "is_scheduled": len(sched_tags) > 0,
            "schedule_id": sched_tags[0] if sched_tags else None,
            "test_command": test_cmd,
            "created_at": d.get("created_at")
        })

    return {"droplets": droplets, "error": None}


@app.post("/api/droplets/sync")
def sync_droplets_from_cloud():
    """Manual sync: queries DigitalOcean API and reconciles local active_droplets table."""
    client = get_do_client()
    if not client.is_configured():
        raise HTTPException(status_code=400, detail="DigitalOcean API token is not configured.")
    try:
        cloud_droplets = client.list_droplets(force_refresh=True)
        res = db_sync_active_droplets(cloud_droplets)
        try:
            edl_mgr.invalidate_cache()
            edl_mgr.write_edl_file(do_client=client)
        except Exception:
            pass
        return {
            "status": "success",
            "message": f"Successfully synced {res['active_count']} active droplet(s) from DigitalOcean.",
            "synced": res
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to sync droplets from DigitalOcean: {str(e)}")


@app.post("/api/cloud/sync")
def sync_cloud_metadata():
    """Manual sync: pulls regions, sizes, images, ssh keys, and tags from DigitalOcean into SQLite."""
    client = get_do_client()
    if not client.is_configured():
        raise HTTPException(status_code=400, detail="DigitalOcean API token is not configured.")
    try:
        regions = client.list_regions()
        sizes = client.list_sizes()
        images = client.list_images()
        ssh_keys = client.list_ssh_keys()
        tags = client.list_tags()

        stats = db_save_cloud_metadata(
            regions=regions,
            sizes=sizes,
            images=images,
            ssh_keys=ssh_keys,
            tags=tags
        )
        return {
            "status": "success",
            "message": f"Successfully synced {len(regions)} regions, {len(sizes)} sizes, {len(images)} images, {len(ssh_keys)} SSH keys, and {stats.get('tags', 0)} tags into database.",
            "stats": stats
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to sync cloud metadata: {str(e)}")


@app.get("/api/sensors")
def list_all_sensors():
    """Unified endpoint returning sensors across all cloud providers."""
    sensors = []
    client = get_do_client()

    # DigitalOcean sensors
    if client.is_configured():
        try:
            droplets = client.list_droplets()
            leases = ttl_mgr.get_all_leases()
            for d in droplets:
                sid = str(d["id"])
                pub_ip = d.get("public_ip")
                tags = d.get("tags", [])
                sched_tags = [t for t in tags if t.startswith("sched-")]
                stype = _extract_sensor_type(tags)
                st_info = get_sensor_type(stype)

                test_cmd = None
                if pub_ip and st_info.get("test_commands"):
                    test_cmd = st_info["test_commands"][0]["cmd"].format(ip=pub_ip)

                sensors.append({
                    "id": d["id"],
                    "name": d.get("name"),
                    "sensor_type": stype,
                    "sensor_name": st_info.get("short_name", stype.capitalize()),
                    "sensor_icon": st_info.get("icon", "🛡️"),
                    "provider": "digitalocean",
                    "public_ip": pub_ip,
                    "region": d.get("region", {}).get("slug", ""),
                    "size": d.get("size_slug", ""),
                    "status": d.get("status"),
                    "ttl_info": leases.get(sid),
                    "is_scheduled": len(sched_tags) > 0,
                    "schedule_id": sched_tags[0] if sched_tags else None,
                    "ssh_command": f"ssh -p 64295 root@{pub_ip}" if pub_ip else None,
                    "test_command": test_cmd,
                })
        except Exception:
            pass

    # GCP sensors (live from the Compute API, same as the DO block above)
    gcp_client = get_gcp_client()
    if gcp_client.is_configured():
        try:
            instances = gcp_client.list_instances()
            leases = ttl_mgr.get_all_leases()
            db_rows = {r["id"]: r for r in db_get_all_active_droplets() if r.get("provider") == "gcp"}
            for inst in instances:
                sid = str(inst["id"])
                pub_ip = inst.get("public_ip")
                row = db_rows.get(inst["id"], {})
                stype = row.get("sensor_type") or _extract_sensor_type(inst.get("tags", []))
                st_info = get_sensor_type(stype)

                test_cmd = None
                if pub_ip and st_info.get("test_commands"):
                    test_cmd = st_info["test_commands"][0]["cmd"].format(ip=pub_ip)

                sensors.append({
                    "id": inst["id"],
                    "name": inst.get("name"),
                    "sensor_type": stype,
                    "sensor_name": st_info.get("short_name", stype.capitalize()),
                    "sensor_icon": st_info.get("icon", "🛡️"),
                    "provider": "gcp",
                    "public_ip": pub_ip,
                    "private_ip": inst.get("private_ip"),
                    "region": inst.get("zone"),
                    "size": inst.get("machine_type"),
                    "status": inst.get("status"),
                    "ttl_info": leases.get(sid),
                    "ssh_command": f"ssh -p 64295 tpotadmin@{pub_ip}" if pub_ip else None,
                    "test_command": test_cmd,
                })
        except Exception:
            pass

    return {"sensors": sensors, "count": len(sensors)}


@app.post("/api/droplets/{droplet_id}/action")
def droplet_action(droplet_id: DropletId, payload: ActionPayload):
    row = db_get_active_droplet(droplet_id)
    if not row:
        raise HTTPException(status_code=404, detail="Sensor not found")
    provider = row.get("provider", "digitalocean")
    try:
        client = get_client(provider)
        if provider == "gcp":
            client.instance_action(row["name"], row.get("region"), payload.action)
            res = {"action": payload.action, "instance": row["name"]}
        else:
            res = client.droplet_action(droplet_id, payload.action)
        return {"status": "success", "action": res}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/droplets/{droplet_id}/ttl")
def update_droplet_ttl(droplet_id: DropletId, payload: TTLPayload):
    if payload.cancel:
        ttl_mgr.cancel_lease(droplet_id)
        return {"status": "cancelled"}
    if payload.ttl:
        existing = db_get_active_droplet(droplet_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Sensor not found")
        provider = existing.get("provider", "digitalocean")
        name = existing.get("name", str(droplet_id))
        sensor_type = existing.get("sensor_type") or "cowrie"
        token = get_do_token() if provider == "digitalocean" else None
        lease = ttl_mgr.schedule_lease(
            droplet_id, name, payload.ttl, token, sensor_type=sensor_type,
            provider=provider, region=existing.get("region"),
        )
        return {"status": "scheduled", "lease": lease}
    raise HTTPException(status_code=400, detail="Must provide ttl or cancel flag")


@app.get("/api/droplets/{droplet_id}/health")
def droplet_health(droplet_id: DropletId, deep: bool = False):
    """Ping/TCP health of a sensor; deep=true also runs the sensor type's health playbook over SSH."""
    d = db_get_active_droplet(droplet_id)
    if not d or not d.get("public_ip"):
        raise HTTPException(status_code=404, detail="Sensor not found or has no public IP")
    stype = d.get("sensor_type") or "cowrie"
    result = quick_check(d["public_ip"], stype)
    if deep:
        cfg = load_config()
        remote_ssh_user = "tpotadmin" if d.get("provider") == "gcp" else "root"
        result["deep"] = deep_check(
            d["public_ip"], stype, hive_ip=cfg.get("hive_ip") or hive_mgr.detect_hive_ip(),
            remote_ssh_user=remote_ssh_user,
        )
        result["healthy"] = result["healthy"] and result["deep"]["healthy"]
    return result


@app.delete("/api/droplets/{droplet_id}")
def delete_droplet(droplet_id: DropletId, deregister_hive: bool = False, sensor_name: Optional[str] = None):
    row = db_get_active_droplet(droplet_id)
    provider = (row or {}).get("provider", "digitalocean")
    client = get_client(provider)
    try:
        # Fill in the name/zone from our own record if not provided - needed for GCP (whose delete call
        # addresses the instance by name, not id) and to save DO a live lookup it doesn't need either.
        if not sensor_name:
            sensor_name = (row or {}).get("name")

        if provider == "gcp":
            if not sensor_name:
                raise HTTPException(status_code=404, detail="Sensor not found")
            client.destroy_instance(sensor_name, (row or {}).get("region"))
        else:
            client.destroy_droplet(droplet_id)
        ttl_mgr.cancel_lease(droplet_id)
        db_delete_active_droplet(droplet_id)
        try:
            edl_mgr.invalidate_cache()
            edl_mgr.write_edl_file(do_client=client if provider == "digitalocean" else None)
        except Exception:
            pass

        msg = f"Sensor {droplet_id} destroyed successfully"
        if deregister_hive and sensor_name:
            ok, dereg_msg = hive_mgr.deregister_sensor(sensor_name)
            msg += f". {dereg_msg}"

        return {"status": "success", "message": msg}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/firewall/setup")
def setup_firewall():
    """Create or refresh the DigitalOcean Cloud Firewall for the sensor fleet."""
    cfg = load_config()
    client = get_do_client()
    if not client.is_configured():
        raise HTTPException(status_code=400, detail="DigitalOcean API token is not configured.")

    hive_ip = cfg.get("hive_ip") or hive_mgr.detect_hive_ip() or ""
    try:
        fw_id = client.ensure_sensor_firewall(
            name="tpot-sensor-firewall",
            tag="tpot-sensor",
            hive_ip=hive_ip,
            restrict_ssh=True,
            admin_ssh_port=64295,
            open_all_ports=True
        )
        return {
            "status": "success",
            "firewall_id": fw_id,
            "firewall_name": "tpot-sensor-firewall",
            "tags": ["tpot-sensor", "tpot-cowrie-sensor"],
            "hive_ip": hive_ip,
            "admin_ssh_port": 64295,
            "rules": {
                "inbound_tcp_open": ["1-64294", "64296-65535"],
                "inbound_tcp_admin": f"64295 restricted to {hive_ip}/32" if hive_ip else "open",
                "inbound_udp_open": "1-65535",
                "inbound_icmp": "all",
                "outbound": "all"
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to configure firewall: {str(e)}")


@app.get("/api/hive/sensors")
def list_hive_sensors():
    return {
        "sensors": hive_mgr.list_registered_sensors()
    }


@app.delete("/api/hive/sensors/{username}")
def remove_hive_sensor(username: str):
    ok, msg = hive_mgr.deregister_sensor(username)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"status": "success", "message": msg}


# ---------------------------------------------------------
# Palo Alto Networks - External Dynamic List (EDL) Endpoints
# ---------------------------------------------------------
@app.api_route("/edl/sensors.txt", methods=["GET", "HEAD"], response_class=PlainTextResponse)
@app.api_route("/edl.txt", methods=["GET", "HEAD"], response_class=PlainTextResponse)
@app.api_route("/edl/sensors", methods=["GET", "HEAD"], response_class=PlainTextResponse)
@app.api_route("/sensors.txt", methods=["GET", "HEAD"], response_class=PlainTextResponse)
def get_edl_text(comments: bool = True):
    """
    Hosts the plaintext IP list for Palo Alto Networks External Dynamic Lists (EDL).
    PAN-OS polls this endpoint over HTTP to dynamically permit sensor traffic through NAT/Firewall.
    """
    entries = edl_mgr.get_all_entries()
    text = edl_mgr.generate_edl_plaintext(include_comments=comments, entries=entries)
    return PlainTextResponse(
        content=text,
        media_type="text/plain; charset=utf-8",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )


@app.get("/api/edl")
def get_edl_info(request: Request):
    """Returns JSON metadata and current preview of the EDL feed for the Web UI."""
    cfg = load_config()
    hive_ip = cfg.get("hive_ip") or hive_mgr.detect_hive_ip()
    host_header = request.headers.get("host") or hive_ip

    entries = edl_mgr.get_all_entries()
    raw_text = edl_mgr.generate_edl_plaintext(include_comments=True, entries=entries)
    if not EDL_FILE_PATH.exists():
        edl_mgr.write_edl_file(entries=entries)

    return {
        "edl_url": f"http://{host_header}/edl/sensors.txt",
        "edl_url_relative": "/edl/sensors.txt",
        "file_path": str(EDL_FILE_PATH),
        "total_ips": len(entries),
        "entries": entries,
        "raw_preview": raw_text
    }


@app.post("/api/edl/static")
def add_static_edl_ip(payload: StaticIPPayload):
    """Manually add a static sensor or external honeypot IP to the EDL."""
    client = get_do_client()
    ok, msg = edl_mgr.add_static_ip(payload.ip, payload.comment or "")
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    edl_mgr.write_edl_file(do_client=client)
    return {"status": "success", "message": msg}


@app.put("/api/edl/static")
def update_static_edl_ip(payload: StaticIPUpdatePayload):
    """Edit a static entry's address or label."""
    client = get_do_client()
    ok, msg = edl_mgr.update_static_ip(payload.old_ip, payload.ip, payload.comment or "")
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    edl_mgr.write_edl_file(do_client=client)
    return {"status": "success", "message": msg}


@app.delete("/api/edl/static")
def remove_static_edl_ip(payload: StaticIPPayload):
    """Remove a static IP from the EDL."""
    client = get_do_client()
    ok, msg = edl_mgr.remove_static_ip(payload.ip)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    edl_mgr.write_edl_file(do_client=client)
    return {"status": "success", "message": msg}


# ---------------------------------------------------------
# Honeypot Campaign Scheduler Endpoints
# ---------------------------------------------------------
@app.get("/api/schedules")
def list_schedules():
    """List all configured recurring and interval honeypot schedules."""
    return {
        "schedules": scheduler_mgr.list_schedules(),
        "presets": PRESETS
    }


@app.post("/api/schedules")
def create_schedule(payload: ScheduleCreatePayload):
    """Creates a new automated honeypot schedule (e.g. weekly 24h, 8h with 48-72h rebuild)."""
    try:
        s = scheduler_mgr.create_schedule(payload.dict())
        return {"status": "success", "schedule": s}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/schedules/{schedule_id}")
def get_schedule(schedule_id: str):
    """Returns details and history of a specific schedule."""
    s = scheduler_mgr.get_schedule(schedule_id)
    if not s:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return s


@app.post("/api/schedules/{schedule_id}/pause")
def pause_schedule(schedule_id: str):
    """Pauses an automated schedule."""
    ok = scheduler_mgr.pause_schedule(schedule_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {"status": "paused"}


@app.post("/api/schedules/{schedule_id}/resume")
def resume_schedule(schedule_id: str):
    """Resumes a paused schedule."""
    ok = scheduler_mgr.resume_schedule(schedule_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {"status": "resumed"}


@app.post("/api/schedules/{schedule_id}/trigger")
def trigger_schedule(schedule_id: str):
    """Manually forces the next action immediately (e.g. force rebuild or force teardown)."""
    try:
        res = scheduler_mgr.trigger_action(schedule_id)
        return res
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/schedules/{schedule_id}")
def delete_schedule(schedule_id: str, destroy_droplet: bool = True):
    """Deletes a schedule and optionally destroys its active droplet."""
    ok = scheduler_mgr.delete_schedule(schedule_id, destroy_droplet=destroy_droplet)
    if not ok:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {"status": "deleted"}


# ---------------------------------------------------------
# Deployment Background Engine & SSE Streaming (RQ & Redis)
# ---------------------------------------------------------
@app.post("/api/deploy")
def start_deployment(payload: DeployPayload, bg: BackgroundTasks):
    """
    Triggers a sensor deployment (DigitalOcean or GCP, per payload.provider).
    Enqueues to dedicated RQ worker on Redis or runs in fallback thread.
    """
    task_id = str(uuid.uuid4())
    payload_dict = payload.dict()
    provider = payload_dict.get("provider", "digitalocean")
    sensor_type = payload_dict.get("sensor_type", "cowrie")
    sensor_name = payload_dict.get("name") or f"tpot-{sensor_type}-sensor"
    job_fn = run_gcp_deployment_job if provider == "gcp" else run_do_deployment_job
    provider_label = "GCP" if provider == "gcp" else "DigitalOcean"
    record_task(
        task_id=task_id,
        task_type=f"{provider}_deploy",
        type_label=f"{provider_label} ({sensor_type.upper()})",
        sensor_name=sensor_name,
        sensor_type=sensor_type,
        status="queued"
    )
    if is_redis_available():
        task_queue.enqueue(job_fn, task_id, payload_dict, job_timeout=3600)
        logger.info(f"Enqueued {provider_label} deployment task {task_id} to Redis queue.")
    else:
        logger.warning(f"Redis offline; running deployment task {task_id} in background thread fallback.")
        thread = threading.Thread(target=job_fn, args=(task_id, payload_dict), daemon=True)
        thread.start()

    return {"status": "started", "task_id": task_id}


@app.get("/api/deploy/stream/{task_id}")
async def stream_deployment(task_id: str):
    """
    Server-Sent Events (SSE) endpoint for streaming live deployment logs.
    Replays historical logs from Redis/memory and listens to Redis Pub/Sub for real-time events.
    """
    async def event_generator():
        # 1. Replay historical logs
        history = get_task_logs(task_id)
        for entry in history:
            yield f"event: log\ndata: {json.dumps(entry)}\n\n"

        meta = get_task_status(task_id)
        if meta.get("status") in ("completed", "failed"):
            final_payload = json.dumps({
                "status": meta["status"],
                "done": True,
                "deployed": meta.get("deployed", []),
                "error": meta.get("error")
            })
            yield f"event: complete\ndata: {final_payload}\n\n"
            return

        # 2. Live streaming via Redis Pub/Sub
        if is_redis_available():
            pubsub = redis_conn.pubsub()
            pubsub.subscribe(f"deploy:{task_id}:stream")
            last_keepalive = time.time()
            try:
                while True:
                    msg = await asyncio.to_thread(pubsub.get_message, ignore_subscribe_messages=True, timeout=1.0)
                    if msg and msg.get("data"):
                        raw = msg["data"]
                        try:
                            data_obj = json.loads(raw)
                        except Exception:
                            data_obj = {"message": raw}

                        if data_obj.get("done") and data_obj.get("status") in ("completed", "failed"):
                            yield f"event: complete\ndata: {json.dumps(data_obj)}\n\n"
                            break
                        else:
                            yield f"event: log\ndata: {json.dumps(data_obj)}\n\n"

                    now = time.time()
                    if now - last_keepalive >= 15:
                        yield ": keep-alive\n\n"
                        last_keepalive = now

                    cur_meta = get_task_status(task_id)
                    if cur_meta.get("status") in ("completed", "failed"):
                        final_payload = json.dumps({
                            "status": cur_meta["status"],
                            "done": True,
                            "deployed": cur_meta.get("deployed", []),
                            "error": cur_meta.get("error")
                        })
                        yield f"event: complete\ndata: {final_payload}\n\n"
                        break

                    await asyncio.sleep(0.1)
            finally:
                try:
                    pubsub.unsubscribe()
                    pubsub.close()
                except Exception:
                    pass
        else:
            # Fallback polling loop if Redis is unavailable
            last_idx = len(history)
            while True:
                await asyncio.sleep(1.0)
                cur_logs = get_task_logs(task_id)
                while last_idx < len(cur_logs):
                    yield f"event: log\ndata: {json.dumps(cur_logs[last_idx])}\n\n"
                    last_idx += 1

                meta = get_task_status(task_id)
                if meta.get("status") in ("completed", "failed"):
                    final_payload = json.dumps({
                        "status": meta["status"],
                        "done": True,
                        "deployed": meta.get("deployed", []),
                        "error": meta.get("error")
                    })
                    yield f"event: complete\ndata: {final_payload}\n\n"
                    break
                yield ": keep-alive\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ---------------------------------------------------------
# Dedicated RQ Worker & Tags Endpoints
# ---------------------------------------------------------
@app.get("/api/workers")
def get_workers_status():
    """Returns real-time status of Redis and dedicated RQ workers (FragForge architecture)."""
    return {
        "redis": get_redis_info(),
        "queue": get_queue_stats(),
        "workers": list_active_workers(),
        "tasks": get_recent_tasks(limit=20)
    }


@app.get("/api/tasks")
def list_recent_tasks():
    """List recent deployment, teardown, and maintenance tasks from Redis queue."""
    return {
        "tasks": get_recent_tasks(limit=50),
        "queue": get_queue_stats()
    }


@app.get("/api/tasks/{task_id}")
def get_task_details(task_id: str):
    """Retrieve details and historical logs for a specific task."""
    meta = get_task_status(task_id)
    logs = get_task_logs(task_id)
    return {
        "task": meta,
        "logs": logs
    }


@app.get("/api/tags")
def list_tags():
    """List tags stored in SQLite database."""
    opts = db_get_cloud_options()
    tag_names = opts.get("tags", [])
    return {"tags": [{"name": t} for t in tag_names]}


@app.post("/api/tags")
def create_tag(payload: Dict[str, str]):
    """Ensure a DigitalOcean tag exists."""
    name = payload.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="Tag name is required")
    client = get_do_client()
    tag = client.ensure_tag(name)
    return {"status": "success", "tag": tag}


@app.get("/api/fleet/history")
def get_fleet_history(limit: int = 50):
    """Retrieve historical sensor lifecycle telemetry recorded in SQLite."""
    return {
        "history": db_get_fleet_history(limit=limit)
    }

