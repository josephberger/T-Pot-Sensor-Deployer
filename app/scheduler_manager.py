import logging
import random
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

from app.config import (
    LOGS_DIR, load_config, get_do_token, get_local_ssh_pubkey, DB_PATH,
    get_gcp_project_id, gcp_credentials_available,
)
from app.db import (
    db_get_all_schedules,
    db_get_schedule,
    db_save_schedule,
    db_delete_schedule,
    db_save_active_droplet,
    db_delete_active_droplet,
    db_get_active_droplet,
    db_delete_lease,
)
from app.ttl_manager import TTLManager, parse_duration, format_remaining
from app.models import Sensor, SensorStatus
from app.ansible_runner import ansible_available, playbook_for, private_key_path, supported_sensor_types
from app.provisioning import configure_and_verify
from app.cloud import get_client

logger = logging.getLogger("scheduler")

# Available region pool for geographic rotation
DEFAULT_REGION_POOL = ["nyc1", "nyc3", "sfo3", "ams3", "fra1", "lon1", "sgp1"]

# Operational Safeguards
MAX_CONCURRENT_PROVISIONING = 3   # Concurrency cap on in-flight creations
PROVISIONING_TIMEOUT_MINUTES = 30  # Timeout for in-flight creation + SSH configuration + health checks
MAX_CONSECUTIVE_FAILURES = 3      # Consecutive failures before circuit breaker trips
# The reconciliation tick interval lives in worker.py's SchedulerThread (the only caller of
# reconcile_tick); there is no standalone loop here (see the lifespan comment in app/api.py).

# TTL backstop lease for campaign droplets: a campaign's only lifecycle control is its own
# next_action_at state machine, ticked every 10s by worker.py. If that tick stops advancing (a crashed
# worker, a bug, a campaign paused while its sensor is active), nothing else ever destroys the droplet -
# unlike a manual deploy, which always gets an independent TTL lease. Mirrors that precedent: a lease is
# recorded the moment a droplet/instance exists (before configuration even starts), extended as the
# cycle progresses, and cancelled on any real teardown. See _schedule_backstop_lease/_cancel_backstop_lease.
PROVISIONING_BACKSTOP_MINUTES = PROVISIONING_TIMEOUT_MINUTES + 15  # slack past the tick's own 30m deadline
CAMPAIGN_BACKSTOP_BUFFER_SECONDS = 24 * 3600   # slack added past active_duration once a sensor is active
PAUSE_BACKSTOP_SECONDS = 30 * 86400            # generous ceiling while a campaign is paused with a live sensor

# One lock for every SchedulerManager in this process (the worker builds a fresh instance per tick),
# and the set of schedules whose sensor is currently being configured by a live background thread.
_SCHEDULER_LOCK = threading.RLock()
_CONFIGURING: set = set()

PRESETS = {
    "weekly_24h": {
        "id": "weekly_24h",
        "name": "Weekly 24h Cowrie Trap",
        "sensor_type": "cowrie",
        "description": "Runs Cowrie (SSH/Telnet) for 24 hours, destroys, and rebuilds at the same day & time 1 week later with a fresh dynamic IP.",
        "active_duration": "24h",
        "mode": "weekly",
        "cooldown_duration": "144h",  # 6 days
        "cooldown_min": "144h",
        "cooldown_max": "144h",
        "rotate_regions": False
    },
    "shift_8h_random": {
        "id": "shift_8h_random",
        "name": "8h Shift / 48-72h Randomized Rebuild",
        "sensor_type": "cowrie",
        "description": "Runs for 8 hours, destroys, then rebuilds randomly between 48 and 72 hours later with geographic rotation to evade scanning patterns.",
        "active_duration": "8h",
        "mode": "random_window",
        "cooldown_min": "48h",
        "cooldown_max": "72h",
        "rotate_regions": True
    },
    "daily_6h": {
        "id": "daily_6h",
        "name": "Daily 6h Prime-Time Trap",
        "sensor_type": "cowrie",
        "description": "Runs for 6 hours daily during peak scanner windows, destroys for 18 hours, then rebuilds with a new IP.",
        "active_duration": "6h",
        "mode": "interval",
        "cooldown_duration": "18h",
        "cooldown_min": "18h",
        "cooldown_max": "18h",
        "rotate_regions": False
    },
    "dionaea_malware_48h": {
        "id": "dionaea_malware_48h",
        "name": "Dionaea 48h Malware Trap (SMB/RPC)",
        "sensor_type": "dionaea",
        "description": "Runs Dionaea for 48 hours capturing WannaCry/SMB malware, destroys, and rebuilds randomly 48-72h later on a fresh IP.",
        "active_duration": "48h",
        "mode": "random_window",
        "cooldown_min": "48h",
        "cooldown_max": "72h",
        "rotate_regions": True
    },
    "conpot_scada_weekend": {
        "id": "conpot_scada_weekend",
        "name": "Conpot Weekend SCADA/ICS Deception",
        "sensor_type": "conpot",
        "description": "Runs Conpot (Modbus/S7/BACnet) for 60 hours over weekends when industrial OT networks face stealthy reconnaissance, then tears down until next week.",
        "active_duration": "60h",
        "mode": "weekly",
        "cooldown_duration": "108h",
        "cooldown_min": "108h",
        "cooldown_max": "108h",
        "rotate_regions": True
    },
    "elasticpot_cloud_trap": {
        "id": "elasticpot_cloud_trap",
        "name": "Elasticpot Cloud RCE Trap",
        "sensor_type": "elasticpot",
        "description": "Runs Elasticpot on port 9200 for 12 hours trapping cloud exploit scans, destroys, and rebuilds 24-48 hours later.",
        "active_duration": "12h",
        "mode": "random_window",
        "cooldown_min": "24h",
        "cooldown_max": "48h",
        "rotate_regions": True
    }
}


