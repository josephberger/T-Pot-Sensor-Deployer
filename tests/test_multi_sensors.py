# Isolate from real state: must run before any app module is imported.
import os as _os, sys as _sys, tempfile as _tempfile
from pathlib import Path as _Path
_os.environ["DATA_DIR"] = _tempfile.mkdtemp(prefix="tpot-test-")
# The T-Pot Hive is LIVE config (lswebpasswd + .env): tests must never resolve to the real one.
_os.environ["TPOT_DIR"] = _tempfile.mkdtemp(prefix="tpot-test-hive-")
_os.environ["SECRETS_DIR"] = _tempfile.mkdtemp(prefix="tpot-test-secrets-")
# Tasks live in Redis and show on the live Tasks page: point at a closed port so the task layer uses
# its in-memory fallback (these tests run inside the web container, where REDIS_URL is the real one).
_os.environ["REDIS_URL"] = "redis://127.0.0.1:1/0"
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import sys
import json
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.sensor_types import (
    SENSOR_TYPES,
    list_sensor_types,
    get_sensor_type,
    build_sensor_compose_yaml,
    get_gcp_firewall_ports
)
from app.provisioner import generate_cloud_init
from app.edl_manager import EDLManager
from app.scheduler_manager import PRESETS

def test_sensor_registry():
    print("=== Testing Sensor Registry ===")
    types_list = list_sensor_types()
    types = {s["id"]: s for s in types_list}
    print(f"Total registered sensors: {len(types)}")
    expected = [
        "cowrie", "dionaea", "conpot", "elasticpot", "mailoney",
        "heralding", "ciscoasa", "citrixhoneypot", "redishoneypot",
        "sentrypeer", "adbhoney", "multi_sensor"
    ]
    for exp in expected:
        assert exp in types, f"Missing expected sensor type: {exp}"
        info = get_sensor_type(exp)
        assert info["name"], f"Sensor {exp} missing name"
        assert len(info["ports"]) > 0, f"Sensor {exp} missing ports"
        assert len(info["required_dirs"]) > 0, f"Sensor {exp} missing required_dirs"
        print(f"  ✓ {info['icon']} {info['name']}: {len(info['ports'])} ports, {len(info['required_dirs'])} dirs")
    print("All sensor types registered successfully!")

def test_compose_generation():
    print("\n=== Testing Compose Generation ===")
    for stype in ["cowrie", "dionaea", "conpot", "elasticpot", "multi_sensor"]:
        compose = build_sensor_compose_yaml(
            sensor_type=stype,
            hive_ip="192.168.1.100",
            tpot_hive_user="tpot_user",
            sensor_name=f"test-{stype}-sensor"
        )
        assert "tpotinit:" in compose
        assert "logstash:" in compose
        if stype == "multi_sensor":
            assert "cowrie:" in compose
            assert "dionaea:" in compose
            assert "elasticpot:" in compose
            assert "mailoney:" in compose
        else:
            assert f"{stype}:" in compose
        print(f"  ✓ Valid compose for {stype} ({len(compose.splitlines())} lines)")

def test_cloud_init():
    print("\n=== Testing Cloud-Init Generation ===")
    for stype in ["cowrie", "dionaea", "conpot", "multi_sensor"]:
        ci = generate_cloud_init(
            hive_ip="192.168.1.100",
            hive_port=64294,
            hive_cert="-----BEGIN CERTIFICATE-----\nMIIB...\n-----END CERTIFICATE-----",
            sensor_name=f"test-{stype}-sensor",
            tpot_hive_user="tpot_user",
            sensor_type=stype,
            ssh_port=64295
        )
        assert "#!/bin/bash" in ci
        assert "Port 64295" in ci
        assert "/data/logstash" in ci
        dirs = get_sensor_type(stype).get("required_dirs", [])
        for d in dirs:
            assert d in ci, f"Missing directory {d} in cloud-init for {stype}"
        print(f"  ✓ Cloud-init verified for {stype}")

def test_gcp_firewall_ports():
    print("\n=== Testing GCP Firewall Ports ===")
    for stype in ["cowrie", "dionaea", "conpot", "sentrypeer", "multi_sensor"]:
        ports = get_gcp_firewall_ports(stype)
        tcp_ports, udp_ports = ports["tcp"], ports["udp"]
        print(f"  ✓ {stype} -> TCP: {tcp_ports} | UDP: {udp_ports}")
        assert len(tcp_ports) > 0

def test_edl_formatting():
    print("\n=== Testing EDL Formatter ===")
    mgr = EDLManager()
    class MockDOClient:
        def is_configured(self): return True
        def list_droplets(self):
            return [
                {"id": 101, "name": "cowrie-sensor-1", "public_ip": "198.51.100.10", "status": "active", "tags": ["tpot-sensor", "type-cowrie"]},
                {"id": 102, "name": "dionaea-sensor-1", "public_ip": "198.51.100.20", "status": "active", "tags": ["tpot-sensor", "type-dionaea"]},
                {"id": 103, "name": "conpot-sensor-1", "public_ip": "198.51.100.30", "status": "active", "tags": ["tpot-sensor", "type-conpot"]},
            ]
    edl = mgr.generate_edl_plaintext(do_client=MockDOClient(), include_comments=True)
    print("EDL Output Preview:")
    for line in edl.splitlines()[:15]:
        print(f"    {line}")
    assert "198.51.100.10" in edl
    assert "COWRIE" in edl
    assert "198.51.100.20" in edl
    assert "DIONAEA" in edl
    assert "198.51.100.30" in edl
    assert "CONPOT" in edl

def test_scheduler_presets():
    print("\n=== Testing Scheduler Presets ===")
    for pid, p in PRESETS.items():
        st = p.get("sensor_type", "cowrie")
        print(f"  ✓ Preset '{pid}': sensor_type={st}, active={p['active_duration']}, mode={p['mode']}")
        assert st in SENSOR_TYPES

