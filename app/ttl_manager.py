import json
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any

from app.config import LOGS_DIR, DB_PATH
from app.db import (
    db_get_all_leases,
    db_get_lease,
    db_save_lease,
    db_delete_lease,
    db_record_sensor_launch,
    db_record_sensor_destroy
)

_DURATION_RE = re.compile(r"^(\d+)([smhd])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
# A generous ceiling (10 years), not a real limit on anything legitimate: it exists only so a typo like
# "99999999d" is rejected here instead of reaching datetime + timedelta(seconds=...) later (a campaign's
# active window, a TTL lease, a cooldown), which raises OverflowError past ~year 9999. Found live: a
# schedule with such a duration only fails once it actually goes active - after a real droplet was already
# created and configured - and while the campaign's own "interrupted configuration" self-heal (built for a
# worker restart) happens to clean it up, that is a wasted deploy and an unhandled traceback to get there.
_MAX_DURATION_SECONDS = 10 * 365 * 86400


def parse_duration(text: str) -> int:
    """Parse '90m', '2h', '1d', '30s' into number of seconds."""
    if not text:
        return 0
    match = _DURATION_RE.match(text.strip().lower())
    if not match:
        raise ValueError(f"Invalid duration '{text}'. Use a number followed by s, m, h, or d (e.g. 2h, 1d).")
    value, unit = match.groups()
    seconds = int(value) * _UNIT_SECONDS[unit]
    if seconds > _MAX_DURATION_SECONDS:
        raise ValueError(f"Duration '{text}' is too long (max 10 years).")
    return seconds


def format_remaining(seconds: float) -> str:
    """Format remaining seconds into human-readable e.g. '1h 24m' or 'Expired'."""
    if seconds <= 0:
        return "Expired"
    secs = int(seconds)
    days = secs // 86400
    hours = (secs % 86400) // 3600
    minutes = (secs % 3600) // 60
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


class TTLManager:
    """Manages TTL auto-destroy leases for droplets backed by SQLite."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._lock = threading.RLock()

    def _load_leases(self) -> dict:
        return db_get_all_leases(self.db_path)

    def _save_leases(self, leases: dict):
        with self._lock:
            current = db_get_all_leases(self.db_path)
            for sid in list(current.keys()):
                if sid not in leases:
                    db_delete_lease(int(sid), self.db_path)
            for sid, entry in leases.items():
                db_save_lease(entry, self.db_path)

    def schedule_lease(
        self,
        droplet_id: int,
        droplet_name: str,
        ttl_text: str,
        token: Optional[str] = None,
        sensor_user: Optional[str] = None,
        sensor_type: str = "cowrie",
        provider: str = "digitalocean",
        region: Optional[str] = None,
    ) -> dict:
        ttl_seconds = parse_duration(ttl_text)
        if ttl_seconds <= 0:
            return {}

        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()

        lease_entry = {
            "droplet_id": droplet_id,
            "droplet_name": droplet_name,
            "sensor_user": sensor_user or droplet_name,
            "ttl": ttl_text,
            "ttl_seconds": ttl_seconds,
            "created_at": now.isoformat(),
            "expires_at": expires_at,
            "provider": provider,
            # The GCP zone (unused for DO): destroy_instance needs it and sweep_expired_leases has no
            # other way to get it without an extra DB lookup per expired lease.
            "region": region,
        }

        with self._lock:
            db_save_lease(lease_entry, self.db_path)
            db_record_sensor_launch(
                droplet_id=droplet_id,
                name=droplet_name,
                sensor_type=sensor_type,
                created_at=now.isoformat(),
                db_path=self.db_path
            )

        return lease_entry

    def cancel_lease(self, droplet_id: int) -> bool:
        with self._lock:
            db_record_sensor_destroy(droplet_id=droplet_id, reason="Cancelled by User", db_path=self.db_path)
            return db_delete_lease(droplet_id, self.db_path)

    def extend_lease(self, droplet_id: int, extra_ttl_text: str) -> Optional[dict]:
        extra_seconds = parse_duration(extra_ttl_text)
        with self._lock:
            leases = self._load_leases()
            sid = str(droplet_id)
            if sid not in leases:
                return None

            entry = leases[sid]
            try:
                curr_expiry = datetime.fromisoformat(entry["expires_at"])
            except Exception:
                curr_expiry = datetime.now(timezone.utc)

            new_expiry = curr_expiry + timedelta(seconds=extra_seconds)
            entry["expires_at"] = new_expiry.isoformat()
            leases[sid] = entry
            self._save_leases(leases)
            return entry

    def get_lease(self, droplet_id: int) -> Optional[dict]:
        leases = self._load_leases()
        entry = leases.get(str(droplet_id))
        if not entry:
            return None

        now = datetime.now(timezone.utc)
        try:
            exp = datetime.fromisoformat(entry["expires_at"])
            remaining = (exp - now).total_seconds()
        except Exception:
            remaining = 0

        info = dict(entry)
        info["remaining_seconds"] = max(0, remaining)
        info["remaining_formatted"] = format_remaining(remaining)
        info["is_expired"] = remaining <= 0
        return info

    def get_all_leases(self) -> Dict[str, dict]:
        leases = self._load_leases()
        result = {}
        now = datetime.now(timezone.utc)
        for sid, entry in leases.items():
            try:
                exp = datetime.fromisoformat(entry["expires_at"])
                remaining = (exp - now).total_seconds()
            except Exception:
                remaining = 0
            info = dict(entry)
            info["remaining_seconds"] = max(0, remaining)
            info["remaining_formatted"] = format_remaining(remaining)
            info["is_expired"] = remaining <= 0
            result[sid] = info
        return result

    def sweep_expired_leases(self) -> List[str]:
        """Perform a single sweep of all active leases and auto-destroy any that have expired."""
        from app.cloud import get_client
        from app.do_client import DOClient
        from app.hive_manager import HiveManager
        from app.edl_manager import EDLManager

        hive_mgr = HiveManager()
        edl_mgr = EDLManager()

        leases = self._load_leases()
        now = datetime.now(timezone.utc)
        to_delete = []

        # One client per provider actually needed, built lazily (a GCP client shouldn't be constructed -
        # and doesn't need to be configured - when nothing here is a GCP lease, and vice versa).
        clients: Dict[str, Any] = {}

        def client_for(provider: str):
            if provider not in clients:
                try:
                    c = get_client(provider)
                    clients[provider] = c if c.is_configured() else None
                except Exception:
                    clients[provider] = None
            return clients[provider]

        do_client_for_edl: Optional[DOClient] = None

        for sid, entry in list(leases.items()):
            try:
                exp = datetime.fromisoformat(entry["expires_at"])
                if now >= exp:
                    droplet_id = int(sid)
                    droplet_name = entry.get("droplet_name", str(droplet_id))
                    sensor_user = entry.get("sensor_user") or droplet_name
                    provider = entry.get("provider", "digitalocean")
                    zone = entry.get("region")

                    client = client_for(provider)
                    if provider == "digitalocean":
                        do_client_for_edl = client
                    if client:
                        try:
                            if provider == "gcp":
                                client.destroy_instance(droplet_name, zone)
                            else:
                                client.destroy_droplet(droplet_id)
                            log_path = LOGS_DIR / "auto_destroy.log"
                            with open(log_path, "a", encoding="utf-8") as lf:
                                lf.write(f"[{datetime.now().isoformat()}] Auto-destroyed {provider} sensor {droplet_name} ({droplet_id}) due to TTL expiration.\n")
                        except Exception:
                            pass

                    # Deregister sensor credentials from Hive
                    if sensor_user:
                        try:
                            hive_mgr.deregister_sensor(sensor_user)
                        except Exception:
                            pass

                    # Update Palo Alto EDL feed
                    try:
                        edl_mgr.write_edl_file(do_client=do_client_for_edl)
                    except Exception:
                        pass

                    to_delete.append(sid)
            except Exception:
                pass

        if to_delete:
            with self._lock:
                for sid in to_delete:
                    try:
                        db_record_sensor_destroy(droplet_id=int(sid), reason="TTL Expired", db_path=self.db_path)
                    except Exception:
                        pass
                    db_delete_lease(int(sid), self.db_path)

        return to_delete
