import os
from pathlib import Path
from dotenv import load_dotenv

from app.models import Settings

# Base paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = Path(__file__).resolve().parent
HOME_DIR = Path.home()

# Load .env from project root if present (in Docker, compose injects env vars instead)
ENV_PATH = PROJECT_ROOT / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)
else:
    load_dotenv()

# Persistent state (SQLite DB incl. settings, logs, EDL file, Terraform working dir).
# In Docker this is the /data volume (DATA_DIR=/data); otherwise ~/.tpot-sensor-deployer.
CONFIG_DIR = Path(os.environ.get("DATA_DIR") or (HOME_DIR / ".tpot-sensor-deployer"))
DB_PATH = CONFIG_DIR / "tpot.db"
LOGS_DIR = CONFIG_DIR / "logs"

# Ensure config directory exists
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# T-Pot Hive directory (mounted at /tpot in Docker; see TPOT_HOST_DIR in docker-compose.yml)
TPOT_DIR = Path(os.environ.get("TPOT_DIR") or (HOME_DIR / "tpotce"))

# Read-only secrets directory (mounted at /secrets in Docker): do_token, gcp-sa.json, ssh_key(.pub)
SECRETS_DIR = Path(os.environ.get("SECRETS_DIR", "/secrets"))
DEFAULT_SSH_PUBKEY_PATH = SECRETS_DIR / "ssh_key.pub"
# The DigitalOcean token lives only here: not in .env (env vars show up in `docker inspect`) and not
# in the database (it can't be set from the UI). Read on every call, so replacing it needs no restart.
DO_TOKEN_PATH = SECRETS_DIR / "do_token"


def load_config() -> dict:
    """Settings: model defaults, overlaid with saved values (SQLite), overlaid with environment variables."""
    from app.db import db_get_settings  # local import: db imports this module for paths

    cfg = Settings().model_dump()
    cfg.update({k: v for k, v in db_get_settings().items() if k in cfg})
    cfg["do_token"] = get_do_token()

    env_hive_ip = os.environ.get("TPOT_HIVE_IP") or os.environ.get("HIVE_IP")
    if env_hive_ip:
        cfg["hive_ip"] = env_hive_ip

    env_gcp_project = os.environ.get("GCP_PROJECT_ID")
    if env_gcp_project:
        cfg["gcp_project_id"] = env_gcp_project

    return cfg


def save_config(updates: dict) -> dict:
    """Validate and persist setting updates. Only explicit updates are stored, never env-derived values."""
    from app.db import db_get_settings, db_save_settings

    known = Settings.model_fields
    stored = db_get_settings()
    stored.update({k: v for k, v in updates.items() if k in known})
    valid = Settings(**{k: v for k, v in stored.items() if k in known})  # raises on bad types
    db_save_settings({k: getattr(valid, k) for k in stored if k in known})
    return load_config()


def public_settings(cfg: dict) -> dict:
    """Settings safe to send to the browser: secrets are replaced by a *_set flag."""
    out = {k: v for k, v in cfg.items() if k != "do_token"}
    out["do_token_set"] = bool(cfg.get("do_token"))
    out["do_token_path"] = str(DO_TOKEN_PATH)
    return out


def ssh_fingerprint(pubkey: str) -> str:
    """OpenSSH-style SHA256 fingerprint of a public key line ('' if unparseable)."""
    import base64
    import hashlib
    try:
        blob = base64.b64decode(pubkey.split()[1])
    except Exception:
        return ""
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def get_do_token() -> str:
    """DigitalOcean API token from secrets/do_token ('' if the file is missing or empty)."""
    try:
        return DO_TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def purge_stored_do_token():
    """Delete a DO token saved to the database by older versions (it was settable from the UI)."""
    from app.db import db_delete_setting
    db_delete_setting("do_token")


def get_local_ssh_pubkey() -> tuple:
    """Return (path, content) of the SSH public key injected into sensors; content is '' if absent."""
    path = Path(load_config().get("ssh_key_path") or DEFAULT_SSH_PUBKEY_PATH)
    try:
        return path, path.read_text(encoding="utf-8").strip()
    except OSError:
        return path, ""


def gcp_sa_key_path() -> Path:
    """Path to the GCP service account key (mounted read-only; see SECRETS_DIR)."""
    return SECRETS_DIR / "gcp-sa.json"


def gcp_credentials_available() -> bool:
    """Whether a GCP service account key file is present (not whether it's valid - that needs a real API call)."""
    return gcp_sa_key_path().exists()


def get_gcp_project_id() -> str:
    """Retrieve the configured GCP project id from env or settings."""
    return load_config().get("gcp_project_id", "") or os.environ.get("GCP_PROJECT_ID", "")


# Convenience aliases for worker jobs & modules
get_active_token = get_do_token
get_config = load_config