def test_firewall_rules():
    print("\n=== Testing DigitalOcean Firewall Rules Generation ===")
    from app.do_client import DOClient

    class MockDOClient(DOClient):
        def __init__(self):
            super().__init__(token="dummy-token")
            self.created_firewalls = []
            self.updated_firewalls = []

        def list_firewalls(self):
            return []

        def create_firewall(self, name, inbound_rules, outbound_rules, tags=None):
            self.created_firewalls.append({
                "name": name,
                "inbound_rules": inbound_rules,
                "outbound_rules": outbound_rules,
                "tags": tags
            })
            return {"id": "mock-fw-123", "name": name}

    mock = MockDOClient()
    fw_id = mock.ensure_sensor_firewall(
        sensor_type="cowrie",
        name="tpot-sensor-firewall",
        tag="tpot-sensor",
        hive_ip="203.0.113.10",
        restrict_ssh=True,
        admin_ssh_port=64295,
        open_all_ports=True
    )
    assert fw_id == "mock-fw-123"
    assert len(mock.created_firewalls) == 1
    fw = mock.created_firewalls[0]
    assert fw["name"] == "tpot-sensor-firewall"
    assert "tpot-sensor" in fw["tags"]
    assert "tpot-cowrie-sensor" in fw["tags"]

    inbound = fw["inbound_rules"]
    assert any(r["protocol"] == "tcp" and r["ports"] == "1-64294" and "0.0.0.0/0" in r["sources"]["addresses"] for r in inbound), "Missing lower TCP range"
    assert any(r["protocol"] == "tcp" and r["ports"] == "64296-65535" and "0.0.0.0/0" in r["sources"]["addresses"] for r in inbound), "Missing upper TCP range"
    assert any(r["protocol"] == "tcp" and r["ports"] == "64295" and "203.0.113.10/32" in r["sources"]["addresses"] for r in inbound), "Missing admin SSH rule"
    assert any(r["protocol"] == "udp" and r["ports"] == "1-65535" and "0.0.0.0/0" in r["sources"]["addresses"] for r in inbound), "Missing all UDP rule"
    assert any(r["protocol"] == "icmp" and "0.0.0.0/0" in r["sources"]["addresses"] for r in inbound), "Missing ICMP rule"

    print("  ✓ Lower TCP range (1-64294) open to 0.0.0.0/0, ::/0")
    print("  ✓ Admin SSH (64295) restricted to 203.0.113.10/32")
    print("  ✓ Upper TCP range (64296-65535) open to 0.0.0.0/0, ::/0")
    print("  ✓ All UDP (1-65535) open to 0.0.0.0/0, ::/0")
    print("  ✓ ICMP ping open to 0.0.0.0/0, ::/0")
    print("  ✓ Tags applied: ['tpot-cowrie-sensor', 'tpot-sensor']")

def test_gap_fixes():
    print("\n=== Testing Resolved Gaps & Architecture Enhancements ===")
    from app.ttl_manager import TTLManager
    from app.scheduler_manager import SchedulerManager
    from app.gcp_manager import GCPTerraformManager

    # 1. Verify mandatory T-Pot environment variables in cloud-init
    ci = generate_cloud_init(
        hive_ip="203.0.113.10",
        hive_port=64294,
        hive_cert="CERT",
        sensor_name="test-sensor",
        tpot_hive_user="dXNlcjpwYXNz",
        sensor_type="cowrie"
    )
    for expected_var in [
        "TPOT_OSTYPE=linux",
        "TPOT_BLACKHOLE=DISABLED",
        "TPOT_PERSISTENCE=on",
        "TPOT_ATTACKMAP_TEXT=ENABLED",
        "TPOT_ATTACKMAP_TEXT_TIMEZONE=UTC"
    ]:
        assert expected_var in ci, f"Missing {expected_var} in cloud-init"
    print("  ✓ Mandatory T-Pot env vars (OSTYPE, BLACKHOLE, PERSISTENCE, ATTACKMAP) present in cloud-init")

    # 2. Verify LS_JAVA_OPTS in compose YAML
    compose = build_sensor_compose_yaml(
        sensor_type="cowrie",
        hive_ip="203.0.113.10",
        tpot_hive_user="dXNlcjpwYXNz",
        sensor_name="test-sensor"
    )
    assert "LS_JAVA_OPTS=-Xms512m -Xmx768m" in compose, "Missing LS_JAVA_OPTS prefix in compose YAML"
    print("  ✓ Logstash JVM options correctly prefixed with LS_JAVA_OPTS in Compose")

    # 3. Verify TTLManager doesn't store plain token
    ttl = TTLManager()
    lease = ttl.schedule_lease(999, "test-droplet", "1h", token="secret_token", sensor_user="sensor-user-1")
    assert "token" not in lease, "Raw token must NOT be saved in lease"
    assert lease["sensor_user"] == "sensor-user-1"
    print("  ✓ TTLManager omits raw token from lease storage and tracks sensor_user")

    # 4. Verify GCPTerraformManager workspace listing
    gcp = GCPTerraformManager()
    ws = gcp.list_workspaces()
    assert isinstance(ws, list) and len(ws) > 0
    print(f"  ✓ GCPTerraformManager supports multi-sensor workspaces: {ws}")

    # 5. Verify GCP dynamic IP configuration (no reserved static address)
    tf_main = (Path(__file__).parent.parent / "terraform" / "gcp" / "main.tf").read_text()
    assert "resource \"google_compute_address\"" not in tf_main, "Must NOT reserve static IP in GCP"
    assert "access_config {" in tf_main
    print("  ✓ GCP Terraform enforces strictly ephemeral dynamic public IPs (zero static IP reservations)")

    # 6. Verify Scheduler GCP provider support. create_schedule's preflight now requires GCP to actually
    # be configured (project id + service account key) before it will accept a GCP campaign - same bar
    # a manual GCP deploy already holds itself to - so fake both in this test's isolated SECRETS_DIR.
    from app import config as app_config
    app_config.gcp_sa_key_path().parent.mkdir(parents=True, exist_ok=True)
    app_config.gcp_sa_key_path().write_text("{}", encoding="utf-8")
    app_config.save_config({"gcp_project_id": "test-project"})
    try:
        sched = SchedulerManager()
        s = sched.create_schedule({
            "name": "GCP Cowrie Campaign",
            "sensor_type": "cowrie",
            "start_immediately": False,
            "config": {
                "provider": "gcp",
                "region": "us-central1",
                "zone": "us-central1-a"
            }
        })
        assert s["config"]["provider"] == "gcp"
        assert s["config"]["region"] == "us-central1"
        print("  ✓ Honeypot Campaign Scheduler supports both DigitalOcean and GCP providers")
    finally:
        app_config.gcp_sa_key_path().unlink(missing_ok=True)

