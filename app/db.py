import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Generator

from app.config import DB_PATH

logger = logging.getLogger("tpot.db")


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    target_path = Path(db_path) if db_path else DB_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    # High-performance WAL mode & busy timeout
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


@contextmanager
def get_db(db_path: Optional[Path] = None) -> Generator[sqlite3.Connection, None, None]:
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """Idempotent 'ALTER TABLE ADD COLUMN': init_db runs on every process start and must stay safe
    to re-run against a database created by an older version of the schema."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db(db_path: Optional[Path] = None) -> Path:
    """Create the SQLite schema (idempotent) and seed default cloud metadata."""
    target_path = Path(db_path) if db_path else DB_PATH
    with get_db(target_path) as conn:
        # 1. Leases Table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS leases (
                droplet_id INTEGER PRIMARY KEY,
                droplet_name TEXT NOT NULL,
                sensor_user TEXT,
                ttl TEXT NOT NULL,
                ttl_seconds INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
        """)
        # provider/region (zone, for GCP): which cloud client can destroy this lease's machine, and
        # in which zone (GCP delete/get calls are zone-scoped, unlike DO's flat droplet id).
        _add_column_if_missing(conn, "leases", "provider", "TEXT NOT NULL DEFAULT 'digitalocean'")
        _add_column_if_missing(conn, "leases", "region", "TEXT")

        # 2. Schedules Table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schedules (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                preset_id TEXT,
                sensor_type TEXT NOT NULL,
                description TEXT,
                enabled INTEGER DEFAULT 1,
                timing_json TEXT NOT NULL,
                config_json TEXT NOT NULL,
                state_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)

        # 3. Static IPs Table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS static_ips (
                ip TEXT PRIMARY KEY,
                comment TEXT DEFAULT '',
                added_at TEXT NOT NULL
            );
        """)

        # 4. Fleet Historical Telemetry Table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fleet_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                droplet_id INTEGER,
                name TEXT NOT NULL,
                sensor_type TEXT NOT NULL,
                provider TEXT DEFAULT 'digitalocean',
                public_ip TEXT,
                created_at TEXT NOT NULL,
                destroyed_at TEXT,
                reason TEXT
            );
        """)

        # 5. Active Droplets Fleet Tracker (Database-as-source-of-truth)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS active_droplets (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                public_ip TEXT,
                private_ip TEXT,
                sensor_type TEXT NOT NULL DEFAULT 'cowrie',
                sensor_user TEXT,
                region TEXT,
                size TEXT,
                image TEXT,
                tags_json TEXT DEFAULT '[]',
                status TEXT DEFAULT 'active',
                ttl TEXT,
                ttl_seconds INTEGER,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                data_json TEXT DEFAULT '{}'
            );
        """)
        # Which cloud client owns this row ('digitalocean' or 'gcp'). `region` already carries the DO
        # region slug or the GCP zone generically - no separate zone column needed.
        _add_column_if_missing(conn, "active_droplets", "provider", "TEXT NOT NULL DEFAULT 'digitalocean'")

        # 6. Cloud Metadata Tables (Regions, Sizes, Images, Keys, Tags)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cloud_regions (
                slug TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                available INTEGER DEFAULT 1,
                features_json TEXT DEFAULT '[]',
                sizes_json TEXT DEFAULT '[]'
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS cloud_sizes (
                slug TEXT PRIMARY KEY,
                memory INTEGER NOT NULL,
                vcpus INTEGER NOT NULL,
                disk INTEGER NOT NULL,
                transfer REAL DEFAULT 0,
                price_monthly REAL NOT NULL,
                price_hourly REAL DEFAULT 0,
                description TEXT,
                available INTEGER DEFAULT 1
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS cloud_images (
                id INTEGER PRIMARY KEY,
                slug TEXT,
                name TEXT NOT NULL,
                distribution TEXT,
                description TEXT,
                min_disk_size INTEGER DEFAULT 0
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS cloud_ssh_keys (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                fingerprint TEXT,
                public_key TEXT
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS cloud_tags (
                name TEXT PRIMARY KEY
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS cloud_metadata_sync (
                key TEXT PRIMARY KEY,
                last_synced_at TEXT NOT NULL,
                stats_json TEXT DEFAULT '{}'
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL
            );
        """)

        _seed_default_metadata(conn)

    return target_path


