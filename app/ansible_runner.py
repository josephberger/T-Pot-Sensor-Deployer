"""
Cloud-agnostic sensor configuration via Ansible.

The cloud client only creates/destroys machines. Everything on the machine (Docker, the
T-Pot sensor stack, health checks) is done by playbooks in playbooks/ over plain SSH, so any
provider that can hand back an IP works the same way.

Playbook layout:
    playbooks/sensors/<sensor_type>.yml   deploy a sensor
    playbooks/health/<sensor_type>.yml    deep health check
Adding a sensor type = adding those two small files (see cowrie.yml).
"""

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from app.config import PROJECT_ROOT, SECRETS_DIR

PLAYBOOKS_DIR = PROJECT_ROOT / "playbooks"
DEFAULT_PRIVATE_KEY = SECRETS_DIR / "ssh_key"

LogFn = Callable[[str], None]


def private_key_path() -> Path:
    return Path(os.environ.get("SSH_PRIVATE_KEY_PATH") or DEFAULT_PRIVATE_KEY)


def ansible_available() -> bool:
    return shutil.which("ansible-playbook") is not None


def playbook_for(kind: str, sensor_type: str) -> Optional[Path]:
    """Return the playbook for kind ('sensors' | 'health') and sensor type, or None."""
    p = PLAYBOOKS_DIR / kind / f"{sensor_type}.yml"
    return p if p.exists() else None


def supported_sensor_types() -> List[str]:
    """Sensor types that have both a deploy and a health playbook."""
    deploy = {p.stem for p in (PLAYBOOKS_DIR / "sensors").glob("*.yml")}
    health = {p.stem for p in (PLAYBOOKS_DIR / "health").glob("*.yml")}
    return sorted(deploy & health)


def wait_for_port(host: str, port: int, timeout: float = 300.0, interval: float = 5.0,
                  on_wait: Optional[LogFn] = None) -> bool:
    """Block until host:port accepts TCP connections (e.g. SSH on a fresh machine)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=5):
                return True
        except OSError:
            if on_wait:
                on_wait(f"waiting for {host}:{port} ...")
            time.sleep(interval)
    return False


def wait_for_ssh_auth(host: str, port: int, user: str, timeout: float = 90.0, interval: float = 5.0,
                       on_wait: Optional[LogFn] = None) -> bool:
    """
    Block until an SSH login as `user` actually succeeds, not just until the port is open.

    A DO droplet has the deployer's key baked in by the cloud API itself, so auth succeeds the instant
    the port is open. A GCP instance's key is installed by the guest agent *after* boot from instance
    metadata, a few seconds after sshd starts accepting TCP connections on the same port - a plain
    wait_for_port() there races the guest agent and can hand a fresh machine to ansible-playbook before
    its key exists, failing with "Permission denied (publickey)" and no retry. This is a cheap, generic
    fix (a no-op wait for DO, where the first attempt already succeeds).
    """
    key = private_key_path()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        res = subprocess.run(
            [
                "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=5", "-i", str(key), "-p", str(port),
                f"{user}@{host}", "true",
            ],
            capture_output=True, timeout=10,
        )
        if res.returncode == 0:
            return True
        if on_wait:
            on_wait(f"waiting for SSH auth as {user}@{host}:{port} ...")
        time.sleep(interval)
    return False


def run_playbook(
    playbook: Path,
    host: str,
    extra_vars: Optional[Dict] = None,
    port: int = 22,
    user: str = "root",
    on_log: Optional[LogFn] = None,
    timeout: float = 1800.0,
) -> Tuple[int, List[str]]:
    """
    Run a playbook against a single host. Streams each output line to on_log.
    Returns (returncode, output_lines). Secrets in extra_vars go through a 0600 temp file,
    not the command line.
    """
    if not ansible_available():
        raise RuntimeError("ansible-playbook is not installed")
    key = private_key_path()
    if not key.exists():
        raise RuntimeError(f"SSH private key not found at {key} (mount it as secrets/ssh_key)")

    vars_all = dict(extra_vars or {})
    vars_all["ansible_port"] = port

    with tempfile.TemporaryDirectory(prefix="ansible-run-") as tmp:
        tmp_key = Path(tmp) / "key"
        shutil.copyfile(key, tmp_key)
        tmp_key.chmod(0o600)  # ssh refuses keys with loose permissions (e.g. read-only mounts)
        vars_file = Path(tmp) / "vars.json"
        vars_file.write_text(json.dumps(vars_all))
        vars_file.chmod(0o600)

        cmd = [
            "ansible-playbook", "-i", f"{host},", "-u", user,
            f"--private-key={tmp_key}",
            "-e", f"@{vars_file}",
            str(playbook),
        ]
        env = dict(
            os.environ,
            ANSIBLE_CONFIG=str(PLAYBOOKS_DIR / "ansible.cfg"),
            ANSIBLE_LOCAL_TEMP=str(Path(tmp) / "local"),
            # No ANSIBLE_REMOTE_TEMP override: let it default to Ansible's own '~/.ansible/tmp'. A prior
            # version hardcoded a single shared /tmp/.ansible (then, briefly, /tmp/.ansible-<user>) for
            # every play. DO never noticed because it connects as root and becomes root - same identity,
            # so a shared path is harmless. GCP connects as a non-root user (tpotadmin) and becomes root
            # per task (every play here uses become: true). Whichever task first needs a remote temp dir
            # creates the shared base path - and modules that fetch content *on* the remote host (e.g.
            # get_url, downloading the Docker GPG key) do that entirely under become, creating the base
            # dir owned by root 0700. Modules that transfer *local* content to the remote host (copy/
            # template - the Hive cert, .env, docker-compose.yml, the logstash pipeline) stage it over
            # SFTP as the raw connecting user first, before the become'd move into place, and that SFTP
            # phase can no longer create anything under a root-owned base dir - found live, verified with
            # -vvv against a real instance: "Failed to create temporary directory... did not have
            # permissions on the target directory", at the very first copy task, right after several
            # become'd shell-command tasks (which never needed a temp dir at all, thanks to pipelining)
            # had already succeeded. A literal fixed path can't dodge this, whatever name it uses, because
            # both identities (the become target and the raw connecting user) are forced to the exact same
            # absolute path. '~/.ansible/tmp' fixes it structurally: '~' resolves per *effective* user at
            # the moment each command actually runs, so become'd commands and raw-connecting-user commands
            # land in different directories (/root/.ansible/tmp vs /home/tpotadmin/.ansible/tmp) and never
            # collide - which is exactly why this is Ansible's own default, not a workaround we invented.
        )
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, env=env)
        lines: List[str] = []
        deadline = time.monotonic() + timeout
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                lines.append(line)
                if on_log:
                    on_log(line)
            if time.monotonic() > deadline:
                proc.kill()
                lines.append(f"ERROR: playbook timed out after {int(timeout)}s")
                if on_log:
                    on_log(lines[-1])
                break
        return proc.wait(), lines