class SchedulerManager:
    """
    Manages automated recurring and interval honeypot deployment schedules.
    Enforces anti-fingerprinting deception: honeypots run for an active window,
    are destroyed, and rebuild on fresh dynamic IPs (never using reserved IPs).
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._lock = _SCHEDULER_LOCK

    def _load_schedules(self) -> Dict[str, dict]:
        return db_get_all_schedules(self.db_path)

    def _save_schedules(self, schedules: Dict[str, dict]):
        with self._lock:
            current = db_get_all_schedules(self.db_path)
            for sid in list(current.keys()):
                if sid not in schedules:
                    db_delete_schedule(sid, self.db_path)
            for sid, s in schedules.items():
                db_save_schedule(s, self.db_path)

    def _log_event(self, schedule_id: str, message: str, level: str = "INFO"):
        log_path = LOGS_DIR / "scheduler.log"
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] [{level}] [Schedule {schedule_id}] {message}\n")
        except Exception:
            pass

    def list_schedules(self) -> List[dict]:
        """Returns all schedules augmented with live status and time calculations."""
        with self._lock:
            schedules = self._load_schedules()

        now = datetime.now(timezone.utc)
        result = []

        for sid, s in schedules.items():
            entry = dict(s)
            state = entry.get("state", {})
            state.pop("hive_token", None)  # per-sensor secret; never sent to the browser
            next_action_at_str = state.get("next_action_at")

            remaining_sec = 0
            if next_action_at_str:
                try:
                    next_dt = datetime.fromisoformat(next_action_at_str)
                    remaining_sec = max(0, (next_dt - now).total_seconds())
                except Exception:
                    remaining_sec = 0

            state["remaining_seconds"] = remaining_sec
            state["remaining_formatted"] = format_remaining(remaining_sec)
            entry["state"] = state
            result.append(entry)

        return result

    def get_schedule(self, schedule_id: str) -> Optional[dict]:
        with self._lock:
            schedules = self._load_schedules()
            s = schedules.get(schedule_id)
            if not s:
                return None

        entry = dict(s)
        state = entry.get("state", {})
        state.pop("hive_token", None)  # per-sensor secret; never sent to the browser
        next_action_at_str = state.get("next_action_at")
        now = datetime.now(timezone.utc)

        remaining_sec = 0
        if next_action_at_str:
            try:
                next_dt = datetime.fromisoformat(next_action_at_str)
                remaining_sec = max(0, (next_dt - now).total_seconds())
            except Exception:
                remaining_sec = 0

        state["remaining_seconds"] = remaining_sec
        state["remaining_formatted"] = format_remaining(remaining_sec)
        entry["state"] = state
        return entry

    def create_schedule(self, payload: dict) -> dict:
        """
        Creates a new recurring honeypot deployment schedule.
        """
        schedule_id = f"sched-{uuid.uuid4().hex[:8]}"
        name = payload.get("name") or f"Campaign-{schedule_id}"
        preset_id = payload.get("preset_id")

        preset = PRESETS.get(preset_id) or {}
        timing_payload = payload.get("timing") or {}
        config_payload = payload.get("config") or {}

        # Resolve timing
        mode = timing_payload.get("mode") or preset.get("mode") or "interval"
        active_dur = timing_payload.get("active_duration") or preset.get("active_duration") or "24h"
        cooldown_dur = timing_payload.get("cooldown_duration") or preset.get("cooldown_duration") or "144h"
        cooldown_min = timing_payload.get("cooldown_min") or preset.get("cooldown_min") or "48h"
        cooldown_max = timing_payload.get("cooldown_max") or preset.get("cooldown_max") or "72h"

        active_dur_sec = parse_duration(active_dur)
        cooldown_dur_sec = parse_duration(cooldown_dur)
        cooldown_min_sec = parse_duration(cooldown_min)
        cooldown_max_sec = parse_duration(cooldown_max)

        rotate_regions = config_payload.get("rotate_regions", preset.get("rotate_regions", False))
        regions_pool = config_payload.get("regions_pool") or DEFAULT_REGION_POOL

        timing = {
            "mode": mode,
            "active_duration": active_dur,
            "active_duration_seconds": active_dur_sec,
            "cooldown_duration": cooldown_dur,
            "cooldown_duration_seconds": cooldown_dur_sec,
            "cooldown_min": cooldown_min,
            "cooldown_min_seconds": cooldown_min_sec,
            "cooldown_max": cooldown_max,
            "cooldown_max_seconds": cooldown_max_sec
        }

        from app.sensor_types import get_sensor_type

        sensor_type = config_payload.get("sensor_type") or payload.get("sensor_type") or preset.get("sensor_type") or "cowrie"
        st_info = get_sensor_type(sensor_type)
        resolved_size = config_payload.get("size") or preset.get("size") or st_info.get("recommended_size_do", "s-1vcpu-2gb")
        name_prefix = config_payload.get("name_prefix") or f"{sensor_type}-sched"

        provider = config_payload.get("provider") or payload.get("provider") or "digitalocean"
        # Playbooks are cloud-agnostic, so this check is just as valid for a GCP campaign now that GCP
        # campaigns are on the same create-then-Ansible pipeline as DO's (it used to be DO-only, from
        # when a GCP campaign meant something entirely different - the old Terraform path).
        if not playbook_for("sensors", sensor_type):
            raise ValueError(
                f"No Ansible playbook for sensor type '{sensor_type}' yet, so this campaign could never deploy. "
                f"Supported: {', '.join(supported_sensor_types()) or 'none'}"
            )
        if provider == "gcp" and (not get_gcp_project_id() or not gcp_credentials_available()):
            raise ValueError(
                "GCP project id or service account key not configured, so this campaign could never deploy. "
                "Set both in Admin before creating a GCP campaign."
            )
        config = {
            "provider": provider,
            "sensor_type": sensor_type,
            "region": config_payload.get("region", "us-central1" if provider == "gcp" else "nyc1"),
            "zone": config_payload.get("zone", "us-central1-a"),
            "machine_type": config_payload.get("machine_type") or st_info.get("recommended_size_gcp", "e2-small"),
            "project_id": config_payload.get("project_id"),
            "size": resolved_size,
            "image": config_payload.get("image", "ubuntu-24-04-x64"),
            "name_prefix": name_prefix,
            "attach_firewall": config_payload.get("attach_firewall", True),
            "restrict_ssh": config_payload.get("restrict_ssh", False),
            "auto_register_hive": config_payload.get("auto_register_hive", True),
            "admin_ssh_port": int(config_payload.get("admin_ssh_port", 64295)),
            "rotate_regions": rotate_regions,
            "regions_pool": regions_pool,
            "last_region_idx": 0
        }

        start_now = payload.get("start_immediately", True)
        now = datetime.now(timezone.utc)

        state = {
            "status": "pending_deploy" if start_now else "cooling_down",
            "sensor_type": sensor_type,
            "current_droplet_id": None,
            "current_droplet_name": None,
            "current_public_ip": None,
            "current_sensor_user": None,
            "cycle_number": 0,
            "cycle_started_at": None,
            "provisioning_started_at": None,
            "provisioning_deadline_at": None,
            "consecutive_failures": 0,
            "next_action": "deploy",
            "next_action_at": now.isoformat() if start_now else self._calculate_next_rebuild(timing, None).isoformat(),
            "last_action_at": now.isoformat(),
            "last_message": "Schedule created. Ready to launch cycle 1.",
            "history": []
        }

        schedule_obj = {
            "id": schedule_id,
            "name": name,
            "sensor_type": sensor_type,
            "description": payload.get("description", preset.get("description", "")),
            "preset_id": preset_id,
            "enabled": True,
            "created_at": now.isoformat(),
            "timing": timing,
            "config": config,
            "state": state
        }

        with self._lock:
            schedules = self._load_schedules()
            schedules[schedule_id] = schedule_obj
            self._save_schedules(schedules)

        self._log_event(schedule_id, f"Created schedule '{name}' (Mode: {mode}, Active: {active_dur}). Start immediately: {start_now}")
        return schedule_obj

    def pause_schedule(self, schedule_id: str) -> bool:
        with self._lock:
            schedules = self._load_schedules()
            if schedule_id not in schedules:
                return False
            s = schedules[schedule_id]
            s["enabled"] = False
            # Pausing only disables the timers (reconcile_tick's Phase 2 skips disabled schedules); it
            # must NOT overwrite status. It used to set status = "paused", which broke both ends:
            # resume_schedule only recognizes a live sensor when status == "active", so pause + resume
            # on an active campaign fell through to "deploy now" and orphaned the running droplet (its
            # current_droplet_id overwritten by the next dispatch); and pausing mid-provisioning took the
            # cycle out of Phase 1's reconcile, so its droplet was never configured or cleaned up (in
            # waiting_ip it had no lease yet, so nothing would ever destroy it). An in-flight cycle now
            # finishes normally - see the paused branch in _finalize_active.
            if s["state"].get("status") in ("provisioning", "tearing_down"):
                s["state"]["last_message"] = "Paused. The in-flight cycle finishes first; nothing new starts until resumed."
            else:
                s["state"]["last_message"] = "Schedule paused by user."
            # Pausing freezes the state machine that would otherwise destroy this droplet on schedule -
            # its own next_action_at timer never fires while disabled (reconcile_tick's Phase 2 skips
            # disabled schedules outright). Without this, the backstop lease from _finalize_active (just
            # active_duration + 24h slack) could fire *because* of the pause, stranding the campaign
            # mid-pause instead of protecting it. Extend it to a generous, but still finite, ceiling
            # instead - a brief pause never approaches it; a truly forgotten paused campaign still gets
            # cleaned up eventually rather than billing forever.
            if s["state"].get("status") == "active" and s["state"].get("current_droplet_id"):
                self._schedule_backstop_lease(s, PAUSE_BACKSTOP_SECONDS)
            self._save_schedules(schedules)

        self._log_event(schedule_id, "Schedule paused.")
        return True

    def resume_schedule(self, schedule_id: str) -> bool:
        with self._lock:
            schedules = self._load_schedules()
            if schedule_id not in schedules:
                return False
            s = schedules[schedule_id]
            s["enabled"] = True
            st = s["state"]
            st["consecutive_failures"] = 0  # Reset circuit breaker
            now = datetime.now(timezone.utc)
            if st.get("current_droplet_id") and st.get("current_public_ip") and st.get("status") == "active":
                # Reality check, not a blind trust of the stored status: the droplet this claims is
                # active may be long gone - the pause-extended backstop lease finally fired, or someone
                # destroyed it by hand from Fleet while the campaign was paused. Either way, the
                # schedule's own destroy timer never runs again while paused, so without this check a
                # stale "active" status (pointing at nothing) would persist forever once resumed - no
                # sensor, no destroy timer, and no way to notice short of reading the code.
                row = db_get_active_droplet(st["current_droplet_id"], db_path=self.db_path)
                if row:
                    st["status"] = "active"
                    # Pausing may have extended the backstop lease to PAUSE_BACKSTOP_SECONDS; shrink it
                    # back down to the normal active-phase target now that the campaign is live again.
                    cycle_start_dt = None
                    if st.get("cycle_started_at"):
                        try:
                            cycle_start_dt = datetime.fromisoformat(st["cycle_started_at"])
                        except Exception:
                            pass
                    if cycle_start_dt:
                        active_sec = s.get("timing", {}).get("active_duration_seconds", 86400)
                        remaining = (cycle_start_dt + timedelta(seconds=active_sec + CAMPAIGN_BACKSTOP_BUFFER_SECONDS) - now).total_seconds()
                        if remaining > 0:
                            self._schedule_backstop_lease(s, remaining)
                    st["last_message"] = "Schedule resumed. Circuit breaker reset."
                else:
                    self._log_event(
                        schedule_id,
                        f"Resumed while 'active', but sensor {st.get('current_droplet_name')} "
                        f"(id {st.get('current_droplet_id')}) no longer exists - the TTL backstop likely "
                        f"fired while paused, or it was destroyed by hand. Treating as lost; cooling down "
                        f"for a fresh cycle instead of a phantom active state.",
                        "WARN",
                    )
                    cycle_start_dt = None
                    if st.get("cycle_started_at"):
                        try:
                            cycle_start_dt = datetime.fromisoformat(st["cycle_started_at"])
                        except Exception:
                            pass
                    st["current_droplet_id"] = None
                    st["current_droplet_name"] = None
                    st["current_public_ip"] = None
                    st["current_sensor_user"] = None
                    st["status"] = "cooling_down"
                    st["next_action"] = "deploy"
                    st["next_action_at"] = self._calculate_next_rebuild(s.get("timing", {}), cycle_start_dt).isoformat()
                    st["last_message"] = "Schedule resumed: its sensor was lost while paused (see log). Cooling down for a fresh cycle."
            elif st.get("status") == "provisioning":
                pass  # Reconciler will pick it up
            else:
                st["status"] = "cooling_down"
                st["next_action"] = "deploy"
                st["next_action_at"] = now.isoformat()
                st["last_message"] = "Schedule resumed. Circuit breaker reset."
            self._save_schedules(schedules)

        self._log_event(schedule_id, "Schedule resumed (circuit breaker reset).")
        return True

    def trigger_action(self, schedule_id: str) -> dict:
        """
        Manually trigger the next action immediately (e.g. force deploy if cooling down,
        or force destroy if active).
        """
        # The in-flight check and the claim that backs it off must happen under ONE lock acquisition.
        # Checking status, releasing the lock, and only then dispatching left a window where several
        # concurrent calls (e.g. a mashed or double-clicked "Trigger now") all read the same
        # pre-dispatch status, all passed the check, and all dispatched - found live: 5 rapid concurrent
        # calls created 5 real droplets for one schedule slot; the schedule can only remember one
        # current_droplet_id, so the other 4 became untracked, un-leased orphans. Claiming the in-flight
        # status here, still under the lock, closes that: a concurrent call now sees the claimed status
        # and backs off before ever dispatching.
        with self._lock:
            schedules = self._load_schedules()
            if schedule_id not in schedules:
                raise ValueError("Schedule not found")
            s = schedules[schedule_id]
            state = s.get("state", {})
            status = state.get("status")

            if status == "provisioning":
                return {
                    "status": "warning",
                    "action": "none",
                    "message": "Deployment is already in-flight. Reconciler is awaiting public IP assignment.",
                    "schedule": s
                }
            if status == "tearing_down":
                return {
                    "status": "warning",
                    "action": "none",
                    "message": "Teardown is already in-flight. Please wait.",
                    "schedule": s
                }

            is_active = bool(state.get("current_droplet_id")) or (s.get("config", {}).get("provider") == "gcp" and status == "active")
            state["status"] = "tearing_down" if is_active else "provisioning"
            if not is_active:
                state["consecutive_failures"] = 0
                s["enabled"] = True
            schedules[schedule_id] = s
            self._save_schedules(schedules)

        if is_active:
            ok, msg = self._execute_destroy(s)
            action_done = "destroy"
        else:
            ok, msg = self._dispatch_deploy(s)
            action_done = "deploy"

        with self._lock:
            schedules = self._load_schedules()
            schedules[schedule_id] = s
            self._save_schedules(schedules)

        return {"status": "success" if ok else "error", "action": action_done, "message": msg, "schedule": s}

    def delete_schedule(self, schedule_id: str, destroy_droplet: bool = True) -> bool:
        with self._lock:
            schedules = self._load_schedules()
            if schedule_id not in schedules:
                return False
            s = schedules[schedule_id]
            del schedules[schedule_id]
            self._save_schedules(schedules)

        # Teardown active or in-flight infrastructure if requested
        st = s.get("state", {})
        is_active_or_in_flight = bool(st.get("current_droplet_id")) or (
            s.get("config", {}).get("provider") == "gcp" and st.get("status") in ["active", "provisioning"]
        )
        if destroy_droplet and is_active_or_in_flight:
            try:
                self._execute_destroy(s)
            except Exception as e:
                logger.error(f"Error destroying sensor during schedule deletion: {e}")

        self._log_event(schedule_id, "Schedule deleted.")
        return True

    def _schedule_backstop_lease(self, schedule: dict, ttl_seconds: float):
        """(Re)schedule the TTL backstop lease for this cycle's droplet/instance - the independent 60s
        sweep (jobs.ttl_sweep_job) is the only thing that will ever destroy it if this campaign's own
        tick stops advancing (a crashed worker, a bug, a campaign paused while its sensor is active).
        Provider-aware: `region` carries the GCP zone for a GCP row, the same convention active_droplets/
        leases already use everywhere else."""
        state = schedule.get("state", {})
        config = schedule.get("config", {})
        droplet_id = state.get("current_droplet_id")
        droplet_name = state.get("current_droplet_name")
        if not droplet_id or ttl_seconds <= 0:
            return
        provider = config.get("provider", "digitalocean")
        region = config.get("zone") if provider == "gcp" else (state.get("current_region") or config.get("region"))
        try:
            TTLManager(self.db_path).schedule_lease(
                droplet_id=droplet_id,
                droplet_name=droplet_name or f"sensor-{droplet_id}",
                ttl_text=f"{int(ttl_seconds)}s",
                sensor_user=state.get("current_sensor_user"),
                sensor_type=config.get("sensor_type", "cowrie"),
                provider=provider,
                region=region,
            )
        except Exception as e:
            logger.warning(f"Could not schedule backstop lease for {droplet_name or droplet_id}: {e}")

    def _cancel_backstop_lease(self, droplet_id: Optional[int]):
        """Release the backstop lease on a real teardown, so the independent sweep never tries to
        destroy an already-gone resource. Deletes the lease row directly rather than going through
        TTLManager.cancel_lease() - that also writes a "Cancelled by User" fleet_history entry, which is
        inaccurate for an automated campaign teardown (and campaign teardowns don't write fleet_history
        today; not introducing that as an unplanned side effect here)."""
        if not droplet_id:
            return
        try:
            db_delete_lease(int(droplet_id), self.db_path)
        except Exception:
            pass

    def _calculate_next_rebuild(self, timing: dict, cycle_started_at_dt: Optional[datetime]) -> datetime:
        """
        Calculates when the next rebuild should happen according to the campaign timing.
        Anti-Fingerprinting guarantee: Dynamic IPs are always freshly assigned.
        """
        now = datetime.now(timezone.utc)
        mode = timing.get("mode", "interval")

        if mode == "weekly":
            # Rebuild at the same time a week later
            if cycle_started_at_dt:
                next_rebuild = cycle_started_at_dt + timedelta(days=7)
                if next_rebuild > now:
                    return next_rebuild
            # Fallback if past or None
            return now + timedelta(seconds=timing.get("cooldown_duration_seconds", 518400))

        elif mode == "random_window":
            # Random rebuild between min and max cooldown
            min_sec = timing.get("cooldown_min_seconds", 172800)
            max_sec = timing.get("cooldown_max_seconds", 259200)
            if max_sec < min_sec:
                max_sec = min_sec + 3600
            rand_sec = random.randint(min_sec, max_sec)
            return now + timedelta(seconds=rand_sec)

        else:
            # Standard interval
            cooldown_sec = timing.get("cooldown_duration_seconds", 86400)
            return now + timedelta(seconds=cooldown_sec)

    def _handle_failure(self, schedule: dict, error_msg: str):
        """
        Operational Safeguards: Circuit Breaker & Exponential Backoff.
        Increments consecutive failures.
        If threshold (MAX_CONSECUTIVE_FAILURES) reached: trips circuit breaker to 'suspended'.
        Otherwise: schedules retry with exponential backoff (5m, 10m, 20m...).
        """
        schedule_id = schedule["id"]
        state = schedule["state"]
        now = datetime.now(timezone.utc)

        failures = state.get("consecutive_failures", 0) + 1
        state["consecutive_failures"] = failures
        state["provisioning_started_at"] = None
        state["provisioning_deadline_at"] = None
        state["current_droplet_id"] = None
        state["current_droplet_name"] = None
        state["current_public_ip"] = None
        state["current_sensor_user"] = None
        state.pop("stage", None)
        state.pop("hive_token", None)

        if failures >= MAX_CONSECUTIVE_FAILURES:
            state["status"] = "suspended"
            schedule["enabled"] = False
            state["next_action"] = None
            state["next_action_at"] = None
            state["last_message"] = (
                f"Circuit breaker tripped after {failures} consecutive failures. "
                f"Campaign suspended to protect cloud resources. Error: {error_msg}"
            )
            self._log_event(
                schedule_id,
                f"CIRCUIT BREAKER TRIPPED ({failures}/{MAX_CONSECUTIVE_FAILURES} failures): "
                f"Campaign suspended. Reason: {error_msg}",
                "ERROR"
            )
        else:
            backoff_minutes = 5 * (2 ** (failures - 1))  # 5m, 10m, 20m
            retry_at = now + timedelta(minutes=backoff_minutes)
            state["status"] = "cooling_down"
            state["next_action"] = "deploy"
            state["next_action_at"] = retry_at.isoformat()
            state["last_message"] = (
                f"Deploy attempt failed ({failures}/{MAX_CONSECUTIVE_FAILURES}): {error_msg}. "
                f"Backoff retry in {backoff_minutes}m (at {retry_at.strftime('%H:%M:%S UTC')})."
            )
            self._log_event(
                schedule_id,
                f"Deployment failed ({failures}/{MAX_CONSECUTIVE_FAILURES}): {error_msg}. "
                f"Retrying with backoff in {backoff_minutes}m.",
                "WARN"
            )

    def _async_gcp_provision_worker(self, schedule_id: str, ctx: dict):
        """Background thread for a GCP cycle: create the instance, then configure it over SSH with
        Ansible, then join the same "ready" wait-loop DO's _async_do_configure_worker uses (below) to
        hand off to the now provider-generic _finalize_active/_abort_provisioning.

        Unlike DO, there's no non-blocking create step to split into its own tick-driven poll -
        GCPClient.create_instance() already blocks until the instance exists with an IP - so this one
        thread does what DO splits across _reconcile_provisioning's waiting_ip stage plus
        _async_do_configure_worker. ctx carries everything needed up front (the tick that spawned us may
        not have saved state yet): sensor_name, zone, machine_type, image, sensor_user, hive_token,
        sensor_type, admin_ssh_port, network_tags.
        """
        from app.gcp_client import GCPClient, GCP_SSH_USER
        from app.hive_manager import HiveManager
        from app.sensor_types import get_sensor_type

        client = GCPClient(project_id=get_gcp_project_id())
        _, local_pub = get_local_ssh_pubkey()
        instance = None
        error = None
        try:
            instance = client.create_instance(
                name=ctx["sensor_name"], zone=ctx["zone"], machine_type=ctx["machine_type"],
                image=ctx["image"], ssh_pubkey=local_pub, network_tags=ctx["network_tags"], wait_active=True,
            )
            now = datetime.now(timezone.utc)
            with self._lock:
                schedules = self._load_schedules()
                s = schedules.get(schedule_id)
                if not s:
                    logger.warning(f"Schedule {schedule_id} was removed during GCP provisioning. Tearing down...")
                    try:
                        client.destroy_instance(ctx["sensor_name"], ctx["zone"])
                    except Exception:
                        pass
                    return
                st = s["state"]
                if st.get("status") != "provisioning":
                    return  # superseded (e.g. deleted and recreated)
                st["current_droplet_id"] = instance["id"]
                st["current_public_ip"] = instance.get("public_ip")
                st["last_message"] = f"Instance {ctx['sensor_name']} is up at {instance.get('public_ip')}; configuring sensor over SSH (Ansible)..."
                # Record and publish the IP now (not after health), same principle as DO: the firewall
                # needs the feed to list this sensor before it can admit it to the Hive.
                try:
                    db_save_active_droplet(Sensor(
                        id=instance["id"], name=ctx["sensor_name"], public_ip=instance.get("public_ip"),
                        private_ip=instance.get("private_ip"), sensor_type=ctx["sensor_type"],
                        sensor_user=ctx["sensor_user"], provider="gcp", region=ctx["zone"],
                        size=ctx["machine_type"], tags=[f"sched-{schedule_id}", "tpot-sensor", f"type-{ctx['sensor_type']}"],
                        status=SensorStatus.PROVISIONING.value, created_at=now.isoformat(),
                    ), db_path=self.db_path)
                    from app.edl_manager import EDLManager
                    EDLManager().invalidate_cache()
                except Exception as db_err:
                    logger.warning(f"Could not record provisioning instance {instance['id']}: {db_err}")
                # Independent backstop, same as DO: covers "worker dies mid-Ansible-run".
                self._schedule_backstop_lease(s, PROVISIONING_BACKSTOP_MINUTES * 60)
                self._save_schedules(schedules)

            warnings = configure_and_verify(
                instance["public_ip"], ctx["sensor_type"],
                sensor_name=ctx["sensor_name"],
                hive_ip=load_config().get("hive_ip") or HiveManager().detect_hive_ip(),
                hive_port=64294,
                hive_cert=HiveManager().get_hive_certificate(),
                hive_token=ctx["hive_token"],
                admin_ssh_port=ctx["admin_ssh_port"],
                swap_size_gb=get_sensor_type(ctx["sensor_type"]).get("swap_size_gb", 2),
                remote_ssh_user=GCP_SSH_USER,
                on_log=lambda line: self._log_event(schedule_id, f"[ansible] {line}", "DEBUG"),
                on_step=lambda msg, pct: self._log_event(schedule_id, msg),
            )
            for w in warnings:
                self._log_event(schedule_id, f"WARNING: {w}", "WARN")
        except Exception as e:
            error = str(e) or type(e).__name__
            logger.warning(f"Campaign {schedule_id} GCP provisioning failed: {error}")

        # Same "ready" wait-loop pattern _async_do_configure_worker uses: the spawning tick may not have
        # saved state yet, so wait briefly for stage == "configuring" before handing off.
        try:
            deadline = time.monotonic() + 30
            while True:
                with self._lock:
                    schedules = self._load_schedules()
                    s = schedules.get(schedule_id)
                    if not s:
                        if instance:
                            try:
                                client.destroy_instance(ctx["sensor_name"], ctx["zone"])
                            except Exception:
                                pass
                        return
                    st = s["state"]
                    ready = (
                        st.get("status") == "provisioning" and st.get("stage") == "configuring"
                        and (instance is None or st.get("current_droplet_id") == instance.get("id"))
                    )
                    if ready:
                        if error:
                            self._abort_provisioning(s, None, error)
                        else:
                            try:
                                self._finalize_active(s, None)
                            except Exception as finalize_err:
                                logger.warning(f"Campaign {schedule_id} finalize failed: {finalize_err}")
                                self._abort_provisioning(s, None, f"Finalize failed: {finalize_err}")
                        self._save_schedules(schedules)
                        return
                if time.monotonic() > deadline:
                    logger.warning(f"Campaign {schedule_id}: cycle for instance {ctx['sensor_name']} was superseded; discarding result")
                    return
                time.sleep(0.5)
        finally:
            _CONFIGURING.discard(schedule_id)

    def _dispatch_deploy(self, schedule: dict) -> Tuple[bool, str]:
        """
        Non-blocking dispatch of a fresh deployment cycle for a schedule.
        Provisions cloud resources asynchronously and transitions status to 'provisioning'.
        Crucial requirement: NEVER allocate reserved IPs. Fresh dynamic IP only.
        """
        from app.do_client import DOClient
        from app.hive_manager import HiveManager

        schedule_id = schedule["id"]
        config = schedule["config"]
        timing = schedule["timing"]
        state = schedule["state"]
        provider = config.get("provider", "digitalocean")

        hive_mgr = HiveManager()
        now = datetime.now(timezone.utc)

        if provider == "gcp":
            from app.gcp_client import GCPClient
            from app.sensor_types import get_sensor_type

            sensor_type = config.get("sensor_type", "cowrie")
            st_info = get_sensor_type(sensor_type)

            # Preflight: refuse before creating (and paying for) anything we can't configure afterwards.
            # Same bar as a manual GCP deploy's preflight in app/jobs.py::run_gcp_deployment_job.
            if not playbook_for("sensors", sensor_type):
                msg = f"No Ansible playbook for sensor type '{sensor_type}' yet (supported: {', '.join(supported_sensor_types()) or 'none'})"
                self._handle_failure(schedule, msg)
                return False, msg
            if not ansible_available():
                self._handle_failure(schedule, "ansible-playbook is not installed in this container")
                return False, "ansible-playbook is not installed"
            if not private_key_path().exists():
                self._handle_failure(schedule, f"SSH private key not found at {private_key_path()}")
                return False, "SSH private key not found"
            project_id = get_gcp_project_id()
            if not project_id or not gcp_credentials_available():
                self._handle_failure(schedule, "GCP project id or service account key not configured")
                return False, "GCP not configured"
            _, local_pub = get_local_ssh_pubkey()
            if not local_pub:
                self._handle_failure(schedule, "SSH public key not found")
                return False, "SSH public key not found"

            client = GCPClient(project_id=project_id)
            zone = config.get("zone", "us-central1-a")
            machine_type = config.get("machine_type") or st_info.get("recommended_size_gcp", "e2-small")
            image = load_config().get("gcp_image") or "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64"

            try:
                self._log_event(schedule_id, "Dispatching scheduled GCP honeypot deployment...")

                if config.get("attach_firewall", True):
                    try:
                        hive_ip = load_config().get("hive_ip") or hive_mgr.detect_hive_ip()
                        client.ensure_sensor_firewall(
                            hive_ip=hive_ip if config.get("restrict_ssh", False) else None,
                            restrict_ssh=config.get("restrict_ssh", False),
                            admin_ssh_port=config.get("admin_ssh_port", 64295),
                        )
                    except Exception as fw_err:
                        self._log_event(schedule_id, f"Firewall configuration failed: {fw_err}", "WARN")

                name_prefix = config.get("name_prefix") or f"{sensor_type}-sched"
                creds = hive_mgr.generate_sensor_credentials(name_prefix=name_prefix)
                sensor_name = creds["username"]
                sensor_user = creds["username"]

                if config.get("auto_register_hive", True):
                    ok, msg = hive_mgr.register_sensor(creds)
                    self._log_event(schedule_id, f"Registered sensor '{sensor_user}' into Hive: {msg}")

                deadline = now + timedelta(minutes=PROVISIONING_TIMEOUT_MINUTES)
                state["status"] = "provisioning"
                # No separate waiting_ip stage for GCP (see _reconcile_provisioning's docstring) - go
                # straight to "configuring" and hand off to the background thread, which fills in
                # current_droplet_id/current_public_ip itself once the instance actually exists.
                state["stage"] = "configuring"
                state["current_droplet_id"] = None
                state["current_droplet_name"] = sensor_name
                state["current_sensor_user"] = sensor_user
                state["current_public_ip"] = None
                state["provisioning_started_at"] = now.isoformat()
                state["provisioning_deadline_at"] = deadline.isoformat()
                state["next_action"] = None
                state["next_action_at"] = None
                state["last_action_at"] = now.isoformat()
                state["last_message"] = f"Creating GCP instance {sensor_name} in {zone}..."

                ctx = {
                    "sensor_name": sensor_name, "zone": zone, "machine_type": machine_type, "image": image,
                    "sensor_user": sensor_user, "hive_token": creds["tpot_hive_user"], "sensor_type": sensor_type,
                    "admin_ssh_port": config.get("admin_ssh_port", 64295),
                    "network_tags": ["tpot-sensor", f"tpot-{sensor_type}-sensor"],
                }
                _CONFIGURING.add(schedule_id)
                threading.Thread(target=self._async_gcp_provision_worker, args=(schedule_id, ctx), daemon=True).start()
                return True, f"Dispatched GCP instance creation for {sensor_name}"

            except Exception as e:
                if 'sensor_user' in locals() and config.get("auto_register_hive", True):
                    hive_mgr.deregister_sensor(sensor_user)  # don't leave an orphaned login behind
                self._handle_failure(schedule, str(e))
                return False, str(e)

        # DigitalOcean Deployment
        token = get_do_token()
        if not token:
            self._handle_failure(schedule, "DigitalOcean API token not configured")
            return False, "DigitalOcean API token not configured"

        client = DOClient(token=token)
        sensor_type = config.get("sensor_type", "cowrie")

        try:
            self._log_event(schedule_id, "Dispatching scheduled honeypot deployment cycle...")

            # 0. Preflight: refuse before creating (and paying for) anything we can't configure afterwards.
            if not playbook_for("sensors", sensor_type):
                raise RuntimeError(f"No Ansible playbook for sensor type '{sensor_type}' yet (supported: {', '.join(supported_sensor_types()) or 'none'})")
            if not ansible_available():
                raise RuntimeError("ansible-playbook is not installed in this container")
            if not private_key_path().exists():
                raise RuntimeError(f"SSH private key not found at {private_key_path()} (put it in secrets/ssh_key)")
            _, local_pub = get_local_ssh_pubkey()
            if not local_pub:
                raise RuntimeError("SSH public key not found (put it in secrets/ssh_key.pub)")

            # 1. Determine region (with optional rotation)
            region = config.get("region", "nyc1")
            if config.get("rotate_regions"):
                pool = config.get("regions_pool") or DEFAULT_REGION_POOL
                last_idx = config.get("last_region_idx", 0)
                region = pool[last_idx % len(pool)]
                config["last_region_idx"] = last_idx + 1
                self._log_event(schedule_id, f"Rotating to region: {region.upper()}")

            # 2. SSH key: always the deployer's own key. Ansible logs in with it, and a droplet created
            #    without a key makes DigitalOcean email a root password.
            ssh_key_ids = [client.ensure_ssh_key("tpot-deployer-key", local_pub)]

            hive_ip = load_config().get("hive_ip") or hive_mgr.detect_hive_ip()
            hive_cert = hive_mgr.get_hive_certificate()

            # 3. Cloud firewall (a failure here must be visible, not swallowed)
            if config.get("attach_firewall", True):
                try:
                    client.ensure_sensor_firewall(
                        sensor_type=sensor_type,
                        hive_ip=hive_ip,
                        restrict_ssh=config.get("restrict_ssh", False),
                        admin_ssh_port=config.get("admin_ssh_port", 64295)
                    )
                except Exception as fw_err:
                    self._log_event(schedule_id, f"Firewall configuration failed: {fw_err}", "WARN")

            # 4. Hive credentials & registration
            name_prefix = config.get("name_prefix") or f"{sensor_type}-sched"
            creds = hive_mgr.generate_sensor_credentials(name_prefix=name_prefix)
            sensor_name = creds["username"]
            sensor_user = creds["username"]

            if config.get("auto_register_hive", True):
                ok, msg = hive_mgr.register_sensor(creds)
                self._log_event(schedule_id, f"Registered sensor '{sensor_user}' into Hive: {msg}")

            # 5. Create a BARE droplet (no user-data): STRICTLY DYNAMIC IP, NON-BLOCKING. Configuration happens
            #    over SSH with Ansible once the droplet has an IP (see _reconcile_provisioning).
            self._log_event(schedule_id, f"Creating droplet '{sensor_name}' [{sensor_type}] in {region} ({config.get('size')})...")
            try:
                droplet = client.create_droplet(
                    name=sensor_name,
                    region=region,
                    size=config.get("size", "s-1vcpu-2gb"),
                    image=config.get("image", "ubuntu-24-04-x64"),
                    ssh_keys=ssh_key_ids,
                    tags=["tpot-sensor", f"tpot-{sensor_type}-sensor", f"type-{sensor_type}", f"sched-{schedule_id}"],
                    wait_active=False
                )
            except Exception:
                if config.get("auto_register_hive", True):
                    hive_mgr.deregister_sensor(sensor_user)  # don't leave an orphaned login behind
                raise

            droplet_id = droplet["id"]
            deadline = now + timedelta(minutes=PROVISIONING_TIMEOUT_MINUTES)

            state["status"] = "provisioning"
            state["current_droplet_id"] = droplet_id
            state["current_droplet_name"] = sensor_name
            state["current_sensor_user"] = sensor_user
            state["current_region"] = region  # the rotated region, which config["region"] doesn't reflect
            state["hive_token"] = creds["tpot_hive_user"]  # needed later by the configure step; stripped from API output
            state["stage"] = "waiting_ip"
            state["current_public_ip"] = None
            state["provisioning_started_at"] = now.isoformat()
            state["provisioning_deadline_at"] = deadline.isoformat()
            state["next_action"] = None
            state["next_action_at"] = None
            state["last_action_at"] = now.isoformat()
            state["last_message"] = f"Droplet {sensor_name} (ID {droplet_id}) created. Provisioning in-flight, awaiting dynamic IP..."

            self._log_event(schedule_id, f"Droplet requested (ID {droplet_id}). State set to 'provisioning' with {PROVISIONING_TIMEOUT_MINUTES}m deadline.")
            return True, f"Dispatched droplet {sensor_name} (ID {droplet_id})"

        except Exception as e:
            self._handle_failure(schedule, str(e))
            return False, str(e)

    # Maintain alias for backward-compatibility
    _execute_deploy = _dispatch_deploy

    def _abort_provisioning(self, schedule: dict, do_client: Optional[Any], reason: str):
        """Tear down a cycle that failed while provisioning/configuring: destroy the droplet/instance,
        remove its Hive login, cancel its backstop lease, and feed the circuit breaker / backoff.

        Provider-aware via app.cloud.get_client - this used to only ever call do_client.destroy_droplet(),
        a no-op for a GCP cycle (which never set current_droplet_id under the old Terraform path), so a
        slow/hung GCP provision would leave real resources running, untracked, forever. Fixed now that
        GCP campaigns are on the same create-then-Ansible pipeline as manual GCP deploys, which does set
        current_droplet_id and current_droplet_name as soon as the instance exists.
        """
        from app.hive_manager import HiveManager

        schedule_id = schedule["id"]
        state = schedule.get("state", {})
        config = schedule.get("config", {})
        provider = config.get("provider", "digitalocean")
        droplet_id = state.get("current_droplet_id")
        droplet_name = state.get("current_droplet_name")
        self._log_event(schedule_id, f"Provisioning failed: {reason}", "ERROR")

        if droplet_id:
            try:
                db_delete_active_droplet(droplet_id, db_path=self.db_path)  # stop publishing its IP
            except Exception:
                pass
            self._cancel_backstop_lease(droplet_id)

        if provider == "gcp":
            if droplet_name:
                try:
                    get_client("gcp").destroy_instance(droplet_name, config.get("zone", "us-central1-a"))
                    self._log_event(schedule_id, f"Destroyed failed instance {droplet_name}.")
                except Exception as de:
                    logger.warning(f"Could not destroy failed GCP instance {droplet_name}: {de}")
        elif droplet_id and do_client and do_client.is_configured():
            try:
                do_client.destroy_droplet(droplet_id)
                self._log_event(schedule_id, f"Destroyed failed droplet {droplet_id}.")
            except Exception as de:
                logger.warning(f"Could not destroy failed droplet {droplet_id}: {de}")

        sensor_user = state.get("current_sensor_user")
        if sensor_user and config.get("auto_register_hive", True):
            try:
                HiveManager().deregister_sensor(sensor_user)
            except Exception:
                pass
        self._handle_failure(schedule, reason)

    def _finalize_active(self, schedule: dict, do_client: Optional[Any]):
        """Mark a configured, healthy sensor as active and schedule its teardown."""
        from app.edl_manager import EDLManager

        schedule_id = schedule["id"]
        state = schedule["state"]
        config = schedule.get("config", {})
        timing = schedule.get("timing", {})
        provider = config.get("provider", "digitalocean")
        now = datetime.now(timezone.utc)

        droplet_id = state.get("current_droplet_id")
        pub_ip = state.get("current_public_ip")
        droplet_name = state.get("current_droplet_name") or f"sensor-{droplet_id}"
        self._log_event(schedule_id, f"Sensor {droplet_name} is configured and healthy at fresh dynamic IP {pub_ip}")

        try:
            EDLManager().write_edl_file(do_client=do_client)
            self._log_event(schedule_id, f"Updated Palo Alto EDL with dynamic IP: {pub_ip}")
        except Exception as edl_err:
            logger.error(f"EDL update error: {edl_err}")

        active_sec = timing.get("active_duration_seconds", 86400)
        next_destroy_at = now + timedelta(seconds=active_sec)

        try:
            sensor_type = schedule.get("sensor_type") or config.get("sensor_type", "cowrie")
            # region carries the GCP zone for a GCP row, same convention active_droplets/leases use
            # everywhere else. This used to always default to "digitalocean"/config["region"] regardless
            # of the schedule's actual provider - harmless while GCP campaigns didn't exist for real.
            region = config.get("zone", "us-central1-a") if provider == "gcp" else (state.get("current_region") or config.get("region", "nyc1"))
            size = config.get("machine_type", "e2-small") if provider == "gcp" else config.get("size", "s-1vcpu-2gb")
            db_save_active_droplet(Sensor(
                id=droplet_id,
                name=droplet_name,
                public_ip=pub_ip,
                sensor_type=sensor_type,
                sensor_user=state.get("current_sensor_user"),
                provider=provider,
                region=region,
                size=size,
                image=config.get("image", "ubuntu-24-04-x64"),
                tags=[f"sched-{schedule_id}", "tpot-sensor", f"type-{sensor_type}"],
                status=SensorStatus.ACTIVE.value,
                created_at=now.isoformat(),
                expires_at=next_destroy_at.isoformat(),
            ), db_path=self.db_path)
        except Exception as db_err:
            logger.warning(f"Failed to record active droplet in SQLite: {db_err}")

        # Extend the backstop lease from its short provisioning-phase duration to cover the full active
        # window plus slack - see PROVISIONING_BACKSTOP_MINUTES/CAMPAIGN_BACKSTOP_BUFFER_SECONDS.
        # A campaign paused mid-cycle still finishes that cycle, but its destroy timer won't run until it's
        # resumed - give it the same pause-length ceiling pause_schedule gives an already-active sensor.
        paused = not schedule.get("enabled", True)
        self._schedule_backstop_lease(schedule, PAUSE_BACKSTOP_SECONDS if paused else active_sec + CAMPAIGN_BACKSTOP_BUFFER_SECONDS)

        state["status"] = "active"
        state.pop("stage", None)
        state.pop("hive_token", None)  # no longer needed once configured
        state["cycle_number"] = state.get("cycle_number", 0) + 1
        state["cycle_started_at"] = now.isoformat()
        state["consecutive_failures"] = 0
        state["provisioning_started_at"] = None
        state["provisioning_deadline_at"] = None
        state["next_action"] = "destroy"
        state["next_action_at"] = next_destroy_at.isoformat()
        state["last_action_at"] = now.isoformat()
        state["last_message"] = (
            f"Cycle #{state['cycle_number']} active! Dynamic IP: {pub_ip} "
            f"(Destroy at {next_destroy_at.strftime('%Y-%m-%d %H:%M:%S UTC')})"
        )
        if paused:
            state["last_message"] += " Campaign is paused: the teardown timer runs once it's resumed."

    def _async_do_configure_worker(self, schedule_id: str, ctx: dict):
        """Background thread: configure a freshly created droplet over SSH with Ansible, verify health,
        then flip the campaign to 'active' (or tear the cycle down on failure).

        ctx carries everything needed up front (the tick that spawned us may not have saved state yet):
        droplet_id, ip, name, hive_token, sensor_type, admin_ssh_port.
        """
        from app.do_client import DOClient
        from app.hive_manager import HiveManager
        from app.sensor_types import get_sensor_type

        droplet_id = ctx["droplet_id"]
        error = None
        try:
            hive_mgr = HiveManager()
            warnings = configure_and_verify(
                ctx["ip"], ctx["sensor_type"],
                sensor_name=ctx["name"],
                hive_ip=load_config().get("hive_ip") or hive_mgr.detect_hive_ip(),
                hive_port=64294,
                hive_cert=hive_mgr.get_hive_certificate(),
                hive_token=ctx["hive_token"],
                admin_ssh_port=ctx["admin_ssh_port"],
                swap_size_gb=get_sensor_type(ctx["sensor_type"]).get("swap_size_gb", 2),
                on_log=lambda line: self._log_event(schedule_id, f"[ansible] {line}", "DEBUG"),
                on_step=lambda msg, pct: self._log_event(schedule_id, msg),
            )
            for w in warnings:
                self._log_event(schedule_id, f"WARNING: {w}", "WARN")
        except Exception as e:
            error = str(e) or type(e).__name__
            logger.warning(f"Campaign {schedule_id} configure failed: {error}")

        try:
            token = get_do_token()
            client = DOClient(token=token) if token else None
            deadline = time.monotonic() + 30
            while True:
                with self._lock:
                    schedules = self._load_schedules()
                    s = schedules.get(schedule_id)
                    if not s:
                        # Campaign deleted while configuring: don't leave the droplet running.
                        if client and client.is_configured():
                            try:
                                client.destroy_droplet(droplet_id)
                            except Exception:
                                pass
                        return
                    st = s["state"]
                    ready = (st.get("status") == "provisioning" and st.get("stage") == "configuring"
                             and st.get("current_droplet_id") == droplet_id)
                    if ready:
                        if error:
                            self._abort_provisioning(s, client, error)
                        else:
                            try:
                                self._finalize_active(s, client)
                            except Exception as finalize_err:
                                # Configure and health succeeded, but turning that into "active" state
                                # failed (e.g. a bad timing value overflowing datetime + timedelta). Without
                                # this, the exception would kill the thread here and the cycle would only
                                # get cleaned up a tick later, by the "interrupted configuration" path built
                                # for a worker restart - same outcome, just slower and with an unhandled
                                # traceback in the log instead of a normal abort.
                                logger.warning(f"Campaign {schedule_id} finalize failed: {finalize_err}")
                                self._abort_provisioning(s, client, f"Finalize failed: {finalize_err}")
                        self._save_schedules(schedules)
                        return
                # Not ready: either the spawning tick hasn't saved yet (brief), or the cycle was superseded.
                if time.monotonic() > deadline:
                    logger.warning(f"Campaign {schedule_id}: cycle for droplet {droplet_id} was superseded; discarding result")
                    return
                time.sleep(0.5)
        finally:
            _CONFIGURING.discard(schedule_id)

    def _reconcile_provisioning(self, schedule: dict, do_client: Optional[Any] = None) -> bool:
        """
        Advances an in-flight 'provisioning' cycle. A DigitalOcean cycle goes through two stages:
          waiting_ip   -> poll the DO API until the droplet is ACTIVE with an IP
          configuring  -> a background thread runs Ansible + health checks (cloud-agnostic)
        A GCP cycle skips straight to 'configuring': GCPClient.create_instance() already blocks until
        the instance exists with an IP (there's no DO-style "create non-blocking, poll for it" step for
        GCP), so _dispatch_deploy hands the whole create+configure sequence to one background thread
        (_async_gcp_provision_worker) instead of splitting it across a tick-driven poll.
        Enforces the overall timeout in both stages, for both providers. Returns True if state changed.
        """
        state = schedule.get("state", {})
        if state.get("status") != "provisioning":
            return False

        schedule_id = schedule["id"]
        config = schedule.get("config", {})
        provider = config.get("provider", "digitalocean")
        now = datetime.now(timezone.utc)

        # 1. Enforce Provisioning Timeout Safeguard (both providers)
        deadline_str = state.get("provisioning_deadline_at")
        if deadline_str:
            try:
                if now >= datetime.fromisoformat(deadline_str):
                    self._abort_provisioning(schedule, do_client, f"Provisioning timed out after {PROVISIONING_TIMEOUT_MINUTES} minutes")
                    return True
            except Exception as dt_err:
                logger.warning(f"Failed parsing deadline {deadline_str}: {dt_err}")

        # 2. 'configuring' stage: a background thread (DO's _async_do_configure_worker or GCP's
        # _async_gcp_provision_worker) owns this cycle. Both providers reach this the same way - detect
        # a worker restart mid-configure (the thread that owned it is gone) the same way for both.
        if state.get("stage") == "configuring":
            if schedule_id not in _CONFIGURING:
                self._abort_provisioning(schedule, do_client, "Configuration was interrupted (worker restarted)")
                return True
            return False

        # 3. DigitalOcean-only: waiting_ip -> configuring. GCP never enters this stage (see docstring).
        if provider == "digitalocean":
            droplet_id = state.get("current_droplet_id")
            if not droplet_id or not do_client or not do_client.is_configured():
                return False

            try:
                drop_data = do_client.get_droplet(droplet_id)
                drop_status = drop_data.get("status")
                pub_ip = drop_data.get("public_ip")

                if drop_status == "active" and pub_ip:
                    self._log_event(schedule_id, f"Droplet {droplet_id} is ACTIVE with fresh dynamic IP: {pub_ip}. Configuring over SSH...")
                    state["stage"] = "configuring"
                    state["current_public_ip"] = pub_ip
                    state["current_droplet_name"] = drop_data.get("name") or state.get("current_droplet_name")
                    state["last_message"] = f"Droplet {droplet_id} is up at {pub_ip}; configuring sensor over SSH (Ansible)..."
                    ctx = {
                        "droplet_id": droplet_id,
                        "ip": pub_ip,
                        "name": state["current_droplet_name"] or f"sensor-{droplet_id}",
                        "hive_token": state.get("hive_token", ""),
                        "sensor_type": config.get("sensor_type", "cowrie"),
                        "admin_ssh_port": config.get("admin_ssh_port", 64295),
                    }
                    # Record and publish the IP now (not after health): the firewall needs the feed to list this
                    # sensor before it can admit it to the Hive, and configuring takes minutes.
                    try:
                        sensor_type = config.get("sensor_type", "cowrie")
                        db_save_active_droplet(Sensor(
                            id=droplet_id, name=state["current_droplet_name"] or f"sensor-{droplet_id}", public_ip=pub_ip, sensor_type=sensor_type,
                            sensor_user=state.get("current_sensor_user"), region=state.get("current_region") or config.get("region", "nyc1"),
                            size=config.get("size", "s-1vcpu-2gb"), image=config.get("image", "ubuntu-24-04-x64"),
                            tags=[f"sched-{schedule_id}", "tpot-sensor", f"type-{sensor_type}"],
                            status=SensorStatus.PROVISIONING.value, created_at=now.isoformat(),
                        ), db_path=self.db_path)
                        from app.edl_manager import EDLManager
                        EDLManager().invalidate_cache()
                    except Exception as db_err:
                        logger.warning(f"Could not record provisioning sensor {droplet_id}: {db_err}")
                    # Independent backstop: covers "worker dies mid-Ansible-run", when nothing else would
                    # ever destroy this droplet. See PROVISIONING_BACKSTOP_MINUTES.
                    self._schedule_backstop_lease(schedule, PROVISIONING_BACKSTOP_MINUTES * 60)
                    _CONFIGURING.add(schedule_id)
                    threading.Thread(target=self._async_do_configure_worker, args=(schedule_id, ctx), daemon=True).start()
                    return True

                if drop_status in ["errored", "failed"]:
                    self._abort_provisioning(schedule, do_client, f"Droplet failed with status {drop_status}")
                    return True

            except Exception as e:
                logger.warning(f"Error querying droplet {droplet_id} status during reconciliation: {e}")

        return False

    def _execute_destroy(self, schedule: dict) -> Tuple[bool, str]:
        """
        Executes a teardown cycle for an active scheduled honeypot: destroys the droplet/instance,
        cancels its backstop lease, releases the IP, cleans credentials, and calculates next rebuild.
        Both providers fall through to the same tail (Hive deregister, EDL update, next-rebuild calc,
        history, state reset) - GCP used to spawn its own thread with a second, duplicated copy of all of
        that (the old Terraform-based _async_gcp_destroy_worker); not anymore, since a GCP delete is a
        single ~10-20s API call, not a multi-minute `terraform destroy` that needed to be backgrounded.
        """
        from app.do_client import DOClient
        from app.hive_manager import HiveManager
        from app.edl_manager import EDLManager

        schedule_id = schedule["id"]
        timing = schedule["timing"]
        state = schedule["state"]
        config = schedule["config"]

        droplet_id = state.get("current_droplet_id")
        droplet_name = state.get("current_droplet_name")
        sensor_user = state.get("current_sensor_user")
        old_ip = state.get("current_public_ip")

        provider = config.get("provider", "digitalocean")
        token = get_do_token()
        client = DOClient(token=token) if token else None
        hive_mgr = HiveManager()
        edl_mgr = EDLManager()

        self._log_event(schedule_id, f"Teardown initiated for {provider.upper()} sensor {droplet_name}...")

        if droplet_id:
            try:
                db_delete_active_droplet(droplet_id, db_path=self.db_path)
            except Exception:
                pass
            self._cancel_backstop_lease(droplet_id)

        if provider == "gcp":
            if droplet_name:
                try:
                    get_client("gcp").destroy_instance(droplet_name, config.get("zone", "us-central1-a"))
                    self._log_event(schedule_id, f"Destroyed instance {droplet_name}. Dynamic IP {old_ip} released.")
                except Exception as e:
                    self._log_event(schedule_id, f"Instance destroy note: {e}", "WARN")
        elif droplet_id:
            if client and client.is_configured():
                try:
                    client.destroy_droplet(droplet_id)
                    self._log_event(schedule_id, f"Destroyed droplet {droplet_id}. Dynamic IP {old_ip} released.")
                except Exception as e:
                    self._log_event(schedule_id, f"Droplet destroy note: {e}", "WARN")

        # Clean Hive credentials
        if sensor_user and config.get("auto_register_hive", True):
            try:
                hive_mgr.deregister_sensor(sensor_user)
                self._log_event(schedule_id, f"Deregistered Hive user '{sensor_user}'.")
            except Exception:
                pass

        # Update Palo Alto EDL
        try:
            edl_mgr.write_edl_file(do_client=client)
            self._log_event(schedule_id, "Updated Palo Alto EDL (removed expired sensor IP).")
        except Exception:
            pass

        # Calculate Next Rebuild Time
        now = datetime.now(timezone.utc)
        cycle_start_dt = None
        if state.get("cycle_started_at"):
            try:
                cycle_start_dt = datetime.fromisoformat(state["cycle_started_at"])
            except Exception:
                pass

        next_rebuild_at = self._calculate_next_rebuild(timing, cycle_start_dt)

        # Record History
        history_item = {
            "cycle": state.get("cycle_number", 1),
            "droplet_name": droplet_name,
            "ip": old_ip,
            "deployed_at": state.get("cycle_started_at"),
            "destroyed_at": now.isoformat(),
            "status": "completed"
        }
        history = state.get("history", [])
        history.insert(0, history_item)
        state["history"] = history[:20]  # keep last 20 entries

        # Update State
        state["status"] = "cooling_down"
        state["current_droplet_id"] = None
        state["current_droplet_name"] = None
        state["current_public_ip"] = None
        state["current_sensor_user"] = None
        state["next_action"] = "deploy"
        state["next_action_at"] = next_rebuild_at.isoformat()
        state["last_action_at"] = now.isoformat()
        state["last_message"] = f"Teardown complete. Cooling down until next rebuild at {next_rebuild_at.strftime('%Y-%m-%d %H:%M:%S UTC')}."

        self._log_event(schedule_id, f"Entered cooldown state. Next fresh rebuild scheduled for {next_rebuild_at.isoformat()}")
        return True, "Teardown complete."

    def reconcile_tick(self) -> bool:
        """
        Fast non-blocking two-phase reconciliation tick.
        Phase 1: Reconcile in-flight provisioning honeypots (polling status & enforcing timeouts).
        Phase 2: Evaluate timers, enforce concurrency caps, and dispatch deploys/teardowns.
        Returns True if any schedules were modified.
        """
        from app.do_client import DOClient

        token = get_do_token()
        client = DOClient(token=token) if token else None

        with self._lock:
            schedules = self._load_schedules()

        now = datetime.now(timezone.utc)
        touched: set = set()  # only these are written back, so we never clobber a background thread's newer state

        # Phase 1: Reconcile in-flight provisioning honeypots
        for sid, s in list(schedules.items()):
            state = s.get("state", {})
            if state.get("status") == "provisioning":
                if self._reconcile_provisioning(s, do_client=client):
                    touched.add(sid)

        # Phase 2: Timer evaluation with concurrency capping
        in_flight_count = sum(
            1 for s in schedules.values()
            if s.get("state", {}).get("status") == "provisioning"
        )

        for sid, s in list(schedules.items()):
            if not s.get("enabled", True):
                continue

            state = s.get("state", {})
            status = state.get("status")

            # Skip if an operation is already in-flight
            if status in ["provisioning", "tearing_down"]:
                continue

            next_action = state.get("next_action")
            next_action_at_str = state.get("next_action_at")

            if not next_action_at_str or not next_action:
                continue

            try:
                next_action_at = datetime.fromisoformat(next_action_at_str)
            except Exception:
                continue

            if now >= next_action_at:
                if next_action == "destroy":
                    self._log_event(sid, "Timer triggered for action: DESTROY")
                    self._execute_destroy(s)
                    touched.add(sid)
                elif next_action == "deploy":
                    # Concurrency Cap Safeguard
                    if in_flight_count >= MAX_CONCURRENT_PROVISIONING:
                        self._log_event(
                            sid,
                            f"Concurrency cap reached ({in_flight_count}/{MAX_CONCURRENT_PROVISIONING} provisioning). Deferring deploy by 15s.",
                            "WARN"
                        )
                        state["last_message"] = f"Concurrency cap reached ({in_flight_count}/{MAX_CONCURRENT_PROVISIONING} in-flight). Waiting for available slot."
                        state["next_action_at"] = (now + timedelta(seconds=15)).isoformat()
                        touched.add(sid)
                    else:
                        self._log_event(sid, "Timer triggered for action: DEPLOY")
                        ok, msg = self._dispatch_deploy(s)
                        if ok:
                            in_flight_count += 1
                        touched.add(sid)

        if touched:
            with self._lock:
                curr = self._load_schedules()
                for sid in touched:
                    if sid in curr:  # deleted mid-tick: don't resurrect it
                        curr[sid] = schedules[sid]
                self._save_schedules(curr)

        return bool(touched)