DEFAULT_REGIONS = [
    {"slug": "nyc1", "name": "New York 1", "available": 1},
    {"slug": "nyc3", "name": "New York 3", "available": 1},
    {"slug": "sfo3", "name": "San Francisco 3", "available": 1},
    {"slug": "ams3", "name": "Amsterdam 3", "available": 1},
    {"slug": "fra1", "name": "Frankfurt 1", "available": 1},
    {"slug": "lon1", "name": "London 1", "available": 1},
    {"slug": "sgp1", "name": "Singapore 1", "available": 1},
    {"slug": "tor1", "name": "Toronto 1", "available": 1},
    {"slug": "blr1", "name": "Bangalore 1", "available": 1},
    {"slug": "syd1", "name": "Sydney 1", "available": 1}
]

DEFAULT_SIZES = [
    {"slug": "s-1vcpu-1gb", "memory": 1024, "vcpus": 1, "disk": 25, "transfer": 1.0, "price_monthly": 6.0, "price_hourly": 0.00893, "description": "Basic 1GB / 1 CPU", "available": 1},
    {"slug": "s-1vcpu-2gb", "memory": 2048, "vcpus": 1, "disk": 50, "transfer": 2.0, "price_monthly": 12.0, "price_hourly": 0.01786, "description": "Basic 2GB / 1 CPU (Recommended)", "available": 1},
    {"slug": "s-2vcpu-2gb", "memory": 2048, "vcpus": 2, "disk": 60, "transfer": 3.0, "price_monthly": 18.0, "price_hourly": 0.02679, "description": "Basic 2GB / 2 CPUs", "available": 1},
    {"slug": "s-2vcpu-4gb", "memory": 4096, "vcpus": 2, "disk": 80, "transfer": 4.0, "price_monthly": 24.0, "price_hourly": 0.03571, "description": "Basic 4GB / 2 CPUs (Multi-Sensor)", "available": 1},
    {"slug": "s-4vcpu-8gb", "memory": 8192, "vcpus": 4, "disk": 160, "transfer": 5.0, "price_monthly": 48.0, "price_hourly": 0.07143, "description": "Basic 8GB / 4 CPUs", "available": 1}
]

DEFAULT_IMAGES = [
    {"id": 235153036, "slug": "ubuntu-24-04-x64", "name": "Ubuntu 24.04 (LTS) x64", "distribution": "Ubuntu", "description": "Ubuntu 24.04 LTS (Noble Numbat)", "min_disk_size": 7},
    {"id": 140927038, "slug": "ubuntu-22-04-x64", "name": "Ubuntu 22.04 (LTS) x64", "distribution": "Ubuntu", "description": "Ubuntu 22.04 LTS (Jammy Jellyfish)", "min_disk_size": 7}
]

DEFAULT_TAGS = [
    "tpot",
    "tpot-sensor",
    "tpot-cowrie-sensor",
    "tpot-dionaea-sensor",
    "tpot-conpot-sensor",
    "tpot-elasticpot-sensor",
    "tpot-mailoney-sensor",
    "tpot-heralding-sensor",
    "tpot-ciscoasa-sensor",
    "tpot-citrix-sensor",
    "tpot-redishoneypot-sensor",
    "tpot-sentrypeer-sensor",
    "tpot-adbhoney-sensor",
    "tpot-multi_sensor-sensor"
]


