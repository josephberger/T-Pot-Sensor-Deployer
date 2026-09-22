"""
Sensor health checks, independent of any cloud provider.

quick_check: ping + TCP probes from this host (cheap, no SSH login).
deep_check:  runs the per-sensor-type health playbook over SSH.
"""

import shutil
import socket
import subprocess
from typing import Dict, List, Optional

from app.ansible_runner import LogFn, playbook_for, run_playbook
from app.sensor_types import get_sensor_ports

ADMIN_SSH_PORT = 64295


def _ping(ip: str) -> bool:
    if not shutil.which("ping"):
        return False
    res = subprocess.run(["ping", "-c", "1", "-W", "3", ip], capture_output=True)
    return res.returncode == 0


def _tcp(ip: str, port: int, timeout: float = 4.0) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def quick_check(ip: str, sensor_type: str) -> Dict:
    """Ping, admin SSH and honeypot TCP ports. Healthy = SSH up and every TCP honeypot port open."""
    ports = [p["port"] for p in get_sensor_ports(sensor_type) if p.get("proto") == "tcp"]
    ping_ok = _ping(ip)
    ssh_ok = _tcp(ip, ADMIN_SSH_PORT)
    port_results = {str(p): _tcp(ip, p) for p in ports}
    return {
        "ip": ip,
        "ping": ping_ok,  # informational: ICMP is often filtered or unavailable in containers
        "admin_ssh": ssh_ok,
        "honeypot_ports": port_results,
        "healthy": ssh_ok and all(port_results.values()),
    }


def deep_check(ip: str, sensor_type: str, hive_ip: str = "", hive_port: int = 64294,
               ssh_port: int = ADMIN_SSH_PORT, on_log: Optional[LogFn] = None,
               remote_ssh_user: str = "root") -> Dict:
    """Run playbooks/health/<sensor_type>.yml against the sensor."""
    pb = playbook_for("health", sensor_type)
    if not pb:
        return {"ip": ip, "healthy": False, "error": f"no health playbook for '{sensor_type}'", "output": []}
    rc, lines = run_playbook(
        pb, ip, {"hive_ip": hive_ip, "hive_port": hive_port},
        port=ssh_port, user=remote_ssh_user, on_log=on_log, timeout=300,
    )
    warnings = [l.split("TPOT_WARNING:", 1)[1].strip(' ",') for l in lines if "TPOT_WARNING:" in l]
    return {"ip": ip, "healthy": rc == 0, "returncode": rc, "output": lines[-40:], "warnings": warnings}
