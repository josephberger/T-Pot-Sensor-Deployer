import os
from pathlib import Path
from typing import Dict, Any, Optional

from app.config import PROJECT_ROOT
from app.sensor_types import get_sensor_type, build_sensor_compose_yaml


def generate_cloud_init(
    hive_ip: str,
    hive_port: int,
    hive_cert: str,
    sensor_name: str,
    tpot_hive_user: str,
    sensor_type: str = "cowrie",
    ssl_verification: str = "full",
    swap_size_gb: Optional[int] = None,
    ssh_port: int = 64295,
    tpot_version: str = "24.04",
    tpot_repo: str = "dtagdevsec"
) -> str:
    """
    Generate cloud-init user_data script that fully provisions an Ubuntu 24.04 instance
    as an autonomous T-Pot sensor shipping logs back to Hive.
    Supports any registered sensor type (Cowrie, Dionaea, Conpot, Elasticpot, Mailoney, etc.).
    """
    clean_cert = hive_cert.strip()
    st = get_sensor_type(sensor_type)
    st_id = st["id"]
    st_name = st["name"]

    # Resolve swap size
    resolved_swap = swap_size_gb if swap_size_gb is not None else st.get("swap_size_gb", 2)

    # Resolve directories to create under /data
    req_dirs = ["/data/blackhole", "/data/logstash"] + st.get("required_dirs", [])
    mkdir_commands = "\n".join([f"mkdir -p {d}" for d in req_dirs])

    # Ports string for banner
    ports_summary = ", ".join([f"{p['port']}/{p['proto'].upper()} ({p['service']})" for p in st.get("ports", [])])

    # Generate docker-compose YAML
    compose_content = build_sensor_compose_yaml(
        sensor_type=st_id,
        hive_ip=hive_ip,
        tpot_hive_user=tpot_hive_user,
        sensor_name=sensor_name,
        ssl_verification=ssl_verification,
        tpot_version=tpot_version,
        tpot_repo=tpot_repo
    )

    script = f"""#!/bin/bash
set -euo pipefail
exec > >(tee /var/log/tpot-{st_id}-bootstrap.log) 2>&1

echo "============================================================"
echo "  T-Pot Sensor Deployment: {sensor_name} [{st_name}]"
echo "  Target Hive: {hive_ip}:{hive_port}"
echo "  Active Ports: {ports_summary}"
echo "============================================================"

# 1. Ensure Swap File ({resolved_swap}GB) so Logstash & Docker never run out of memory
if [ ! -f /swapfile ]; then
    echo "[1/6] Creating {resolved_swap}GB swap file..."
    fallocate -l {resolved_swap}G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count={resolved_swap * 1024}
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# 2. Relocate Admin SSH from port 22 to {ssh_port}
echo "[2/6] Relocating host SSH service to port {ssh_port}..."
mkdir -p /etc/ssh/sshd_config.d
cat << 'EOF_PORT' > /etc/ssh/sshd_config.d/tpot_admin_port.conf
Port {ssh_port}
EOF_PORT

# Fallback in main sshd_config
sed -i 's/^#*Port 22/Port {ssh_port}/' /etc/ssh/sshd_config || true
sed -i 's/^Port 22/Port {ssh_port}/' /etc/ssh/sshd_config || true

systemctl restart ssh || systemctl restart sshd || true

# 3. Install Docker and Docker Compose Plugin
echo "[3/6] Installing Docker Engine & Docker Compose plugin..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y ca-certificates curl gnupg apt-transport-https

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes
chmod a+r /etc/apt/keyrings/docker.gpg

UBUNTU_CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME")
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${{UBUNTU_CODENAME}} stable" | \
  tee /etc/apt/sources.list.d/docker.list > /dev/null

apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

systemctl enable --now docker

# 4. Set up T-Pot user (UID/GID 2000) and directories
echo "[4/6] Creating T-Pot directory structure and permissions..."
useradd -u 2000 -r -s /bin/false tpot 2>/dev/null || true
mkdir -p /opt/tpotce
{mkdir_commands}

# 5. Write Hive Certificate
echo "[5/6] Writing Hive SSL certificate..."
cat << 'EOF_CERT' > /data/hive.crt
{clean_cert}
EOF_CERT

chown -R 2000:2000 /data
chmod 770 /data
chmod 644 /data/hive.crt

# 6. Write Configuration & Compose
echo "[6/6] Writing sensor .env and docker-compose.yml..."
cat << 'EOF_ENV' > /opt/tpotce/.env
TPOT_TYPE=SENSOR
TPOT_OSTYPE=linux
TPOT_BLACKHOLE=DISABLED
TPOT_PERSISTENCE=on
TPOT_ATTACKMAP_TEXT=ENABLED
TPOT_ATTACKMAP_TEXT_TIMEZONE=UTC
TPOT_HIVE_USER={tpot_hive_user}
TPOT_HIVE_IP={hive_ip}
LS_SSL_VERIFICATION={ssl_verification}
TPOT_REPO={tpot_repo}
TPOT_VERSION={tpot_version}
TPOT_PULL_POLICY=always
TPOT_DATA_PATH=/data
TPOT_DOCKER_COMPOSE=/opt/tpotce/docker-compose.yml
MY_HOSTNAME={sensor_name}
EOF_ENV

cat << 'EOF_COMPOSE' > /opt/tpotce/docker-compose.yml
{compose_content}
EOF_COMPOSE

# Start the sensor stack
cd /opt/tpotce
docker compose pull
docker compose up -d

echo "============================================================"
echo "  T-Pot Sensor [{st_name}] successfully deployed and running!"
echo "  Honeypot Ports: {ports_summary}"
echo "  Management SSH: {ssh_port}"
echo "============================================================"
touch /opt/tpotce/.provisioned
"""
    return script