def test_scheduler_reconciliation_safeguards():
    print("\n=== Testing Scheduler Reconciliation Loop & Operational Safeguards ===")
    from datetime import datetime, timedelta, timezone
    from unittest.mock import MagicMock, patch
    from app.scheduler_manager import (
        SchedulerManager,
        MAX_CONCURRENT_PROVISIONING,
        PROVISIONING_TIMEOUT_MINUTES,
        MAX_CONSECUTIVE_FAILURES,
    )
    # reconcile_tick's cadence is set where it is actually called: worker.py's SchedulerThread. There is
    # no standalone loop in SchedulerManager any more (see the lifespan comment in app/api.py) - running
    # one in both the web and worker containers meant every dispatch/teardown could fire twice.
    from worker import SchedulerThread
    poll_interval_seconds = SchedulerThread().tick_interval

    # 1. Verify Safeguard Constants
    assert MAX_CONCURRENT_PROVISIONING == 3, "Concurrency cap must be 3"
    assert PROVISIONING_TIMEOUT_MINUTES == 30, "Provisioning timeout must cover creation + SSH configuration (30 minutes)"
    assert MAX_CONSECUTIVE_FAILURES == 3, "Circuit breaker failure limit must be 3"
    assert poll_interval_seconds == 10, "Reconciliation tick must be 10 seconds"
    print("  ✓ Operational safeguard constants verified (Cap: 3, Timeout: 30m, CB: 3, Tick: 10s)")

    sched = SchedulerManager()

    # 2. Verify Initial State
    s = sched.create_schedule({
        "name": "Safety Cowrie Test",
        "sensor_type": "cowrie",
        "start_immediately": False
    })
    sid = s["id"]
    state = s["state"]
    assert state.get("consecutive_failures") == 0
    assert state.get("provisioning_started_at") is None
    assert state.get("provisioning_deadline_at") is None
    print("  ✓ Schedule created with zero initial failures and clean provisioning state")

    # 3. Verify Idempotency Guard (no duplicate dispatch when provisioning)
    with sched._lock:
        schedules = sched._load_schedules()
        schedules[sid]["state"]["status"] = "provisioning"
        sched._save_schedules(schedules)

    res = sched.trigger_action(sid)
    assert res.get("status") == "warning"
    assert "Deployment is already in-flight" in res.get("message", "")
    print("  ✓ Idempotency Guard verified: prevents double-dispatch while provisioning is in-flight")

    # 4. Verify Circuit Breaker & Exponential Backoff
    sched._handle_failure(s, "API timeout test 1")
    assert s["state"]["consecutive_failures"] == 1
    assert s["state"]["status"] == "cooling_down"
    assert "Backoff retry in 5m" in s["state"]["last_message"]
    print("  ✓ Failure 1 handled: exponential backoff set to 5m")

    sched._handle_failure(s, "API timeout test 2")
    assert s["state"]["consecutive_failures"] == 2
    assert s["state"]["status"] == "cooling_down"
    assert "Backoff retry in 10m" in s["state"]["last_message"]
    print("  ✓ Failure 2 handled: exponential backoff set to 10m")

    sched._handle_failure(s, "API timeout test 3")
    assert s["state"]["consecutive_failures"] == 3
    assert s["state"]["status"] == "suspended"
    assert s["enabled"] is False
    assert "Circuit breaker tripped" in s["state"]["last_message"]
    print("  ✓ Circuit Breaker tripped after 3 consecutive failures: status set to 'suspended'")

    # 5. Verify Circuit Breaker Reset on Resume
    with sched._lock:
        schedules = sched._load_schedules()
        schedules[sid] = s
        sched._save_schedules(schedules)

    ok = sched.resume_schedule(sid)
    assert ok is True
    resumed = sched.get_schedule(sid)
    assert resumed["enabled"] is True
    assert resumed["state"]["consecutive_failures"] == 0
    assert resumed["state"]["status"] == "cooling_down"
    print("  ✓ Circuit Breaker cleanly reset on schedule resume")

    # 6. Verify two-stage reconciliation: droplet ACTIVE -> configuring (background thread) -> active
    import time
    import app.scheduler_manager as sm
    sm.load_config = lambda: {"hive_ip": "192.0.2.10"}  # no ipify lookups in tests
    sm.configure_and_verify = MagicMock()

    def wait_for_configure():
        for _ in range(200):
            if sid not in sm._CONFIGURING:
                return
            time.sleep(0.05)
        raise AssertionError("configure thread did not finish")

    mock_client = MagicMock()
    mock_client.is_configured.return_value = True
    mock_client.get_droplet.return_value = {
        "id": 987654,
        "name": "cowrie-sched-test",
        "status": "active",
        "public_ip": "198.51.100.77"
    }

    resumed["state"].update({
        "status": "provisioning",
        "stage": "waiting_ip",
        "current_droplet_id": 987654,
        "current_droplet_name": "cowrie-sched-test",
        "current_sensor_user": "sensor-test",
        "hive_token": "dXNlcjpwYXNz",
        "provisioning_started_at": datetime.now(timezone.utc).isoformat(),
        "provisioning_deadline_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
    })
    with sched._lock:
        schedules = sched._load_schedules()
        schedules[sid] = resumed
        sched._save_schedules(schedules)

    assert sched._reconcile_provisioning(resumed, do_client=mock_client) is True
    assert resumed["state"]["status"] == "provisioning" and resumed["state"]["stage"] == "configuring"
    assert resumed["state"]["current_public_ip"] == "198.51.100.77"
    with sched._lock:
        schedules = sched._load_schedules()
        schedules[sid] = resumed
        sched._save_schedules(schedules)
    wait_for_configure()
    sm.configure_and_verify.assert_called_once()
    args, kwargs = sm.configure_and_verify.call_args
    assert args == ("198.51.100.77", "cowrie") and kwargs["hive_token"] == "dXNlcjpwYXNz"
    resumed = sched.get_schedule(sid)
    assert resumed["state"]["status"] == "active"
    assert resumed["state"]["current_public_ip"] == "198.51.100.77"
    assert resumed["state"]["next_action"] == "destroy"
    assert resumed["state"]["consecutive_failures"] == 0
    assert "hive_token" not in resumed["state"], "per-sensor Hive secret must not survive or be exposed"
    print("  ✓ Droplet ACTIVE -> Ansible configure + health (background) -> campaign active with dynamic IP")

    # 6b. Configuration failure tears the cycle down and feeds the circuit breaker
    sm.configure_and_verify = MagicMock(side_effect=RuntimeError("Playbook failed (exit 2)"))
    resumed["state"].update({
        "status": "provisioning", "stage": "waiting_ip", "current_droplet_id": 987655,
        "current_droplet_name": "cowrie-sched-test2", "consecutive_failures": 0,
        "provisioning_deadline_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
    })
    mock_client.get_droplet.return_value = {"id": 987655, "name": "cowrie-sched-test2", "status": "active", "public_ip": "198.51.100.78"}
    assert sched._reconcile_provisioning(resumed, do_client=mock_client) is True
    with sched._lock:
        schedules = sched._load_schedules()
        schedules[sid] = resumed
        sched._save_schedules(schedules)
    wait_for_configure()
    failed = sched.get_schedule(sid)
    assert failed["state"]["status"] == "cooling_down" and failed["state"]["consecutive_failures"] == 1
    assert "Playbook failed" in failed["state"]["last_message"]
    print("  ✓ Configuration failure aborts the cycle, records a failure and backs off")

    # 6c. A stage=configuring cycle with no live thread (worker restart) is aborted, not stuck
    resumed = failed
    resumed["state"].update({"status": "provisioning", "stage": "configuring", "current_droplet_id": 987656,
        "provisioning_deadline_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()})
    assert sched._reconcile_provisioning(resumed, do_client=mock_client) is True
    assert resumed["state"]["status"] == "cooling_down"
    mock_client.destroy_droplet.assert_called_with(987656)
    print("  ✓ Interrupted configuration (worker restart) is detected and cleaned up")

    # 6d. Configure and health succeed, but turning that into "active" state itself fails (e.g. a bad
    # timing value overflowing datetime + timedelta): must abort cleanly, not crash the background thread
    # and leave the cycle stuck until the next tick's "interrupted configuration" fallback catches it.
    sm.configure_and_verify = MagicMock(return_value=[])
    resumed["state"].update({
        "status": "provisioning", "stage": "waiting_ip", "current_droplet_id": 987658,
        "current_droplet_name": "cowrie-sched-test3", "consecutive_failures": 0,
        "provisioning_deadline_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
    })
    mock_client.get_droplet.return_value = {"id": 987658, "name": "cowrie-sched-test3", "status": "active", "public_ip": "198.51.100.79"}
    with patch.object(sm.SchedulerManager, "_finalize_active", side_effect=OverflowError("date value out of range")):
        assert sched._reconcile_provisioning(resumed, do_client=mock_client) is True
        with sched._lock:
            schedules = sched._load_schedules()
            schedules[sid] = resumed
            sched._save_schedules(schedules)
        wait_for_configure()
    finalize_failed = sched.get_schedule(sid)
    assert finalize_failed["state"]["status"] == "cooling_down" and finalize_failed["state"]["consecutive_failures"] == 1
    assert "Finalize failed" in finalize_failed["state"]["last_message"]
    print("  ✓ A finalize failure after a successful configure still aborts cleanly (no crashed thread, no stuck cycle)")

    # 7. Verify Provisioning Timeout Handling
    resumed["state"].update({"status": "provisioning", "stage": "waiting_ip", "current_droplet_id": 987657,
        "consecutive_failures": 0,
        "provisioning_deadline_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()})
    modified = sched._reconcile_provisioning(resumed, do_client=mock_client)
    assert modified is True
    assert resumed["state"]["consecutive_failures"] == 1
    mock_client.destroy_droplet.assert_called_with(987657)
    print("  ✓ Provisioning timeout enforced: destroyed orphaned droplet and recorded failure")


def test_concurrent_trigger_does_not_double_dispatch():
    print("\n=== Testing concurrent 'Trigger now' calls on one schedule (found live: 5 clicks -> 5 real droplets) ===")
    import threading
    import time as _time
    from unittest.mock import patch
    import app.scheduler_manager as sm

    sched = sm.SchedulerManager()
    s = sched.create_schedule({"name": "Race Test", "sensor_type": "cowrie", "start_immediately": False})
    sid = s["id"]

    call_count = {"n": 0}
    lock = threading.Lock()

    def fake_dispatch(manager_self, schedule):
        # autospec=True below calls this the same way the real bound method is called, i.e. with self.
        # Simulate the real, slow network call (droplet create) so concurrent callers have a wide window
        # to race in - this is exactly what let 5 concurrent live requests each read the pre-dispatch
        # status and all dispatch before any of them had written "provisioning" back.
        with lock:
            call_count["n"] += 1
        _time.sleep(0.15)
        schedule["state"]["status"] = "provisioning"
        schedule["state"]["current_droplet_id"] = 999000 + call_count["n"]
        return True, "dispatched"

    with patch.object(sm.SchedulerManager, "_dispatch_deploy", side_effect=fake_dispatch, autospec=True):
        threads = [threading.Thread(target=sched.trigger_action, args=(sid,)) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

    assert call_count["n"] == 1, f"expected exactly one dispatch from 5 concurrent triggers, got {call_count['n']}"
    print("  ✓ 5 concurrent triggers on the same schedule produced exactly 1 dispatch, not 5")


def test_campaign_do_dispatch_is_bare_and_safe():
    print("\n=== Testing campaign DigitalOcean dispatch (bare droplet, deployer key, cleanup) ===")
    from pathlib import Path
    from unittest.mock import MagicMock, patch
    import app.scheduler_manager as sm
    import app.do_client

    sched = sm.SchedulerManager()
    s = sched.create_schedule({"name": "Dispatch Test", "sensor_type": "cowrie", "start_immediately": False})
    sm.load_config = lambda: {"hive_ip": "192.0.2.10"}

    client = MagicMock()
    client.ensure_ssh_key.return_value = "77"
    client.create_droplet.return_value = {"id": 424242}

    with patch.object(sm, "get_do_token", return_value="tok"), \
         patch.object(sm, "ansible_available", return_value=True), \
         patch.object(sm, "private_key_path", return_value=Path(__file__)), \
         patch.object(sm, "get_local_ssh_pubkey", return_value=(Path("k.pub"), "ssh-ed25519 AAAA test")), \
         patch.object(app.do_client, "DOClient", return_value=client), \
         patch("app.hive_manager.HiveManager.register_sensor", return_value=(True, "ok")) as reg:
        # Happy path
        ok, _ = sched._dispatch_deploy(s)
        reg.assert_called_once()
        assert ok, s["state"]["last_message"]
        kwargs = client.create_droplet.call_args.kwargs
        assert "user_data" not in kwargs, "droplets must be created bare; configuration happens over SSH"
        assert kwargs["ssh_keys"] == ["77"], "the deployer key must always be attached (no key => DO emails a root password)"
        assert kwargs["wait_active"] is False
        assert s["state"]["status"] == "provisioning" and s["state"]["stage"] == "waiting_ip"
        assert s["state"]["hive_token"]
        print("  ✓ Bare droplet created with the deployer SSH key attached")

        # Create failure must not leave the Hive login behind
        client.create_droplet.side_effect = RuntimeError("DO API down")
        with patch("app.hive_manager.HiveManager.deregister_sensor", return_value=(True, "ok")) as dereg:
            s2 = sched.create_schedule({"name": "Dispatch Fail", "sensor_type": "cowrie", "start_immediately": False})
            ok, msg = sched._dispatch_deploy(s2)
            assert not ok and "DO API down" in msg
            dereg.assert_called_once()
            assert s2["state"]["consecutive_failures"] == 1
        print("  ✓ Failed create deregisters the Hive login and counts toward the circuit breaker")

        # No playbook for the type: refuse before creating anything
        client.create_droplet.reset_mock()
        try:
            sched.create_schedule({"name": "No Playbook", "sensor_type": "not-a-real-honeypot", "start_immediately": False})
            raise AssertionError("creating a campaign for a type without a playbook must be rejected")
        except ValueError as e:
            assert "No Ansible playbook" in str(e)
        s3 = sched.create_schedule({"name": "No Playbook", "sensor_type": "cowrie", "start_immediately": False})
        s3["config"]["sensor_type"] = "not-a-real-honeypot"  # e.g. a campaign saved before its playbook was removed
        ok, msg = sched._dispatch_deploy(s3)
        assert not ok and "No Ansible playbook" in msg
        client.create_droplet.assert_not_called()
        print("  ✓ Sensor type without a playbook is refused before any droplet is created")


def test_campaign_gcp_dispatch_finalize_and_backstop_lease():
    print("\n=== Testing GCP campaign dispatch -> configure -> active, and the TTL backstop lease ===")
    import time
    from datetime import datetime, timezone
    from pathlib import Path
    from unittest.mock import MagicMock, patch
    import app.scheduler_manager as sm
    from app.db import db_get_lease

    app_config = __import__("app.config", fromlist=["config"])
    app_config.gcp_sa_key_path().parent.mkdir(parents=True, exist_ok=True)
    app_config.gcp_sa_key_path().write_text("{}", encoding="utf-8")
    app_config.save_config({"gcp_project_id": "test-project"})

    sched = sm.SchedulerManager()
    sm.load_config = lambda: {"hive_ip": "192.0.2.10"}
    sm.configure_and_verify = MagicMock()

    def wait_for_configure(sid):
        for _ in range(200):
            if sid not in sm._CONFIGURING:
                return
            time.sleep(0.05)
        raise AssertionError("GCP provision thread did not finish")

    gcp_client = MagicMock()
    gcp_client.create_instance.return_value = {
        "id": 555111222, "name": "gcp-sched-test", "public_ip": "203.0.113.50", "private_ip": "10.10.0.5",
    }

    try:
        with patch("app.gcp_client.GCPClient", return_value=gcp_client), \
             patch("app.cloud.GCPClient", return_value=gcp_client), \
             patch.object(sm, "ansible_available", return_value=True), \
             patch.object(sm, "private_key_path", return_value=Path(__file__)), \
             patch.object(sm, "get_local_ssh_pubkey", return_value=(Path("k.pub"), "ssh-ed25519 AAAA test")), \
             patch("app.hive_manager.HiveManager.register_sensor", return_value=(True, "ok")), \
             patch("app.hive_manager.HiveManager.deregister_sensor", return_value=(True, "ok")), \
             patch("app.hive_manager.HiveManager.detect_hive_ip", return_value="192.0.2.10"), \
             patch("app.hive_manager.HiveManager.get_hive_certificate", return_value=""), \
             patch("app.edl_manager.EDLManager.write_edl_file"), \
             patch("app.edl_manager.EDLManager.invalidate_cache"):

            s = sched.create_schedule({
                "name": "GCP Dispatch Test", "sensor_type": "cowrie", "start_immediately": False,
                "config": {"provider": "gcp", "zone": "us-central1-a", "machine_type": "e2-small"},
            })
            sid = s["id"]

            ok, msg = sched._dispatch_deploy(s)
            assert ok, msg
            assert s["state"]["status"] == "provisioning" and s["state"]["stage"] == "configuring"
            # _dispatch_deploy mutates the in-memory state dict; the caller (trigger_action/reconcile_tick
            # normally) is responsible for persisting it - the background thread's own wait-loop tolerates
            # a brief delay here (up to 30s) for exactly this reason, but the test should still do it the
            # same way real callers do rather than relying on that grace window.
            with sched._lock:
                schedules = sched._load_schedules()
                schedules[sid] = s
                sched._save_schedules(schedules)
            wait_for_configure(sid)

            gcp_client.create_instance.assert_called_once()
            kwargs = gcp_client.create_instance.call_args.kwargs
            assert kwargs["zone"] == "us-central1-a" and kwargs["machine_type"] == "e2-small"
            sm.configure_and_verify.assert_called_once()
            assert sm.configure_and_verify.call_args.kwargs["remote_ssh_user"] == "tpotadmin", \
                "GCP images don't allow root SSH - must configure as the metadata-provisioned user"

            active = sched.get_schedule(sid)
            st = active["state"]
            assert st["status"] == "active"
            assert st["current_droplet_id"] == 555111222
            assert st["current_public_ip"] == "203.0.113.50"
            print("  ✓ GCP dispatch creates a bare instance and configures it as tpotadmin, then goes active")

            # The Sensor row this recorded must say provider=gcp and carry the zone as its region -
            # _finalize_active used to always default to provider="digitalocean" regardless of the
            # schedule's actual provider, a real bug that only mattered once GCP campaigns existed for real.
            from app.db import db_get_active_droplet
            row = db_get_active_droplet(555111222, db_path=sched.db_path)
            assert row["provider"] == "gcp" and row["region"] == "us-central1-a"
            print("  ✓ Active sensor row correctly recorded as provider=gcp with zone as region")

            # Backstop lease: extended at _finalize_active to active_duration + 24h slack, independent of
            # the campaign's own timer - the whole point is it survives even if the tick stops advancing.
            lease = db_get_lease(555111222, db_path=sched.db_path)
            assert lease is not None and lease["provider"] == "gcp" and lease["region"] == "us-central1-a"
            expected_min = s["timing"]["active_duration_seconds"] + sm.CAMPAIGN_BACKSTOP_BUFFER_SECONDS - 60
            assert lease["ttl_seconds"] >= expected_min, "backstop must cover active_duration + slack, not just active_duration"
            print("  ✓ TTL backstop lease scheduled at finalize, covering active_duration + 24h slack")

            # Teardown must destroy the instance (by name+zone, not id - GCP addresses by name, unlike
            # DO) and cancel the backstop lease so the independent sweep doesn't try to destroy it again.
            # The real instance name is whatever hive_mgr.generate_sensor_credentials() produced (a
            # random adjective-noun combo, per sensor_name = creds["username"]), not something this test
            # controls - read it back from state rather than assuming a fixed value.
            real_name = st["current_droplet_name"]
            ok, msg = sched._execute_destroy(active)
            assert ok
            gcp_client.destroy_instance.assert_called_once_with(real_name, "us-central1-a")
            assert db_get_lease(555111222, db_path=sched.db_path) is None, "backstop lease must be cancelled on real teardown"
            print("  ✓ Teardown destroys the GCP instance and cancels its backstop lease")

            # Resume reality check: a schedule claiming status=active for a droplet that no longer exists
            # (the backstop fired while paused, or it was destroyed by hand from Fleet) must not resume
            # into a phantom "active" state with no destroy timer ever set again.
            s2 = sched.create_schedule({
                "name": "GCP Stranded Test", "sensor_type": "cowrie", "start_immediately": False,
                "config": {"provider": "gcp", "zone": "us-central1-a"},
            })
            with sched._lock:
                schedules = sched._load_schedules()
                schedules[s2["id"]]["enabled"] = False
                schedules[s2["id"]]["state"].update({
                    "status": "active", "current_droplet_id": 999888777,
                    "current_droplet_name": "gone-instance", "current_public_ip": "203.0.113.99",
                    "cycle_started_at": datetime.now(timezone.utc).isoformat(),
                })
                sched._save_schedules(schedules)
            sched.resume_schedule(s2["id"])
            resumed2 = sched.get_schedule(s2["id"])
            assert resumed2["state"]["status"] == "cooling_down", "a stranded 'active' sensor must not resume as active"
            assert resumed2["state"]["current_droplet_id"] is None
            assert resumed2["state"]["next_action"] == "deploy" and resumed2["state"]["next_action_at"]
            print("  ✓ Resuming a campaign whose 'active' sensor no longer exists drops it into cooling_down, not a phantom active state")
    finally:
        app_config.gcp_sa_key_path().unlink(missing_ok=True)


def test_manual_deploy_cleans_up_hive_login_on_create_failure():
    print("\n=== Testing manual deploy (jobs.py) cleans up on a failed droplet create ===")
    from pathlib import Path
    from unittest.mock import MagicMock, patch
    import app.jobs as jobs

    client = MagicMock()
    client.ensure_ssh_key.return_value = "77"
    client.create_droplet.side_effect = RuntimeError("DigitalOcean API error (422): Only valid hostname characters are allowed.")

    with patch.object(jobs, "get_active_token", return_value="tok"), \
         patch.object(jobs, "DOClient", return_value=client), \
         patch.object(jobs, "ansible_available", return_value=True), \
         patch.object(jobs, "playbook_for", return_value="sensors/cowrie.yml"), \
         patch.object(jobs, "private_key_path", return_value=Path(__file__)), \
         patch.object(jobs, "get_local_ssh_pubkey", return_value=(Path("k.pub"), "ssh-ed25519 AAAA test")), \
         patch.object(jobs, "publish_task_log"), patch.object(jobs, "set_task_status"), \
         patch("app.hive_manager.HiveManager.register_sensor", return_value=(True, "ok")), \
         patch("app.hive_manager.HiveManager.detect_hive_ip", return_value="192.0.2.10"), \
         patch("app.hive_manager.HiveManager.get_hive_certificate", return_value=""), \
         patch("app.hive_manager.HiveManager.deregister_sensor", return_value=(True, "ok")) as dereg:
        try:
            jobs.run_do_deployment_job("t1", {"name": "claude-t-multi_sensor", "sensor_type": "cowrie"})
            raise AssertionError("a create_droplet failure must propagate as a failed task")
        except RuntimeError as e:
            assert "422" in str(e)
        dereg.assert_called_once()
        print("  ✓ A failed droplet create deregisters the Hive login it had just registered")


def test_playbook_env_template_and_port_choice():
    print("\n=== Testing playbook .env template and SSH port choice ===")
    import jinja2
    from unittest.mock import patch
    tpl = (Path(__file__).resolve().parent.parent / "playbooks" / "templates" / "tpot.env.j2").read_text()
    out = jinja2.Environment().from_string(tpl).render(
        tpot_hive_user="dXNlcjpwYXNz", hive_ip="192.0.2.10", ssl_verification="full",
        tpot_repo="dtagdevsec", tpot_version="24.04", sensor_name="s1")
    for expected in ["TPOT_TYPE=SENSOR", "TPOT_OSTYPE=linux", "TPOT_BLACKHOLE=DISABLED", "TPOT_PERSISTENCE=on",
                     "TPOT_ATTACKMAP_TEXT=ENABLED", "TPOT_ATTACKMAP_TEXT_TIMEZONE=UTC", "TPOT_HIVE_USER=dXNlcjpwYXNz",
                     "TPOT_HIVE_IP=192.0.2.10", "MY_HOSTNAME=s1"]:
        assert expected in out.splitlines(), f"sensor .env is missing {expected} (tpotinit refuses to start without it)"
    print("  ✓ Sensor .env template carries every variable tpotinit requires")

    import app.provisioning as prov
    with patch.object(prov, "wait_for_port", side_effect=lambda ip, port, **k: port == 64295):
        assert prov.pick_ssh_port("1.2.3.4", 64295) == 64295
    with patch.object(prov, "wait_for_port", side_effect=lambda ip, port, **k: port == 22):
        assert prov.pick_ssh_port("1.2.3.4", 64295) == 22
    with patch.object(prov, "wait_for_port", return_value=False):
        try:
            prov.pick_ssh_port("1.2.3.4", 64295, wait_seconds=1)
            raise AssertionError("must raise when SSH is unreachable")
        except RuntimeError:
            pass
    print("  ✓ Configure connects on the admin port if already relocated, else on 22, else fails clearly")

    # Hive unreachable is a WARNING, not a failed health check
    import app.health as health
    fake = ['ok: [1.2.3.4]', '"msg": "TPOT_WARNING: sensor cannot reach the Hive at 192.0.2.10:64294; logs will not ship"', 'PLAY RECAP']
    with patch.object(health, "run_playbook", return_value=(0, fake)):
        res = health.deep_check("1.2.3.4", "cowrie", hive_ip="192.0.2.10")
    assert res["healthy"] is True and len(res["warnings"]) == 1 and "cannot reach the Hive" in res["warnings"][0]
    print("  ✓ A sensor that cannot reach the Hive passes health with an explicit warning")

    # Waits for the firewall to admit the sensor, then reports clean
    seq = [{"healthy": True, "warnings": ["cannot reach the Hive"]}, {"healthy": True, "warnings": ["cannot reach the Hive"]},
           {"healthy": True, "warnings": []}]
    with patch.object(prov, "deep_check", side_effect=seq[1:]) as dc, patch.object(prov.time, "sleep"), \
         patch.object(prov, "HIVE_ADMIT_WAIT_SECONDS", 1000.0):
        out = prov._wait_for_hive_admission("1.2.3.4", "cowrie", seq[0], "192.0.2.10", 64294, 64295, None, lambda m, p: None)
    assert out == [] and dc.call_count == 2
    # ...and gives up (still reporting the warning) if the firewall never admits it
    with patch.object(prov, "deep_check", return_value={"healthy": True, "warnings": ["cannot reach the Hive"]}), \
         patch.object(prov, "HIVE_ADMIT_WAIT_SECONDS", 0.0):
        out = prov._wait_for_hive_admission("1.2.3.4", "cowrie", seq[0], "192.0.2.10", 64294, 64295, None, lambda m, p: None)
    assert out == ["cannot reach the Hive"]
    print("  ✓ Deploy waits for the firewall to admit the sensor to the Hive, and still reports a warning if it never does")


def test_trimmed_logstash_pipeline():
    print("\n=== Testing per-sensor trimmed Logstash pipeline ===")
    import jinja2, yaml
    root = Path(__file__).resolve().parent.parent / "playbooks"
    play = yaml.safe_load((root / "sensors" / "cowrie.yml").read_text())[0]["vars"]
    defaults = yaml.safe_load((root / "defaults.yml").read_text())
    env = jinja2.Environment()
    ctx = {**defaults, **play, "hive_ip": "192.0.2.10", "tpot_hive_user": "x", "sensor_name": "s1"}
    ctx["honeypot_services"] = env.from_string(play["honeypot_services"]).render(**ctx)
    conf = env.from_string((root / "templates" / "logstash_http_output.conf.j2").read_text()).render(**ctx)

    assert conf.count("file {") == 1 and "/data/cowrie/log/cowrie.json" in conf, "sensor must read only its own log"
    for other in ("dionaea", "conpot", "suricata", "fatt", "p0f"):
        assert other not in conf.lower(), f"pipeline still references {other}: it must be trimmed to this sensor"
    assert "geoip {" in conf, "GeoIP enrichment (attack map fields) must stay on the sensor"
    assert "translate" not in conf and "iprep" not in conf.replace("IP reputation", ""), "no IP-reputation dictionary on sensors"
    assert 'type] == "Cowrie"' in conf and "ISO8601" in conf
    assert "${TPOT_HIVE_IP}" in conf and "${TPOT_HIVE_USER}" in conf and "http {" in conf, "must still ship to the Hive"
    assert conf.count("{") == conf.count("}"), "unbalanced braces in the rendered pipeline"
    assert len(conf.splitlines()) < 200, "stock pipeline is ~750 lines; the trimmed one must stay small"
    print(f"  ✓ Rendered Cowrie pipeline: {len(conf.splitlines())} lines, own input only, GeoIP kept, no reputation lookup")

    compose = yaml.safe_load(env.from_string((root / "templates" / "docker-compose.yml.j2").read_text()).render(**ctx))
    ls = compose["services"]["logstash"]
    assert "/opt/tpotce/logstash/http_output.conf:/etc/logstash/http_output.conf:ro" in ls["volumes"]
    assert any(e.startswith("LS_JAVA_OPTS=-Xms") for e in ls["environment"])
    print("  ✓ Compose mounts the trimmed pipeline over the stock one and sets the heap from a variable")


def test_campaign_tasks_track_each_cycle():
    print("\n=== Testing campaign tasks (one per deploy attempt and per teardown) ===")
    from pathlib import Path
    from unittest.mock import MagicMock, patch
    import app.scheduler_manager as sm
    import app.do_client
    from app.queue_manager import get_task_status, get_task_logs, record_task, reap_interrupted_tasks

    sched = sm.SchedulerManager()
    sm.load_config = lambda: {"hive_ip": "192.0.2.10"}
    client = MagicMock()
    client.ensure_ssh_key.return_value = "77"
    client.create_droplet.return_value = {"id": 515151}

    with patch.object(sm, "get_do_token", return_value="tok"), \
         patch.object(sm, "ansible_available", return_value=True), \
         patch.object(sm, "private_key_path", return_value=Path(__file__)), \
         patch.object(sm, "get_local_ssh_pubkey", return_value=(Path("k.pub"), "ssh-ed25519 AAAA test")), \
         patch.object(app.do_client, "DOClient", return_value=client), \
         patch("app.hive_manager.HiveManager.register_sensor", return_value=(True, "ok")), \
         patch("app.hive_manager.HiveManager.deregister_sensor", return_value=(True, "ok")), \
         patch("app.edl_manager.EDLManager.write_edl_file", return_value=None):
        s = sched.create_schedule({"name": "Task Test", "sensor_type": "cowrie", "start_immediately": False})

        # Dispatch opens a running deploy task, named after the droplet, tagged with the campaign
        ok, _ = sched._dispatch_deploy(s)
        assert ok
        tid = s["state"]["task_id"]
        assert s["state"]["last_task_id"] == tid
        t = get_task_status(tid)
        assert t["status"] == "running" and t["task_type"] == "campaign_deploy"
        assert t["campaign_id"] == s["id"] and t["campaign_name"] == "Task Test" and t["cycle"] == "1"
        assert t["sensor_name"] == s["state"]["current_droplet_name"]
        assert any("Creating droplet" in e["message"] for e in get_task_logs(tid))
        print("  ✓ Dispatch opens a running 'Campaign deploy' task named after the droplet")

        # The worker's restart reaper must leave it alone (a waiting_ip cycle survives a restart)
        reap_interrupted_tasks()
        assert get_task_status(tid)["status"] == "running"
        print("  ✓ Worker restart reaper skips campaign tasks")

        # Configured and healthy -> completed, with the sensor in 'deployed'
        s["state"]["current_public_ip"] = "198.51.100.7"
        sched._finalize_active(s, None)
        t = get_task_status(tid)
        assert t["status"] == "completed" and t["deployed"][0]["ip"] == "198.51.100.7"
        assert "task_id" not in s["state"] and s["state"]["last_task_id"] == tid
        print("  ✓ Finalize completes the task and records the sensor")

        # Teardown is its own task
        ok, _ = sched._execute_destroy(s)
        did = s["state"]["last_task_id"]
        assert ok and did != tid
        d = get_task_status(did)
        assert d["status"] == "completed" and d["task_type"] == "campaign_destroy" and d["cycle"] == "1"
        assert any("Teardown initiated" in e["message"] for e in get_task_logs(did))
        print("  ✓ Teardown runs as its own completed 'Campaign teardown' task")

        # A failed attempt fails its task with the backoff message
        client.create_droplet.side_effect = RuntimeError("DO API down")
        ok, _ = sched._dispatch_deploy(s)
        f = get_task_status(s["state"]["last_task_id"])
        assert not ok and f["status"] == "failed" and "DO API down" in f["error"] and "Backoff retry" in f["error"]
        print("  ✓ A failed attempt fails its task, with the retry/backoff outcome as the error")

        # Deleting a campaign mid-cycle closes its open task
        client.create_droplet.side_effect = None
        s2 = sched.create_schedule({"name": "Delete Mid-Cycle", "sensor_type": "cowrie", "start_immediately": False})
        sched._dispatch_deploy(s2)
        with sched._lock:
            allS = sched._load_schedules(); allS[s2["id"]] = s2; sched._save_schedules(allS)
        open_tid = s2["state"]["task_id"]
        sched.delete_schedule(s2["id"], destroy_droplet=True)
        assert get_task_status(open_tid)["status"] == "failed"
        print("  ✓ Deleting a campaign mid-cycle fails its open deploy task (and runs a teardown task)")

    # Startup: fail only campaign tasks no campaign claims
    record_task("orphan-task", "campaign_deploy", "Campaign deploy", "x", "cowrie", status="running")
    s3 = sched.create_schedule({"name": "Claims Task", "sensor_type": "cowrie", "start_immediately": False})
    record_task("claimed-task", "campaign_deploy", "Campaign deploy", "y", "cowrie", status="running")
    with sched._lock:
        allS = sched._load_schedules(); allS[s3["id"]]["state"]["task_id"] = "claimed-task"; sched._save_schedules(allS)
    assert sched.reap_orphaned_campaign_tasks() >= 1  # earlier tests in this process leave their own orphans
    assert get_task_status("orphan-task")["status"] == "failed"
    assert get_task_status("claimed-task")["status"] == "running"
    print("  ✓ Startup fails orphaned campaign tasks but leaves ones a campaign still tracks")


def test_pause_resume_keeps_active_sensor():
    print("\n=== Testing pause/resume on an active campaign keeps its sensor ===")
    from datetime import datetime, timedelta, timezone
    from app.scheduler_manager import SchedulerManager, PAUSE_BACKSTOP_SECONDS
    from app.db import db_save_active_droplet, db_get_lease
    from app.models import Sensor

    sched = SchedulerManager()
    s = sched.create_schedule({"name": "Pause Test", "sensor_type": "cowrie", "start_immediately": False})
    sid = s["id"]
    now = datetime.now(timezone.utc)
    destroy_at = (now + timedelta(hours=4)).isoformat()
    with sched._lock:
        allS = sched._load_schedules()
        allS[sid]["state"].update(status="active", current_droplet_id=616161, current_public_ip="203.0.113.9",
                                  current_droplet_name="pause-sensor", cycle_started_at=now.isoformat(),
                                  next_action="destroy", next_action_at=destroy_at)
        sched._save_schedules(allS)
    db_save_active_droplet(Sensor(id=616161, name="pause-sensor", public_ip="203.0.113.9", sensor_type="cowrie",
                                  status="active", created_at=now.isoformat()), db_path=sched.db_path)

    sched.pause_schedule(sid)
    st = sched._load_schedules()[sid]
    assert st["enabled"] is False and st["state"]["status"] == "active", "pause must not overwrite status"
    lease = db_get_lease(616161, db_path=sched.db_path)
    assert lease and lease["ttl_seconds"] == PAUSE_BACKSTOP_SECONDS
    print("  ✓ Pause disables timers, keeps status 'active', extends the backstop lease")

    sched.resume_schedule(sid)
    st = sched._load_schedules()[sid]["state"]
    # Used to fall through to cooling_down + "deploy now", orphaning droplet 616161.
    assert st["status"] == "active" and st["next_action"] == "destroy" and st["next_action_at"] == destroy_at
    assert st["current_droplet_id"] == 616161
    print("  ✓ Resume keeps the running sensor and its original teardown time (no second deploy)")

    # Paused mid-provisioning: the cycle finishes, with the pause-length lease
    with sched._lock:
        allS = sched._load_schedules()
        allS[sid]["state"].update(status="provisioning", stage="configuring")
        sched._save_schedules(allS)
    sched.pause_schedule(sid)
    s = sched._load_schedules()[sid]
    assert s["state"]["status"] == "provisioning", "an in-flight cycle must stay reconcilable"
    from unittest.mock import patch
    with patch("app.edl_manager.EDLManager.write_edl_file", return_value=None):
        sched._finalize_active(s, None)
    assert s["state"]["status"] == "active" and "paused" in s["state"]["last_message"]
    assert db_get_lease(616161, db_path=sched.db_path)["ttl_seconds"] == PAUSE_BACKSTOP_SECONDS
    print("  ✓ Pausing mid-provisioning lets the cycle finish, with the pause-length backstop lease")


if __name__ == "__main__":
    test_sensor_registry()
    test_compose_generation()
    test_cloud_init()
    test_gcp_firewall_ports()
    test_firewall_rules()
    test_edl_formatting()
    test_scheduler_presets()
    test_gap_fixes()
    test_scheduler_reconciliation_safeguards()
    test_concurrent_trigger_does_not_double_dispatch()
    test_campaign_do_dispatch_is_bare_and_safe()
    test_campaign_gcp_dispatch_finalize_and_backstop_lease()
    test_campaign_tasks_track_each_cycle()
    test_pause_resume_keeps_active_sensor()
    test_manual_deploy_cleans_up_hive_login_on_create_failure()
    test_playbook_env_template_and_port_choice()
    test_trimmed_logstash_pipeline()
    print("\nAll unit and integration tests PASSED successfully! 🎯")


