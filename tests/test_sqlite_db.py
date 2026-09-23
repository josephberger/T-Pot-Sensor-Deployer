# Isolate from real state: must run before any app module is imported.
import os as _os, sys as _sys, tempfile as _tempfile
from pathlib import Path as _Path
_os.environ["DATA_DIR"] = _tempfile.mkdtemp(prefix="tpot-test-")
# The T-Pot Hive is LIVE config (lswebpasswd + .env): tests must never resolve to the real one.
_os.environ["TPOT_DIR"] = _tempfile.mkdtemp(prefix="tpot-test-hive-")
_os.environ["SECRETS_DIR"] = _tempfile.mkdtemp(prefix="tpot-test-secrets-")
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone

from app.db import (
    init_db,
    get_connection,
    get_db,
    db_get_all_leases,
    db_get_lease,
    db_save_lease,
    db_delete_lease,
    db_get_all_schedules,
    db_get_schedule,
    db_save_schedule,
    db_update_schedule_state,
    db_delete_schedule,
    db_get_static_ips,
    db_add_static_ip,
    db_remove_static_ip,
    db_record_sensor_launch,
    db_record_sensor_destroy,
    db_get_fleet_history,
    db_get_active_droplet,
)
from app.ttl_manager import TTLManager
from app.edl_manager import EDLManager
from app.scheduler_manager import SchedulerManager