def _seed_default_metadata(conn: sqlite3.Connection):
    """Seed standard cloud options into SQLite if tables are empty."""
    # Regions
    if conn.execute("SELECT COUNT(*) as cnt FROM cloud_regions").fetchone()["cnt"] == 0:
        for r in DEFAULT_REGIONS:
            conn.execute("INSERT OR IGNORE INTO cloud_regions (slug, name, available) VALUES (?, ?, ?)",
                         (r["slug"], r["name"], r.get("available", 1)))
    # Sizes
    if conn.execute("SELECT COUNT(*) as cnt FROM cloud_sizes").fetchone()["cnt"] == 0:
        for s in DEFAULT_SIZES:
            conn.execute("""
                INSERT OR IGNORE INTO cloud_sizes (slug, memory, vcpus, disk, transfer, price_monthly, price_hourly, description, available)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (s["slug"], s["memory"], s["vcpus"], s["disk"], s.get("transfer", 0), s["price_monthly"], s.get("price_hourly", 0), s["description"], s.get("available", 1)))
    # Images
    if conn.execute("SELECT COUNT(*) as cnt FROM cloud_images").fetchone()["cnt"] == 0:
        for img in DEFAULT_IMAGES:
            conn.execute("""
                INSERT OR IGNORE INTO cloud_images (id, slug, name, distribution, description, min_disk_size)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (img["id"], img["slug"], img["name"], img["distribution"], img["description"], img["min_disk_size"]))
    # Tags
    if conn.execute("SELECT COUNT(*) as cnt FROM cloud_tags").fetchone()["cnt"] == 0:
        for t in DEFAULT_TAGS:
            conn.execute("INSERT OR IGNORE INTO cloud_tags (name) VALUES (?)", (t,))


# ---------------------------------------------------------
# Leases Query Operations
# ---------------------------------------------------------
def db_get_all_leases(db_path: Optional[Path] = None) -> Dict[str, dict]:
    init_db(db_path)
    leases = {}
    with get_db(db_path) as conn:
        for row in conn.execute("SELECT * FROM leases").fetchall():
            d = dict(row)
            leases[str(d["droplet_id"])] = d
    return leases


def db_get_lease(droplet_id: int, db_path: Optional[Path] = None) -> Optional[dict]:
    init_db(db_path)
    with get_db(db_path) as conn:
        row = conn.execute("SELECT * FROM leases WHERE droplet_id = ?", (int(droplet_id),)).fetchone()
        return dict(row) if row else None


def db_save_lease(lease_entry: dict, db_path: Optional[Path] = None):
    init_db(db_path)
    with get_db(db_path) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO leases (
                droplet_id, droplet_name, sensor_user, ttl, ttl_seconds, created_at, expires_at,
                provider, region
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            int(lease_entry["droplet_id"]),
            lease_entry.get("droplet_name", str(lease_entry["droplet_id"])),
            lease_entry.get("sensor_user"),
            lease_entry.get("ttl", "2h"),
            int(lease_entry.get("ttl_seconds", 7200)),
            lease_entry.get("created_at", datetime.now(timezone.utc).isoformat()),
            lease_entry.get("expires_at", datetime.now(timezone.utc).isoformat()),
            lease_entry.get("provider", "digitalocean"),
            lease_entry.get("region"),
        ))


def db_delete_lease(droplet_id: int, db_path: Optional[Path] = None) -> bool:
    init_db(db_path)
    with get_db(db_path) as conn:
        cur = conn.execute("DELETE FROM leases WHERE droplet_id = ?", (int(droplet_id),))
        return cur.rowcount > 0


# ---------------------------------------------------------
# Schedules Query Operations
# ---------------------------------------------------------
def db_get_all_schedules(db_path: Optional[Path] = None) -> Dict[str, dict]:
    init_db(db_path)
    schedules = {}
    with get_db(db_path) as conn:
        for row in conn.execute("SELECT * FROM schedules").fetchall():
            s = _row_to_schedule(row)
            schedules[s["id"]] = s
    return schedules


def db_get_schedule(schedule_id: str, db_path: Optional[Path] = None) -> Optional[dict]:
    init_db(db_path)
    with get_db(db_path) as conn:
        row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
        return _row_to_schedule(row) if row else None


def db_save_schedule(schedule_dict: dict, db_path: Optional[Path] = None):
    init_db(db_path)
    now_iso = datetime.now(timezone.utc).isoformat()
    sid = schedule_dict.get("id")
    timing = json.dumps(schedule_dict.get("timing", {}))
    config = json.dumps(schedule_dict.get("config", {}))
    state = json.dumps(schedule_dict.get("state", {}))
    with get_db(db_path) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO schedules (id, name, preset_id, sensor_type, description, enabled, timing_json, config_json, state_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            sid,
            schedule_dict.get("name", "Unnamed Schedule"),
            schedule_dict.get("preset_id"),
            schedule_dict.get("config", {}).get("sensor_type", schedule_dict.get("sensor_type", "cowrie")),
            schedule_dict.get("description"),
            1 if schedule_dict.get("enabled", True) else 0,
            timing,
            config,
            state,
            schedule_dict.get("created_at", now_iso),
            now_iso
        ))


