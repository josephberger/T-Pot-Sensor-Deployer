import os
import re
import ipaddress
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from app.config import CONFIG_DIR, DB_PATH
from app.do_client import DOClient
from app.db import db_get_static_ips, db_add_static_ip, db_remove_static_ip, db_get_all_active_droplets

EDL_FILE_PATH = CONFIG_DIR / "sensors.txt"
EDL_PUBLISHED_STATUSES = ("active", "provisioning")  # not provision_failed

_IPV4_RE = re.compile(r"^([0-9]{1,3}\.){3}[0-9]{1,3}(/[0-9]{1,2})?$")


def is_valid_ip_or_cidr(val: str) -> bool:
    try:
        ipaddress.ip_network(val.strip(), strict=False)
        return True
    except ValueError:
        return False


class EDLManager:
    """
    Manages the External Dynamic List (EDL) for Palo Alto Networks firewalls.
    Palo Alto EDL format:
    - Plaintext (text/plain)
    - One IP address or CIDR subnet per line
    - Lines starting with '#' are treated as comments and ignored by PAN-OS
    """

    def __init__(self, edl_path: Path = EDL_FILE_PATH, db_path: Path = DB_PATH):
        self.edl_path = edl_path
        self.db_path = db_path
        self._cached_entries: Optional[List[Dict[str, str]]] = None
        self._cached_at: float = 0.0

    def invalidate_cache(self):
        """Invalidate in-memory EDL entries cache."""
        self._cached_entries = None
        self._cached_at = 0.0

    def _load_static_ips(self) -> List[Dict[str, str]]:
        return db_get_static_ips(self.db_path)

    def _save_static_ips(self, entries: List[Dict[str, str]]):
        for e in entries:
            db_add_static_ip(e["ip"], e.get("comment", ""), e.get("added_at"), self.db_path)

    def add_static_ip(self, ip: str, comment: str = "") -> Tuple[bool, str]:
        clean_ip = ip.strip()
        if not is_valid_ip_or_cidr(clean_ip):
            return False, f"Invalid IPv4 address or CIDR: {ip}"

        entries = self._load_static_ips()
        for e in entries:
            if e.get("ip") == clean_ip:
                return False, f"IP {clean_ip} already exists in static list"

        db_add_static_ip(clean_ip, comment.strip(), db_path=self.db_path)
        self.invalidate_cache()
        # Refresh physical file
        self.write_edl_file()
        return True, f"Added {clean_ip} to static EDL"

    def update_static_ip(self, old_ip: str, ip: str, comment: str = "") -> Tuple[bool, str]:
        """Change the address and/or label of a static entry, keeping its added_at."""
        old = old_ip.strip()
        new = ip.strip()
        if not is_valid_ip_or_cidr(new):
            return False, f"Invalid IPv4 address or CIDR: {ip}"
        entries = {e["ip"]: e for e in self._load_static_ips()}
        if old not in entries:
            return False, f"IP {old} not found in static list"
        if new != old and new in entries:
            return False, f"IP {new} already exists in static list"

        if new != old:
            db_remove_static_ip(old, db_path=self.db_path)
        db_add_static_ip(new, comment.strip(), entries[old].get("added_at"), self.db_path)
        self.invalidate_cache()
        self.write_edl_file()
        return True, f"Updated {new}"

    def remove_static_ip(self, ip: str) -> Tuple[bool, str]:
        clean_ip = ip.strip()
        ok = db_remove_static_ip(clean_ip, db_path=self.db_path)
        if not ok:
            return False, f"IP {clean_ip} not found in static list"

        self.invalidate_cache()
        self.write_edl_file()
        return True, f"Removed {clean_ip} from static EDL"

    def get_all_entries(self, do_client: Optional[DOClient] = None, force_refresh: bool = False) -> List[Dict[str, str]]:
        """
        Collects all active sensor IPs with short 10s caching:
        1. Dynamic IPs queried from DigitalOcean droplets tagged with tpot-cowrie-sensor
        2. User-defined static sensor IPs
        """
        import time
        now = time.time()
        if not force_refresh and self._cached_entries is not None and (now - self._cached_at < 10.0):
            return list(self._cached_entries)

        results = []
        seen_ips = set()

        # 1. Fetch dynamic sensor IPs from SQLite active_droplets
        try:
            db_drops = db_get_all_active_droplets(self.db_path)
            for d in db_drops:
                pub_ip = d.get("public_ip")
                status = d.get("status", "active")
                # "provisioning" is published too: a firewall can only admit a sensor once the feed lists it,
                # and admitting it is what lets the sensor reach the Hive while it is still being configured.
                if pub_ip and status in EDL_PUBLISHED_STATUSES and pub_ip not in seen_ips:
                    seen_ips.add(pub_ip)
                    stype = d.get("sensor_type", "cowrie")
                    tags = d.get("tags", [])
                    provider_label = "GCP" if d.get("provider") == "gcp" else "DO"
                    is_scheduled = any(t.startswith("sched-") for t in tags)
                    src_label = f"{provider_label} Scheduled ({stype.upper()})" if is_scheduled else f"{provider_label} Sensor ({stype.upper()})"
                    results.append({
                        "ip": pub_ip,
                        "source": src_label,
                        "sensor_type": stype,
                        "name": d.get("name"),
                        "region": d.get("region", ""),
                        "status": status
                    })
        except Exception:
            pass

        # If DO client is explicitly provided, fetch DO droplets directly
        if do_client and do_client.is_configured():
            try:
                try:
                    droplets = do_client.list_droplets(force_refresh=True)
                except TypeError:
                    droplets = do_client.list_droplets()
                for d in droplets:
                    tags = d.get("tags", [])
                    is_tpot = (
                        any(t.startswith("tpot-") and t.endswith("-sensor") for t in tags) or
                        "tpot-sensor" in tags or
                        any(t.startswith("type-") for t in tags) or
                        "sensor" in d.get("name", "").lower()
                    )
                    if is_tpot:
                        pub_ip = d.get("public_ip")
                        status = d.get("status")
                        if pub_ip and status == "active" and pub_ip not in seen_ips:
                            seen_ips.add(pub_ip)
                            stype = "sensor"
                            for t in tags:
                                if t.startswith("type-"):
                                    stype = t.replace("type-", "")
                                    break
                                elif t.startswith("tpot-") and t.endswith("-sensor") and t != "tpot-sensor":
                                    stype = t.replace("tpot-", "").replace("-sensor", "")
                                    break

                            is_scheduled = any(t.startswith("sched-") for t in tags)
                            src_label = f"DO Scheduled ({stype.upper()})" if is_scheduled else f"DO Sensor ({stype.upper()})"
                            results.append({
                                "ip": pub_ip,
                                "source": src_label,
                                "sensor_type": stype,
                                "name": d.get("name"),
                                "region": d.get("region", {}).get("slug", "") if isinstance(d.get("region"), dict) else str(d.get("region", "")),
                                "status": status
                            })
            except Exception:
                pass

        # 2. Add static IPs
        for s in self._load_static_ips():
            sip = s.get("ip")
            if sip and sip not in seen_ips:
                seen_ips.add(sip)
                results.append({
                    "ip": sip,
                    "source": "Static Configuration",
                    "name": s.get("comment") or "Manual Entry",
                    "region": "custom",
                    "status": "active"
                })

        self._cached_entries = results
        self._cached_at = now
        return results

    def generate_edl_plaintext(
        self,
        do_client: Optional[DOClient] = None,
        include_comments: bool = True,
        entries: Optional[List[Dict[str, str]]] = None
    ) -> str:
        """
        Generate strict PAN-OS compliant plaintext.
        PAN-OS parses each line; lines starting with '#' are ignored.
        """
        if entries is None:
            entries = self.get_all_entries(do_client)
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        lines = []
        if include_comments:
            lines.append("# =============================================================")
            lines.append("# Palo Alto Networks - External Dynamic List (EDL)")
            lines.append("# Type: IP List")
            lines.append(f"# Generated: {now_str}")
            lines.append(f"# Total Sensor IPs: {len(entries)}")
            lines.append("# =============================================================")

        for e in entries:
            ip = e["ip"]
            if include_comments and e.get("name"):
                lines.append(f"{ip}  # {e.get('source', '')} - {e.get('name', '')}")
            else:
                lines.append(ip)

        # PAN-OS requires trailing newline
        return "\n".join(lines) + "\n"

    def write_edl_file(
        self,
        do_client: Optional[DOClient] = None,
        entries: Optional[List[Dict[str, str]]] = None
    ) -> Path:
        """Writes the plaintext list to the physical file on disk only if changed."""
        content = self.generate_edl_plaintext(do_client, include_comments=True, entries=entries)
        self.edl_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if not self.edl_path.exists() or self.edl_path.read_text(encoding="utf-8") != content:
                self.edl_path.write_text(content, encoding="utf-8")
        except Exception:
            self.edl_path.write_text(content, encoding="utf-8")
        return self.edl_path
