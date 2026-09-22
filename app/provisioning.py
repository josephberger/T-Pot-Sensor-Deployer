"""
Cloud-agnostic "configure and verify" step shared by manual deploys (jobs.py) and campaigns
(scheduler_manager.py). Once a cloud backend has produced a reachable machine, everything else
is plain SSH + Ansible + standard checks; nothing here knows which cloud it is.
"""

import os
import time
from typing import Callable, List, Optional

from app.ansible_runner import LogFn, playbook_for, run_playbook, wait_for_port, wait_for_ssh_auth
from app.health import ADMIN_SSH_PORT, deep_check

StepFn = Callable[[str, int], None]

HEALTH_ATTEMPTS = 3
HEALTH_RETRY_SECONDS = 30
# A firewall that gates the Hive (e.g. a Palo Alto refreshing an EDL every ~5 minutes) admits a sensor some
# minutes after its IP is published. Wait up to this long for that before reporting "cannot reach the Hive".
HIVE_ADMIT_WAIT_SECONDS = float(os.environ.get("HIVE_ADMIT_WAIT_SECONDS", "480"))
HIVE_ADMIT_POLL_SECONDS = 45


def pick_ssh_port(ip: str, admin_ssh_port: int, wait_seconds: float = 300.0, on_wait=None) -> int:
    """
    Which port to configure over. A machine we already configured has admin SSH on admin_ssh_port
    (port 22 may now be the honeypot), so try that first; a fresh machine only has SSH on 22.
    """
    if wait_for_port(ip, admin_ssh_port, timeout=4, interval=1):
        return admin_ssh_port
    if on_wait:
        on_wait(f"🔌 Waiting for SSH on {ip}:22...")
    if not wait_for_port(ip, 22, timeout=wait_seconds):
        raise RuntimeError(f"SSH never became reachable on {ip}:22")
    return 22


def _wait_for_hive_admission(ip, sensor_type, health, hive_ip, hive_port, ssh_port, on_log, step, remote_ssh_user="root") -> List[str]:
    """Healthy sensor that cannot reach the Hive yet: give the firewall time to admit it, then report."""
    warnings = health.get("warnings", [])
    deadline = time.monotonic() + HIVE_ADMIT_WAIT_SECONDS
    while warnings and time.monotonic() < deadline:
        step("⏳ Sensor is healthy; waiting for the firewall to admit it to the Hive (it refreshes about every 5 minutes)...", 92)
        time.sleep(HIVE_ADMIT_POLL_SECONDS)
        health = deep_check(
            ip, sensor_type, hive_ip=hive_ip, hive_port=hive_port, ssh_port=ssh_port, on_log=on_log,
            remote_ssh_user=remote_ssh_user,
        )
        if not health["healthy"]:
            break  # something else broke while waiting; the next full check will report it
        warnings = health.get("warnings", [])
    return warnings


def configure_and_verify(
    ip: str,
    sensor_type: str,
    *,
    sensor_name: str,
    hive_ip: str,
    hive_port: int,
    hive_cert: str,
    hive_token: str,
    admin_ssh_port: int = ADMIN_SSH_PORT,
    swap_size_gb: int = 2,
    on_log: Optional[LogFn] = None,
    on_step: Optional[StepFn] = None,
    ssh_wait_seconds: float = 300.0,
    remote_ssh_user: str = "root",
) -> List[str]:
    """
    Wait for SSH, run the sensor playbook, then run the health playbook. Raises RuntimeError on any failure.
    Returns non-fatal warnings (e.g. the sensor cannot reach the Hive yet).

    remote_ssh_user: the account Ansible logs in as. DO images allow root SSH directly (default). A GCP
    image doesn't - its guest agent provisions a named, non-root sudo user instead - so the GCP deploy
    path passes that username here; every playbook already runs with `become: true`, so this is the only
    change needed to support it.
    """
    def step(msg: str, pct: int):
        if on_step:
            on_step(msg, pct)

    playbook = playbook_for("sensors", sensor_type)
    if not playbook:
        raise RuntimeError(f"No Ansible playbook for sensor type '{sensor_type}'")

    ssh_port = pick_ssh_port(ip, admin_ssh_port, ssh_wait_seconds, on_wait=lambda m: step(m, 70))

    # The port being open doesn't mean the login user's key is installed yet (true immediately for DO,
    # a guest-agent-provisioning race for GCP - see wait_for_ssh_auth's docstring).
    if not wait_for_ssh_auth(ip, ssh_port, remote_ssh_user, on_wait=lambda m: step(m, 72)):
        raise RuntimeError(f"SSH auth as {remote_ssh_user}@{ip}:{ssh_port} never succeeded")

    step(f"📦 Running playbook {playbook.name} on {ip}...", 75)
    rc, _ = run_playbook(
        playbook, ip,
        {
            "hive_ip": hive_ip,
            "hive_port": hive_port,
            "hive_cert_content": hive_cert or "",
            "ssl_verification": "full" if hive_cert else "none",
            "tpot_hive_user": hive_token,
            "sensor_name": sensor_name,
            "swap_size_gb": swap_size_gb,
            "admin_ssh_port": admin_ssh_port,
        },
        port=ssh_port,
        user=remote_ssh_user,
        on_log=on_log,
    )
    if rc != 0:
        raise RuntimeError(f"Playbook {playbook.name} failed (exit {rc})")

    step("🩺 Running health checks...", 88)
    health = {"healthy": False, "output": []}
    for attempt in range(1, HEALTH_ATTEMPTS + 1):  # containers need a moment to pull and become healthy
        health = deep_check(
            ip, sensor_type, hive_ip=hive_ip, hive_port=hive_port, ssh_port=admin_ssh_port,
            on_log=on_log, remote_ssh_user=remote_ssh_user,
        )
        if health["healthy"]:
            return _wait_for_hive_admission(
                ip, sensor_type, health, hive_ip, hive_port, admin_ssh_port, on_log, step, remote_ssh_user
            )
        if attempt < HEALTH_ATTEMPTS:
            step(f"⏳ Health check {attempt}/{HEALTH_ATTEMPTS} not yet passing; retrying in {HEALTH_RETRY_SECONDS}s...", 88)
            time.sleep(HEALTH_RETRY_SECONDS)
    tail = " | ".join(health.get("output", [])[-5:]) or health.get("error", "no output")
    raise RuntimeError(f"Failed health checks: {tail}")
