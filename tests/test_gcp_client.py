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

import unittest
from pathlib import Path

from google.cloud import compute_v1

from app.gcp_client import GCPClient, GCP_SSH_USER, NETWORK, _instance_to_dict, _zone_from_url, region_from_zone, _subnet_name
from app import config as app_config


def _fake_instance(id_=123, name="tpot-cowrie-1", public_ip="203.0.113.5", private_ip="10.0.0.5",
                    machine_type="e2-small", status="RUNNING", tags=None):
    return compute_v1.Instance(
        id=id_,
        name=name,
        status=status,
        machine_type=f"zones/us-central1-a/machineTypes/{machine_type}",
        tags=compute_v1.Tags(items=tags or ["tpot-sensor"]),
        network_interfaces=[
            compute_v1.NetworkInterface(
                network_i_p=private_ip,
                access_configs=[compute_v1.AccessConfig(nat_i_p=public_ip)],
            )
        ],
    )


class TestGCPClientConfiguration(unittest.TestCase):
    """No real network calls here (no live GCP credentials to test against in this environment) -
    just that a misconfigured/unconfigured client fails cleanly instead of crashing, and that the
    request/response shaping helpers do the right thing given fake SDK objects."""

    def test_is_configured_false_without_project_id(self):
        client = GCPClient(project_id="")
        self.assertFalse(client.is_configured())

    def test_is_configured_false_without_key_file(self):
        # SECRETS_DIR is a fresh temp dir (see header) with no gcp-sa.json in it.
        self.assertFalse(app_config.gcp_credentials_available())
        client = GCPClient(project_id="some-real-project")
        self.assertFalse(client.is_configured())

    def test_is_configured_true_once_key_file_and_project_present(self):
        key_path = app_config.gcp_sa_key_path()
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text("{}", encoding="utf-8")
        try:
            self.assertTrue(app_config.gcp_credentials_available())
            client = GCPClient(project_id="some-real-project")
            self.assertTrue(client.is_configured())
        finally:
            key_path.unlink(missing_ok=True)

    def test_constructing_client_does_not_touch_the_network(self):
        # Lazy client properties: building a GCPClient (e.g. for an is_configured() check on a page
        # load) must never itself try to authenticate or make a request.
        client = GCPClient(project_id="unconfigured-project")
        self.assertIsNone(client._instances)
        self.assertIsNone(client._firewalls)


class TestGCPResponseShaping(unittest.TestCase):
    def test_instance_to_dict_extracts_public_and_private_ip(self):
        inst = _fake_instance(id_=42, public_ip="203.0.113.9", private_ip="10.0.0.9")
        d = _instance_to_dict(inst, zone="us-central1-a")
        self.assertEqual(d["id"], 42)
        self.assertEqual(d["public_ip"], "203.0.113.9")
        self.assertEqual(d["private_ip"], "10.0.0.9")
        self.assertEqual(d["zone"], "us-central1-a")
        self.assertEqual(d["machine_type"], "e2-small")
        self.assertIn("tpot-sensor", d["tags"])

    def test_instance_to_dict_handles_no_external_ip_yet(self):
        inst = compute_v1.Instance(
            id=7, name="pending", status="PROVISIONING",
            network_interfaces=[compute_v1.NetworkInterface(network_i_p="10.0.0.7", access_configs=[])],
        )
        d = _instance_to_dict(inst, zone="us-central1-a")
        self.assertIsNone(d["public_ip"])
        self.assertEqual(d["private_ip"], "10.0.0.7")

    def test_zone_from_aggregated_list_scope_key(self):
        self.assertEqual(_zone_from_url("zones/us-central1-a"), "us-central1-a")
        self.assertEqual(_zone_from_url("projects/p/zones/europe-west1-b"), "europe-west1-b")


class TestGCPInstanceAction(unittest.TestCase):
    def test_unsupported_action_raises_cleanly(self):
        from app.gcp_client import GCPAPIError
        client = GCPClient(project_id="demo")
        with self.assertRaises(GCPAPIError):
            client.instance_action("some-instance", "us-central1-a", "not_a_real_action")


class TestGCPDedicatedNetwork(unittest.TestCase):
    """Sensors get a dedicated tpot-sensor-vpc network, never the project's "default" one (which
    plenty of real projects don't have - found live, the first real deploy attempt failed exactly
    this way). These are pure helper/shape checks; no live network calls."""

    def test_region_from_zone(self):
        self.assertEqual(region_from_zone("us-central1-a"), "us-central1")
        self.assertEqual(region_from_zone("europe-west1-b"), "europe-west1")
        # A region name that itself contains a digit-letter-like suffix must still split on the last hyphen.
        self.assertEqual(region_from_zone("asia-southeast1-a"), "asia-southeast1")

    def test_subnet_name_is_dedicated_network_scoped(self):
        name = _subnet_name("us-central1")
        self.assertTrue(name.startswith(NETWORK))
        self.assertIn("us-central1", name)

    def test_network_constant_is_not_the_gcp_default(self):
        # Regression guard: create_instance/ensure_sensor_firewall must never fall back to "default".
        self.assertNotEqual(NETWORK, "default")


class TestGCPDeployFieldResolution(unittest.TestCase):
    """Regression coverage for a bug found live: DeployPayload.region/size/image default to
    DigitalOcean-shaped values, and the GCP deploy form doesn't send `image` at all - so Pydantic
    filled in DO's "ubuntu-24-04-x64" and it went straight to the GCP API as a disk image reference,
    failing with 'malformed URL'. resolve_gcp_deploy_field must treat a DO-default value as unset."""

    def setUp(self):
        from app.jobs import resolve_gcp_deploy_field
        self.resolve = resolve_gcp_deploy_field

    def test_do_default_image_falls_back_to_gcp_default(self):
        # Exactly what a real /api/deploy POST looks like from the GCP tab: no "image" key at all, so
        # Pydantic (DeployPayload.image: str = "ubuntu-24-04-x64") fills in DO's default.
        payload = {"provider": "gcp", "region": "us-central1-a", "size": "e2-small", "image": "ubuntu-24-04-x64"}
        cfg = {"gcp_image": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64"}
        result = self.resolve(payload, cfg, "image", "gcp_image", "hard-default-image")
        self.assertEqual(result, cfg["gcp_image"])
        self.assertNotEqual(result, "ubuntu-24-04-x64")

    def test_real_gcp_value_passes_through_unchanged(self):
        payload = {"region": "europe-west1-b"}
        result = self.resolve(payload, {}, "region", "gcp_zone", "us-central1-a")
        self.assertEqual(result, "europe-west1-b")

    def test_missing_field_and_missing_config_uses_hard_default(self):
        result = self.resolve({}, {}, "size", "gcp_machine_type", "e2-small")
        self.assertEqual(result, "e2-small")

    def test_do_default_region_and_size_also_fall_back(self):
        # Same bug class could hit region/size too if a future caller ever omits them; confirm both,
        # not just the one field that happened to have no matching GCP UI element.
        payload = {"region": "nyc1", "size": "s-1vcpu-2gb"}
        cfg = {"gcp_zone": "us-east1-b", "gcp_machine_type": "e2-medium"}
        self.assertEqual(self.resolve(payload, cfg, "region", "gcp_zone", "x"), "us-east1-b")
        self.assertEqual(self.resolve(payload, cfg, "size", "gcp_machine_type", "x"), "e2-medium")


if __name__ == "__main__":
    unittest.main()
