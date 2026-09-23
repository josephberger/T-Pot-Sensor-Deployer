"""
Typed models: the one place that defines the shapes the app passes around.

- Settings: UI-editable configuration (persisted in the SQLite `settings` table).
- Sensor / SensorStatus: a tracked machine (row in `active_droplets`).
- *Payload: HTTP request bodies for the API.
"""

from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class Settings(BaseModel):
    region: str = "nyc1"
    size: str = "s-1vcpu-2gb"
    image: str = "ubuntu-24-04-x64"
    default_tag: str = "tpot-sensor"
    ttl: str = "2h"
    attach_firewall: bool = True
    firewall_restrict_ssh: bool = True
    auto_register_hive: bool = True
    honeypot_type: str = "cowrie"
    ssh_key_path: str = ""  # empty = default location (secrets/ssh_key.pub)
    hive_ip: str = ""
    hive_port: int = 64294
    # No DO token here: it's read from secrets/do_token (config.get_do_token), never stored.
    # GCP: no token to store (auth is the service account file at secrets/gcp-sa.json, picked up via
    # GOOGLE_APPLICATION_CREDENTIALS), just a project id and deploy-page defaults for zone/machine/image.
    gcp_project_id: str = ""
    gcp_zone: str = "us-central1-a"
    gcp_machine_type: str = "e2-small"
    gcp_image: str = "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64"


class SensorStatus(str, Enum):
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    PROVISION_FAILED = "provision_failed"


class Sensor(BaseModel):
    id: int
    name: str
    public_ip: Optional[str] = None
    private_ip: Optional[str] = None
    sensor_type: str = "cowrie"
    sensor_user: Optional[str] = None
    provider: str = "digitalocean"  # "digitalocean" | "gcp"
    region: str = "nyc1"  # DO region slug, or the GCP zone for a provider="gcp" row
    size: str = "s-1vcpu-2gb"  # DO size slug, or the GCP machine type
    image: str = "ubuntu-24-04-x64"
    tags: List[str] = Field(default_factory=list)
    status: str = SensorStatus.ACTIVE.value  # str: DO sync may also record provider statuses
    ttl: Optional[str] = None
    ttl_seconds: Optional[int] = None
    created_at: Optional[str] = None
    expires_at: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------
# API request payloads
# ---------------------------------------------------------
class SettingsPayload(BaseModel):
    region: Optional[str] = None
    size: Optional[str] = None
    image: Optional[str] = None
    ttl: Optional[str] = None
    attach_firewall: Optional[bool] = None
    firewall_restrict_ssh: Optional[bool] = None
    auto_register_hive: Optional[bool] = None
    ssh_key_path: Optional[str] = None
    hive_ip: Optional[str] = None
    hive_port: Optional[int] = None
    gcp_project_id: Optional[str] = None
    gcp_zone: Optional[str] = None
    gcp_machine_type: Optional[str] = None
    gcp_image: Optional[str] = None


class DeployPayload(BaseModel):
    name: Optional[str] = Field(default=None, max_length=255)  # DigitalOcean's own hostname length limit
    sensor_type: str = "cowrie"
    provider: Literal["digitalocean", "gcp"] = "digitalocean"
    # jobs.py only clamps a low count up to 1 (max(1, count)), never a high one down - an unbounded count
    # here would let one request loop run_do_deployment_job into creating that many real droplets.
    count: int = Field(default=1, ge=1, le=10)
    # DO region slug / GCP zone, DO size slug / GCP machine type, DO image slug / GCP image reference -
    # reused rather than duplicated per-provider fields (provider's own project id is a Settings-level
    # thing, not per-request, same as the DO token isn't in this payload either).
    region: str = "nyc1"
    size: str = "s-1vcpu-2gb"
    image: str = "ubuntu-24-04-x64"
    ttl: Optional[str] = "2h"
    attach_firewall: bool = True
    restrict_ssh: bool = True
    auto_register_hive: bool = True
    hive_ip: Optional[str] = None
    hive_port: int = 64294
    hive_cert: Optional[str] = None


class ActionPayload(BaseModel):
    action: str  # reboot, power_off, power_on


class TTLPayload(BaseModel):
    ttl: Optional[str] = None
    cancel: bool = False


class StaticIPPayload(BaseModel):
    ip: str = Field(max_length=64)
    comment: Optional[str] = Field(default="", max_length=500)


class StaticIPUpdatePayload(BaseModel):
    old_ip: str = Field(max_length=64)
    ip: str = Field(max_length=64)
    comment: Optional[str] = Field(default="", max_length=500)


class ScheduleCreatePayload(BaseModel):
    name: Optional[str] = Field(default=None, max_length=255)
    preset_id: Optional[str] = None
    sensor_type: Optional[str] = None
    description: Optional[str] = Field(default=None, max_length=2000)
    start_immediately: bool = True
    timing: Optional[Dict[str, Any]] = None
    config: Optional[Dict[str, Any]] = None


class ScheduleActionPayload(BaseModel):
    action: str  # pause, resume, trigger