class TestSQLiteDatabase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)
        self.db_path = self.dir_path / "test_tpot.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_init_db_and_wal_mode(self):
        target = init_db(self.db_path)
        self.assertTrue(target.exists())

        conn = get_connection(self.db_path)
        cur = conn.cursor()
        # Verify WAL mode
        journal_mode = cur.execute("PRAGMA journal_mode;").fetchone()[0]
        self.assertEqual(journal_mode.lower(), "wal")

        # Verify tables exist
        tables = [row[0] for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()]
        self.assertIn("leases", tables)
        self.assertIn("schedules", tables)
        self.assertIn("static_ips", tables)
        self.assertIn("fleet_history", tables)
        conn.close()

    def test_leases_crud(self):
        init_db(self.db_path)
        lease_data = {
            "droplet_id": 999123,
            "droplet_name": "test-sensor-alpha",
            "sensor_user": "sensoruser",
            "ttl": "4h",
            "ttl_seconds": 14400,
            "created_at": "2026-09-21T10:00:00Z",
            "expires_at": "2026-09-21T14:00:00Z"
        }
        db_save_lease(lease_data, db_path=self.db_path)

        # Retrieve single
        retrieved = db_get_lease(999123, db_path=self.db_path)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved["droplet_name"], "test-sensor-alpha")
        self.assertEqual(retrieved["ttl"], "4h")
        self.assertEqual(retrieved["ttl_seconds"], 14400)

        # Retrieve all
        all_leases = db_get_all_leases(db_path=self.db_path)
        self.assertIn("999123", all_leases)
        self.assertEqual(all_leases["999123"]["droplet_name"], "test-sensor-alpha")

        # Delete
        deleted = db_delete_lease(999123, db_path=self.db_path)
        self.assertTrue(deleted)
        self.assertIsNone(db_get_lease(999123, db_path=self.db_path))

    def test_schedules_crud(self):
        init_db(self.db_path)
        sched_data = {
            "id": "sched-test-1",
            "name": "Daily Nightly Cowrie",
            "preset_id": "daily_nightly",
            "sensor_type": "cowrie",
            "description": "Deploys cowrie at night",
            "enabled": True,
            "timing": {"type": "daily", "start_time": "22:00", "duration": "4h"},
            "config": {"region": "nyc1", "size": "s-2vcpu-4gb"},
            "state": {"status": "scheduled", "total_runs": 3}
        }
        db_save_schedule(sched_data, db_path=self.db_path)

        # Retrieve single
        retrieved = db_get_schedule("sched-test-1", db_path=self.db_path)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved["name"], "Daily Nightly Cowrie")
        self.assertTrue(retrieved["enabled"])
        self.assertEqual(retrieved["timing"]["start_time"], "22:00")
        self.assertEqual(retrieved["config"]["region"], "nyc1")
        self.assertEqual(retrieved["state"]["total_runs"], 3)

        # Update state
        db_update_schedule_state("sched-test-1", {"status": "running", "total_runs": 4}, db_path=self.db_path)
        retrieved2 = db_get_schedule("sched-test-1", db_path=self.db_path)
        self.assertEqual(retrieved2["state"]["status"], "running")
        self.assertEqual(retrieved2["state"]["total_runs"], 4)

        # Retrieve all
        all_scheds = db_get_all_schedules(db_path=self.db_path)
        self.assertIn("sched-test-1", all_scheds)

        # Delete
        deleted = db_delete_schedule("sched-test-1", db_path=self.db_path)
        self.assertTrue(deleted)
        self.assertIsNone(db_get_schedule("sched-test-1", db_path=self.db_path))

    def test_static_ips_crud(self):
        init_db(self.db_path)
        db_add_static_ip("192.168.10.50", comment="Office Gateway", db_path=self.db_path)
        db_add_static_ip("10.0.0.1", comment="Internal DNS", db_path=self.db_path)

        ips = db_get_static_ips(db_path=self.db_path)
        ip_list = [item["ip"] for item in ips]
        self.assertIn("192.168.10.50", ip_list)
        self.assertIn("10.0.0.1", ip_list)

        # Remove
        removed = db_remove_static_ip("192.168.10.50", db_path=self.db_path)
        self.assertTrue(removed)

        ips_after = db_get_static_ips(db_path=self.db_path)
        ip_list_after = [item["ip"] for item in ips_after]
        self.assertNotIn("192.168.10.50", ip_list_after)
        self.assertIn("10.0.0.1", ip_list_after)

    def test_fleet_history_tracking(self):
        init_db(self.db_path)
        db_record_sensor_launch(
            droplet_id=888123,
            name="sensor-beta-01",
            sensor_type="dionaea",
            provider="digitalocean",
            public_ip="157.230.1.2",
            db_path=self.db_path
        )

        history = db_get_fleet_history(limit=10, db_path=self.db_path)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["droplet_id"], 888123)
        self.assertEqual(history[0]["name"], "sensor-beta-01")
        self.assertIsNone(history[0]["destroyed_at"])

        # Mark destroyed
        db_record_sensor_destroy(
            droplet_id=888123,
            reason="TTL Expired",
            db_path=self.db_path
        )

        history2 = db_get_fleet_history(limit=10, db_path=self.db_path)
        self.assertEqual(len(history2), 1)
        self.assertIsNotNone(history2[0]["destroyed_at"])
        self.assertEqual(history2[0]["reason"], "TTL Expired")

    def test_ttl_manager_with_sqlite(self):
        mgr = TTLManager(db_path=self.db_path)

        # Schedule lease
        lease = mgr.schedule_lease(
            droplet_id=777001,
            droplet_name="sensor-gamma",
            ttl_text="1h",
            token="dummy-token"
        )
        self.assertEqual(lease["droplet_id"], 777001)

        # Check in SQLite directly
        db_lease = db_get_lease(777001, db_path=self.db_path)
        self.assertIsNotNone(db_lease)
        self.assertEqual(db_lease["droplet_name"], "sensor-gamma")

        # Cancel lease
        ok = mgr.cancel_lease(777001)
        self.assertTrue(ok)
        self.assertIsNone(db_get_lease(777001, db_path=self.db_path))

    def test_parse_duration_rejects_absurd_values(self):
        from app.ttl_manager import parse_duration
        from datetime import datetime, timedelta, timezone
        self.assertEqual(parse_duration("2h"), 7200)
        self.assertEqual(parse_duration(""), 0)
        for bad in ("not-a-duration", "-5h", "5x", "5", "h5"):
            with self.assertRaises(ValueError):
                parse_duration(bad)
        # A value that parses fine as a number, but would overflow datetime + timedelta(seconds=...) if
        # ever added to "now" (found live: a campaign only hit this once it tried to go active, after
        # already spending several real minutes creating and configuring a droplet).
        with self.assertRaises(ValueError):
            parse_duration("99999999d")
        # The cap itself must stay usable with real datetime arithmetic, with headroom to spare.
        datetime.now(timezone.utc) + timedelta(seconds=parse_duration("3650d"))

    def test_edl_manager_with_sqlite(self):
        edl_file = self.dir_path / "sensors.txt"
        mgr = EDLManager(edl_path=edl_file, db_path=self.db_path)

        # Add static IP via EDLManager
        mgr.add_static_ip("198.51.100.25", comment="Test Subnet")
        static_entries = mgr._load_static_ips()
        ip_list = [e["ip"] for e in static_entries]
        self.assertIn("198.51.100.25", ip_list)

        # Check SQLite directly
        db_ips = [r["ip"] for r in db_get_static_ips(db_path=self.db_path)]
        self.assertIn("198.51.100.25", db_ips)

        # Remove static IP
        mgr.remove_static_ip("198.51.100.25")
        static_after = [e["ip"] for e in mgr._load_static_ips()]
        self.assertNotIn("198.51.100.25", static_after)

    def test_scheduler_manager_with_sqlite(self):
        mgr = SchedulerManager(db_path=self.db_path)

        # Create schedule
        payload = {
            "name": "SQLite Test Schedule",
            "sensor_type": "cowrie",
            "timing": {"type": "interval", "every": "6h"},
            "config": {"sensor_type": "cowrie", "region": "lon1"}
        }
        sched = mgr.create_schedule(payload)
        sched_id = sched["id"]

        # Verify in SQLite
        db_sched = db_get_schedule(sched_id, db_path=self.db_path)
        self.assertIsNotNone(db_sched)
        self.assertEqual(db_sched["name"], "SQLite Test Schedule")

        # Delete schedule
        mgr.delete_schedule(sched_id)
        self.assertIsNone(db_get_schedule(sched_id, db_path=self.db_path))


    def test_settings_roundtrip(self):
        from app import config
        from app.db import db_get_settings

        cfg = config.save_config({"region": "sfo3", "hive_port": 1234, "bogus": "ignored"})
        self.assertEqual(cfg["region"], "sfo3")
        stored = db_get_settings()
        self.assertEqual(stored["region"], "sfo3")
        self.assertNotIn("bogus", stored)
        with self.assertRaises(Exception):
            config.save_config({"hive_port": "not-a-number"})

    def test_do_token_only_from_secrets_file(self):
        import os
        from unittest import mock
        from app import config
        from app.db import db_get_settings, db_save_settings
        from app.models import SettingsPayload

        token_file = config.DO_TOKEN_PATH
        self.addCleanup(lambda: token_file.unlink(missing_ok=True))
        token_file.unlink(missing_ok=True)

        # The environment is no longer a source (env vars show up in `docker inspect`).
        with mock.patch.dict(os.environ, {"DO_TOKEN": "env-token", "DIGITALOCEAN_TOKEN": "env-token"}):
            self.assertEqual(config.get_do_token(), "")
            self.assertEqual(config.load_config()["do_token"], "")

        token_file.write_text("  file-token\n")
        self.assertEqual(config.get_do_token(), "file-token")  # re-read per call, whitespace stripped
        cfg = config.load_config()
        self.assertEqual(cfg["do_token"], "file-token")
        public = config.public_settings(cfg)
        self.assertNotIn("do_token", public)
        self.assertTrue(public["do_token_set"])

        # It can't be set through the settings API or persisted by save_config.
        self.assertNotIn("do_token", SettingsPayload.model_fields)
        config.save_config({"do_token": "ui-token", "region": "ams3"})
        self.assertNotIn("do_token", db_get_settings())
        self.assertEqual(config.get_do_token(), "file-token")

        # A token an older version stored in the DB is ignored, then purged at startup.
        db_save_settings({"do_token": "old-db-token"})
        self.assertEqual(config.load_config()["do_token"], "file-token")
        config.purge_stored_do_token()
        self.assertNotIn("do_token", db_get_settings())

    def test_sensor_model_saved_to_db(self):
        from app.db import db_save_active_droplet, db_get_active_droplet
        from app.models import Sensor, SensorStatus
        sensor = Sensor(id=424242, name="m1", public_ip="203.0.113.9", tags=["a"], status=SensorStatus.PROVISIONING.value)
        db_save_active_droplet(sensor, db_path=self.db_path)
        row = db_get_active_droplet(424242, db_path=self.db_path)
        self.assertEqual(row["status"], "provisioning")
        self.assertEqual(row["public_ip"], "203.0.113.9")

    def test_edl_publishes_provisioning_but_not_failed_sensors(self):
        from app.db import db_save_active_droplet
        from app.models import Sensor
        edl_file = self.dir_path / "sensors2.txt"
        mgr = EDLManager(edl_path=edl_file, db_path=self.db_path)
        for sid, ip, status in [(9001, "203.0.113.1", "active"), (9002, "203.0.113.2", "provisioning"),
                                (9003, "203.0.113.3", "provision_failed")]:
            db_save_active_droplet(Sensor(id=sid, name=f"s{sid}", public_ip=ip, status=status), db_path=self.db_path)
        ips = {e["ip"] for e in mgr.get_all_entries(force_refresh=True)}
        self.assertIn("203.0.113.1", ips)
        self.assertIn("203.0.113.2", ips)   # so the firewall can admit it while it is still being configured
        self.assertNotIn("203.0.113.3", ips)

    def test_edit_static_entry(self):
        from app.edl_manager import EDLManager
        mgr = EDLManager(edl_path=self.dir_path / "s3.txt", db_path=self.db_path)
        self.assertTrue(mgr.add_static_ip("198.51.100.1", "old")[0])
        self.assertTrue(mgr.add_static_ip("198.51.100.2")[0])
        self.assertTrue(mgr.update_static_ip("198.51.100.1", "198.51.100.9", "new")[0])
        by_ip = {e["ip"]: e for e in mgr._load_static_ips()}
        self.assertEqual(set(by_ip), {"198.51.100.2", "198.51.100.9"})
        self.assertEqual(by_ip["198.51.100.9"]["comment"], "new")
        self.assertFalse(mgr.update_static_ip("198.51.100.9", "198.51.100.2")[0])   # duplicate
        self.assertFalse(mgr.update_static_ip("198.51.100.9", "not-an-ip")[0])
        self.assertFalse(mgr.update_static_ip("192.0.2.77", "192.0.2.78")[0])        # unknown entry

    def test_active_droplets_and_leases_default_to_digitalocean_provider(self):
        from app.db import db_save_active_droplet, db_get_active_droplet
        from app.models import Sensor
        init_db(self.db_path)
        db_save_active_droplet(Sensor(id=555001, name="do-sensor"), db_path=self.db_path)
        row = db_get_active_droplet(555001, db_path=self.db_path)
        self.assertEqual(row["provider"], "digitalocean")

        db_save_lease({"droplet_id": 555002, "droplet_name": "do-lease", "ttl": "1h",
                       "ttl_seconds": 3600, "created_at": "x", "expires_at": "y"}, db_path=self.db_path)
        lease = db_get_lease(555002, db_path=self.db_path)
        self.assertEqual(lease["provider"], "digitalocean")
        self.assertIsNone(lease["region"])

    def test_gcp_active_droplet_and_lease_roundtrip(self):
        """A GCP row: provider='gcp', and `region` carries the zone (not a DO region slug)."""
        from app.db import db_save_active_droplet, db_get_active_droplet, db_get_all_active_droplets
        from app.models import Sensor
        init_db(self.db_path)
        db_save_active_droplet(
            Sensor(id=666001, name="gcp-sensor", provider="gcp", region="us-central1-a", size="e2-small"),
            db_path=self.db_path,
        )
        row = db_get_active_droplet(666001, db_path=self.db_path)
        self.assertEqual(row["provider"], "gcp")
        self.assertEqual(row["region"], "us-central1-a")
        all_rows = {r["id"]: r for r in db_get_all_active_droplets(db_path=self.db_path)}
        self.assertEqual(all_rows[666001]["provider"], "gcp")

        db_save_lease(
            {"droplet_id": 666001, "droplet_name": "gcp-sensor", "ttl": "2h", "ttl_seconds": 7200,
             "created_at": "x", "expires_at": "y", "provider": "gcp", "region": "us-central1-a"},
            db_path=self.db_path,
        )
        lease = db_get_lease(666001, db_path=self.db_path)
        self.assertEqual(lease["provider"], "gcp")
        self.assertEqual(lease["region"], "us-central1-a")

    def test_schema_migration_adds_provider_columns_to_existing_db(self):
        """init_db must stay safe to re-run against a database created by an older schema version
        (before `provider` existed on active_droplets/leases) - it should backfill the column, not
        error, and existing rows keep the default rather than losing data."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            CREATE TABLE active_droplets (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, public_ip TEXT, private_ip TEXT,
                sensor_type TEXT NOT NULL DEFAULT 'cowrie', sensor_user TEXT, region TEXT, size TEXT,
                image TEXT, tags_json TEXT DEFAULT '[]', status TEXT DEFAULT 'active', ttl TEXT,
                ttl_seconds INTEGER, created_at TEXT NOT NULL, expires_at TEXT, data_json TEXT DEFAULT '{}'
            );
        """)
        conn.execute("""
            CREATE TABLE leases (
                droplet_id INTEGER PRIMARY KEY, droplet_name TEXT NOT NULL, sensor_user TEXT,
                ttl TEXT NOT NULL, ttl_seconds INTEGER NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL
            );
        """)
        conn.execute(
            "INSERT INTO active_droplets (id, name, created_at) VALUES (111, 'pre-existing', 'x')"
        )
        conn.commit()
        conn.close()

        init_db(self.db_path)  # must not raise, and must backfill the new columns

        row = db_get_active_droplet(111, db_path=self.db_path)
        self.assertEqual(row["name"], "pre-existing")  # old data survives the migration
        self.assertEqual(row["provider"], "digitalocean")

if __name__ == "__main__":
    unittest.main()