def db_update_schedule_state(schedule_id: str, state: dict, db_path: Optional[Path] = None):
    init_db(db_path)
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_db(db_path) as conn:
        conn.execute("""
            UPDATE schedules SET state_json = ?, updated_at = ? WHERE id = ?
        """, (json.dumps(state), now_iso, schedule_id))


def db_delete_schedule(schedule_id: str, db_path: Optional[Path] = None) -> bool:
    init_db(db_path)
    with get_db(db_path) as conn:
        cur = conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        return cur.rowcount > 0


def _row_to_schedule(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        timing = json.loads(d.get("timing_json", "{}"))
    except Exception:
        timing = {}
    try:
        config = json.loads(d.get("config_json", "{}"))
    except Exception:
        config = {}
    try:
        state = json.loads(d.get("state_json", "{}"))
    except Exception:
        state = {}

    return {
        "id": d["id"],
        "name": d["name"],
        "preset_id": d.get("preset_id"),
        "sensor_type": d.get("sensor_type", "cowrie"),
        "description": d.get("description"),
        "enabled": bool(d.get("enabled", 1)),
        "timing": timing,
        "config": config,
        "state": state,
        "created_at": d.get("created_at"),
        "updated_at": d.get("updated_at")
    }


# ---------------------------------------------------------
# Static IPs Query Operations
# ---------------------------------------------------------
def db_get_static_ips(db_path: Optional[Path] = None) -> List[dict]:
    init_db(db_path)
    with get_db(db_path) as conn:
        rows = conn.execute("SELECT * FROM static_ips ORDER BY added_at ASC").fetchall()
        return [dict(r) for r in rows]


def db_add_static_ip(ip: str, comment: str = "", added_at: Optional[str] = None, db_path: Optional[Path] = None) -> bool:
    init_db(db_path)
    clean_ip = ip.strip()
    ts = added_at or datetime.now(timezone.utc).isoformat()
    with get_db(db_path) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO static_ips (ip, comment, added_at)
            VALUES (?, ?, ?)
        """, (clean_ip, comment.strip(), ts))
    return True


def db_remove_static_ip(ip: str, db_path: Optional[Path] = None) -> bool:
    init_db(db_path)
    clean_ip = ip.strip()
    with get_db(db_path) as conn:
        cur = conn.execute("DELETE FROM static_ips WHERE ip = ?", (clean_ip,))
        return cur.rowcount > 0


# ---------------------------------------------------------
# Fleet Historical Telemetry
# ---------------------------------------------------------
def db_record_sensor_launch(
    droplet_id: int,
    name: str,
    sensor_type: str,
    provider: str = "digitalocean",
    public_ip: Optional[str] = None,
    created_at: Optional[str] = None,
    db_path: Optional[Path] = None
):
    init_db(db_path)
    ts = created_at or datetime.now(timezone.utc).isoformat()
    with get_db(db_path) as conn:
        conn.execute("""
            INSERT INTO fleet_history (droplet_id, name, sensor_type, provider, public_ip, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (int(droplet_id), name, sensor_type, provider, public_ip, ts))


def db_record_sensor_destroy(
    droplet_id: int,
    destroyed_at: Optional[str] = None,
    reason: str = "TTL Expired",
    db_path: Optional[Path] = None
):
    init_db(db_path)
    ts = destroyed_at or datetime.now(timezone.utc).isoformat()
    with get_db(db_path) as conn:
        conn.execute("""
            UPDATE fleet_history
            SET destroyed_at = ?, reason = ?
            WHERE droplet_id = ? AND destroyed_at IS NULL
        """, (ts, reason, int(droplet_id)))


def db_get_fleet_history(limit: int = 50, db_path: Optional[Path] = None) -> List[dict]:
    init_db(db_path)
    with get_db(db_path) as conn:
        rows = conn.execute("""
            SELECT * FROM fleet_history ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------
# Active Droplets Fleet Tracker (Database-as-Source-of-Truth)
# ---------------------------------------------------------
def db_get_all_active_droplets(db_path: Optional[Path] = None) -> List[dict]:
    """Retrieve all actively deployed droplets stored in SQLite."""
    init_db(db_path)
    with get_db(db_path) as conn:
        rows = conn.execute("SELECT * FROM active_droplets ORDER BY created_at DESC").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["tags"] = json.loads(d.get("tags_json") or "[]")
            except Exception:
                d["tags"] = []
            try:
                d["data"] = json.loads(d.get("data_json") or "{}")
            except Exception:
                d["data"] = {}
            result.append(d)
        return result


def db_get_active_droplet(droplet_id: int, db_path: Optional[Path] = None) -> Optional[dict]:
    init_db(db_path)
    with get_db(db_path) as conn:
        row = conn.execute("SELECT * FROM active_droplets WHERE id = ?", (int(droplet_id),)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["tags"] = json.loads(d.get("tags_json") or "[]")
        except Exception:
            d["tags"] = []
        try:
            d["data"] = json.loads(d.get("data_json") or "{}")
        except Exception:
            d["data"] = {}
        return d


def db_save_active_droplet(d: Any, db_path: Optional[Path] = None):
    """Upsert a tracked machine. Accepts a models.Sensor or an equivalent dict."""
    init_db(db_path)
    if hasattr(d, "model_dump"):
        d = d.model_dump()
    tags = d.get("tags") or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except Exception:
            tags = [tags]
    tags_json = json.dumps(tags)
    data_json = json.dumps(d.get("data") or {})
    with get_db(db_path) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO active_droplets (
                id, name, public_ip, private_ip, sensor_type, sensor_user,
                region, size, image, tags_json, status, ttl, ttl_seconds,
                created_at, expires_at, data_json, provider
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            int(d["id"]),
            d.get("name", str(d["id"])),
            d.get("public_ip"),
            d.get("private_ip"),
            d.get("sensor_type", "cowrie"),
            d.get("sensor_user"),
            d.get("region", "nyc1"),
            d.get("size", "s-1vcpu-2gb"),
            d.get("image", "ubuntu-24-04-x64"),
            tags_json,
            d.get("status", "active"),
            d.get("ttl"),
            d.get("ttl_seconds"),
            d.get("created_at") or datetime.now(timezone.utc).isoformat(),
            d.get("expires_at"),
            data_json,
            d.get("provider", "digitalocean"),
        ))


def db_fail_interrupted_provisioning(db_path: Optional[Path] = None) -> int:
    """Manual deploys left in 'provisioning' by a dead worker become 'provision_failed' (campaign cycles
    are excluded: the scheduler detects and cleans those itself)."""
    init_db(db_path)
    with get_db(db_path) as conn:
        cur = conn.execute(
            "UPDATE active_droplets SET status = 'provision_failed' "
            "WHERE status = 'provisioning' AND tags_json NOT LIKE '%sched-%'"
        )
        return cur.rowcount


def db_delete_active_droplet(droplet_id: int, db_path: Optional[Path] = None) -> bool:
    init_db(db_path)
    with get_db(db_path) as conn:
        cur = conn.execute("DELETE FROM active_droplets WHERE id = ?", (int(droplet_id),))
        return cur.rowcount > 0


def db_sync_active_droplets(cloud_droplets: List[dict], db_path: Optional[Path] = None) -> dict:
    """
    Reconciles the local active_droplets table against a live list of droplets from DigitalOcean.
    Used exclusively for manual sync.
    """
    init_db(db_path)
    synced_ids = set()
    with get_db(db_path) as conn:
        leases = {row["droplet_id"]: dict(row) for row in conn.execute("SELECT * FROM leases").fetchall()}

        for d in cloud_droplets:
            did = int(d["id"])
            synced_ids.add(did)

            tags = d.get("tags", [])
            stype = "cowrie"
            for t in tags:
                if t.startswith("type-"):
                    stype = t.replace("type-", "")
                    break
                elif t.startswith("tpot-") and t.endswith("-sensor") and t != "tpot-sensor":
                    stype = t.replace("tpot-", "").replace("-sensor", "")
                    break

            lease = leases.get(did, {})
            pub_ip = d.get("public_ip")
            if not pub_ip:
                v4_nets = d.get("networks", {}).get("v4", []) if isinstance(d.get("networks"), dict) else []
                pub_ip = next((net["ip_address"] for net in v4_nets if net.get("type") == "public"), None)
            priv_ip = d.get("private_ip")
            if not priv_ip:
                v4_nets = d.get("networks", {}).get("v4", []) if isinstance(d.get("networks"), dict) else []
                priv_ip = next((net["ip_address"] for net in v4_nets if net.get("type") == "private"), None)

            reg_slug = d.get("region", {}).get("slug", "nyc1") if isinstance(d.get("region"), dict) else str(d.get("region") or "nyc1")
            size_slug = d.get("size", {}).get("slug", "s-1vcpu-2gb") if isinstance(d.get("size"), dict) else str(d.get("size") or "s-1vcpu-2gb")
            img_slug = d.get("image", {}).get("slug", "ubuntu-24-04-x64") if isinstance(d.get("image"), dict) else str(d.get("image") or "ubuntu-24-04-x64")

            conn.execute("""
                INSERT OR REPLACE INTO active_droplets (
                    id, name, public_ip, private_ip, sensor_type, sensor_user,
                    region, size, image, tags_json, status, ttl, ttl_seconds,
                    created_at, expires_at, data_json, provider
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                did,
                d.get("name", str(did)),
                pub_ip,
                priv_ip,
                stype,
                lease.get("sensor_user"),
                reg_slug,
                size_slug,
                img_slug,
                json.dumps(tags),
                d.get("status", "active"),
                lease.get("ttl"),
                lease.get("ttl_seconds"),
                d.get("created_at") or datetime.now(timezone.utc).isoformat(),
                lease.get("expires_at"),
                json.dumps(d),
                "digitalocean",
            ))

        # Remove local active_droplets that were destroyed outside the app. This sync is DO-only
        # (cloud_droplets always comes from DOClient.list_droplets), so only reconcile DO rows -
        # leaving every GCP row alone regardless of synced_ids, which never contains a GCP instance id.
        current_local = conn.execute(
            "SELECT id FROM active_droplets WHERE provider = 'digitalocean'"
        ).fetchall()
        removed_count = 0
        for r in current_local:
            if r["id"] not in synced_ids:
                conn.execute("DELETE FROM active_droplets WHERE id = ?", (r["id"],))
                removed_count += 1

    return {
        "active_count": len(synced_ids),
        "synced_ids": list(synced_ids),
        "removed_count": removed_count
    }


# ---------------------------------------------------------
# Cloud Metadata Operations (Regions, Sizes, Images, Keys, Tags)
# ---------------------------------------------------------
def db_get_cloud_options(db_path: Optional[Path] = None) -> dict:
    """Retrieve cached cloud options (regions, sizes, images, ssh keys, tags) from SQLite."""
    init_db(db_path)
    with get_db(db_path) as conn:
        regions = [dict(r) for r in conn.execute("SELECT * FROM cloud_regions WHERE available = 1 ORDER BY name ASC").fetchall()]
        sizes = [dict(r) for r in conn.execute("SELECT * FROM cloud_sizes WHERE available = 1 ORDER BY price_monthly ASC").fetchall()]
        images = [dict(r) for r in conn.execute("SELECT * FROM cloud_images ORDER BY name DESC").fetchall()]
        keys = [dict(r) for r in conn.execute("SELECT * FROM cloud_ssh_keys ORDER BY name ASC").fetchall()]
        tags = [r["name"] for r in conn.execute("SELECT name FROM cloud_tags ORDER BY name ASC").fetchall()]
        sync_row = conn.execute("SELECT * FROM cloud_metadata_sync WHERE key = 'do_metadata'").fetchone()
        last_sync = sync_row["last_synced_at"] if sync_row else None
        stats = json.loads(sync_row["stats_json"]) if (sync_row and sync_row["stats_json"]) else {}

    return {
        "regions": regions,
        "sizes": sizes,
        "images": images,
        "ssh_keys": keys,
        "tags": tags,
        "last_synced_at": last_sync,
        "sync_stats": stats
    }


def db_save_cloud_metadata(
    regions: Optional[List[dict]] = None,
    sizes: Optional[List[dict]] = None,
    images: Optional[List[dict]] = None,
    ssh_keys: Optional[List[dict]] = None,
    tags: Optional[List[Any]] = None,
    db_path: Optional[Path] = None
) -> dict:
    """Save synced DigitalOcean cloud metadata into SQLite and update sync timestamp."""
    init_db(db_path)
    now_iso = datetime.now(timezone.utc).isoformat()
    stats = {}

    with get_db(db_path) as conn:
        if regions is not None:
            conn.execute("DELETE FROM cloud_regions")
            for r in regions:
                conn.execute("""
                    INSERT OR REPLACE INTO cloud_regions (slug, name, available)
                    VALUES (?, ?, ?)
                """, (r["slug"], r.get("name", r["slug"].upper()), 1 if r.get("available", True) else 0))
            stats["regions"] = len(regions)

        if sizes is not None:
            conn.execute("DELETE FROM cloud_sizes")
            for s in sizes:
                conn.execute("""
                    INSERT OR REPLACE INTO cloud_sizes (slug, memory, vcpus, disk, transfer, price_monthly, price_hourly, description, available)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    s["slug"],
                    s.get("memory", 0),
                    s.get("vcpus", 0),
                    s.get("disk", 0),
                    s.get("transfer", 0),
                    s.get("price_monthly", 0),
                    s.get("price_hourly", 0),
                    s.get("description", s["slug"]),
                    1 if s.get("available", True) else 0
                ))
            stats["sizes"] = len(sizes)

        if images is not None:
            conn.execute("DELETE FROM cloud_images")
            for img in images:
                conn.execute("""
                    INSERT OR REPLACE INTO cloud_images (id, slug, name, distribution, description, min_disk_size)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    int(img.get("id") or 0),
                    img.get("slug") or str(img.get("id")),
                    img.get("name", "Ubuntu"),
                    img.get("distribution", "Ubuntu"),
                    img.get("description", img.get("name", "")),
                    img.get("min_disk_size", 0)
                ))
            stats["images"] = len(images)

        if ssh_keys is not None:
            conn.execute("DELETE FROM cloud_ssh_keys")
            for k in ssh_keys:
                conn.execute("""
                    INSERT OR REPLACE INTO cloud_ssh_keys (id, name, fingerprint, public_key)
                    VALUES (?, ?, ?, ?)
                """, (
                    str(k["id"]),
                    k.get("name", str(k["id"])),
                    k.get("fingerprint"),
                    k.get("public_key")
                ))
            stats["ssh_keys"] = len(ssh_keys)

        if tags is not None:
            for t in tags:
                tag_name = t if isinstance(t, str) else t.get("name")
                if tag_name:
                    conn.execute("INSERT OR IGNORE INTO cloud_tags (name) VALUES (?)", (tag_name,))
            stats["tags"] = conn.execute("SELECT COUNT(*) as cnt FROM cloud_tags").fetchone()["cnt"]

        conn.execute("""
            INSERT OR REPLACE INTO cloud_metadata_sync (key, last_synced_at, stats_json)
            VALUES (?, ?, ?)
        """, ("do_metadata", now_iso, json.dumps(stats)))

    return stats


