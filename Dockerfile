FROM python:3.11-slim

ARG TERRAFORM_VERSION=1.8.5
ARG TARGETARCH

LABEL description="T-Pot Multi-Sensor Deployer and Deception Orchestrator"

# curl/unzip: Terraform install. apache2-utils: htpasswd for Hive sensor credentials.
# openssh-client/git: used by Terraform and provisioning helpers.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    unzip \
    apache2-utils \
    iputils-ping \
    libnss-wrapper \
    openssh-client \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${TARGETARCH:-amd64}.zip" -o /tmp/terraform.zip \
    && unzip /tmp/terraform.zip -d /usr/local/bin/ \
    && rm /tmp/terraform.zip

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code is baked into the image (rebuild with `docker compose up -d --build`).
COPY . .

# Persistent state volume. World-writable so the container can run as any UID (see `user:` in docker-compose.yml);
# a fresh named volume inherits these permissions.
RUN mkdir -p /data && chmod 777 /data

ENV DATA_DIR=/data \
    TPOT_DIR=/tpot \
    SECRETS_DIR=/secrets \
    HOME=/tmp \
    PYTHONUNBUFFERED=1

RUN chmod +x /app/docker-entrypoint.sh
ENTRYPOINT ["/app/docker-entrypoint.sh"]

EXPOSE 8880

CMD ["python3", "-m", "uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8880"]
