#!/bin/bash
set -euo pipefail
exec > >(tee /var/log/tpot-${sensor_type}-bootstrap.log) 2>&1

echo "============================================================"
echo "  GCP T-Pot Sensor Deployment: ${sensor_name} [${sensor_type}]"
echo "  Target Hive: ${hive_ip}:${hive_port}"
echo "============================================================"

# 1. Configure Swap (2GB) so Logstash & Docker run smoothly
if [ ! -f /swapfile ]; then
    echo "[1/6] Creating 2GB swap file..."
    fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# 2. Relocate Host Admin SSH from Port 22 to ${admin_ssh_port}
echo "[2/6] Relocating host SSH service to port ${admin_ssh_port}..."
mkdir -p /etc/ssh/sshd_config.d
cat << 'EOF_PORT' > /etc/ssh/sshd_config.d/tpot_admin_port.conf
Port ${admin_ssh_port}
EOF_PORT

sed -i 's/^#*Port 22/Port ${admin_ssh_port}/' /etc/ssh/sshd_config || true
sed -i 's/^Port 22/Port ${admin_ssh_port}/' /etc/ssh/sshd_config || true

systemctl restart ssh || systemctl restart sshd || true

# 3. Install Docker Engine & Compose plugin
echo "[3/6] Installing Docker Engine & Compose..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y ca-certificates curl gnupg apt-transport-https

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes
chmod a+r /etc/apt/keyrings/docker.gpg

UBUNTU_CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME")
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $${UBUNTU_CODENAME} stable" | \
  tee /etc/apt/sources.list.d/docker.list > /dev/null

apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

systemctl enable --now docker

# 4. Create T-Pot user (UID/GID 2000) and directories
echo "[4/6] Initializing T-Pot directories..."
useradd -u 2000 -r -s /bin/false tpot 2>/dev/null || true
mkdir -p /opt/tpotce
mkdir -p /data/blackhole
mkdir -p /data/logstash
%{ for dir_path in required_dirs ~}
mkdir -p ${dir_path}
%{ endfor ~}

# 5. Write Hive Certificate
echo "[5/6] Writing Hive SSL certificate..."
cat << 'EOF_CERT' > /data/hive.crt
${hive_cert}
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
TPOT_HIVE_USER=${tpot_hive_user}
TPOT_HIVE_IP=${hive_ip}
LS_SSL_VERIFICATION=full
TPOT_REPO=dtagdevsec
TPOT_VERSION=${tpot_version}
TPOT_PULL_POLICY=always
TPOT_DATA_PATH=/data
TPOT_DOCKER_COMPOSE=/opt/tpotce/docker-compose.yml
MY_HOSTNAME=${sensor_name}
EOF_ENV

cat << 'EOF_COMPOSE' > /opt/tpotce/docker-compose.yml
${compose_content}
EOF_COMPOSE

# Start the sensor stack
cd /opt/tpotce
docker compose pull
docker compose up -d

echo "============================================================"
echo "  GCP T-Pot [${sensor_type}] Sensor bootstrap completed successfully!"
echo "  Admin SSH Port: ${admin_ssh_port}"
echo "============================================================"
touch /opt/tpotce/.bootstrap_complete