def db_save_account_verification(account: dict, db_path: Optional[Path] = None):
    init_db(db_path)
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_db(db_path) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO cloud_metadata_sync (key, last_synced_at, stats_json)
            VALUES (?, ?, ?)
        """, ("do_account", now_iso, json.dumps(account)))


def db_get_cached_account(db_path: Optional[Path] = None) -> Optional[dict]:
    init_db(db_path)
    with get_db(db_path) as conn:
        row = conn.execute("SELECT * FROM cloud_metadata_sync WHERE key = 'do_account'").fetchone()
        if row and row["stats_json"]:
            try:
                acc = json.loads(row["stats_json"])
                acc["verified_at"] = row["last_synced_at"]
                return acc
            except Exception:
                return None
        return None



# ---------------------------------------------------------
# Settings (UI-editable configuration)
# ---------------------------------------------------------
def db_get_settings(db_path: Optional[Path] = None) -> Dict[str, Any]:
    init_db(db_path)
    with get_db(db_path) as conn:
        return {r["key"]: json.loads(r["value_json"]) for r in conn.execute("SELECT key, value_json FROM settings")}


def db_save_settings(values: Dict[str, Any], db_path: Optional[Path] = None):
    init_db(db_path)
    with get_db(db_path) as conn:
        for k, v in values.items():
            conn.execute(
                "INSERT INTO settings (key, value_json) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
                (k, json.dumps(v)),
            )
