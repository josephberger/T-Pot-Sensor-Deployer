"""
GCP Compute Engine client. Mirrors do_client.py's shape: create/destroy/list machines and list
options only. Everything after a machine exists is cloud-agnostic (SSH, Ansible, health checks) -
see CLAUDE.md's design rule. No startup script, no cloud-init: the instance is created bare, with
only an SSH public key in its metadata, and Ansible (playbooks/) does the rest - exactly like DO.

Auth: Application Default Credentials, which google.auth.default() resolves from
GOOGLE_APPLICATION_CREDENTIALS (set to /secrets/gcp-sa.json in docker-compose.yml).

One real asymmetry vs DO: GCP's instances().get/delete/reset/stop/start all address an instance by
its *name*, not its numeric id (unlike DO, where the numeric droplet id is enough for everything).
The numeric `id` GCP also hands back is still what we use as the active_droplets primary key (for
consistency with the DO rows already keyed that way), but every live API call below needs (name, zone).
"""

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from google.api_core.exceptions import GoogleAPIError, NotFound
from google.cloud import compute_v1

from app.config import gcp_sa_key_path

GCP_SSH_USER = "tpotadmin"  # fixed, not user-configurable - same as DO's implicit "root"
# A dedicated custom-mode VPC + one subnet per region, created on demand (ensure_sensor_network) and
# never assumed to exist. Many real projects - including the one this was first deployed against -
# don't have GCP's auto-created "default" network at all (deleted, or an org policy blocks it), so
# depending on it made every deploy fail with a preflight error before creating anything. A dedicated
# network also keeps sensors off whatever else lives in the project's default network. Mirrors the old
# Terraform module's default (create_network = true).
NETWORK = "tpot-sensor-vpc"
SUBNET_CIDR = "10.10.0.0/24"
SENSOR_TAG = "tpot-sensor"
CREATE_INSTANCE_TIMEOUT = 300
DEFAULT_DISK_SIZE_GB = 30


class GCPAPIError(Exception):
    """Raised for any failure talking to the GCP Compute API."""
    def __init__(self, message: str):
        super().__init__(message)


# Project-scoped in-memory cache, same role as do_client.py's _DO_CACHE.
_GCP_CACHE: Dict[str, Dict[str, Tuple[float, Any]]] = {}


def _get_cache(project_id: str, key: str, ttl: float) -> Optional[Any]:
    now = time.time()
    proj_cache = _GCP_CACHE.get(project_id, {})
    if key in proj_cache:
        ts, val = proj_cache[key]
        if now - ts < ttl:
            return val
    return None


def _set_cache(project_id: str, key: str, val: Any):
    _GCP_CACHE.setdefault(project_id, {})[key] = (time.time(), val)


def _wait_for_operation(operation, verbose_name: str = "operation", timeout: float = CREATE_INSTANCE_TIMEOUT):
    """Block for a GCP extended (zone/global) operation to finish, raising on error."""
    try:
        result = operation.result(timeout=timeout)
    except Exception as e:
        raise GCPAPIError(f"GCP {verbose_name} failed: {e}") from e
    if getattr(operation, "error_code", None):
        raise GCPAPIError(f"GCP {verbose_name} failed ({operation.error_code}): {operation.error_message}")
    return result


def _instance_ips(instance: "compute_v1.Instance") -> Tuple[Optional[str], Optional[str]]:
    public_ip, private_ip = None, None
    for iface in instance.network_interfaces or []:
        private_ip = private_ip or iface.network_i_p
        for ac in iface.access_configs or []:
            if ac.nat_i_p:
                public_ip = ac.nat_i_p
    return public_ip, private_ip


def _instance_to_dict(instance: "compute_v1.Instance", zone: str) -> dict:
    public_ip, private_ip = _instance_ips(instance)
    return {
        "id": int(instance.id),
        "name": instance.name,
        "zone": zone,
        "status": instance.status,
        "public_ip": public_ip,
        "private_ip": private_ip,
        "machine_type": instance.machine_type.rsplit("/", 1)[-1] if instance.machine_type else None,
        "tags": list(instance.tags.items) if instance.tags else [],
        "self_link": instance.self_link,
    }


def _zone_from_url(url: str) -> str:
    # aggregatedList's zone key looks like "zones/us-central1-a"
    return url.rsplit("/", 1)[-1]


def region_from_zone(zone: str) -> str:
    """'us-central1-a' -> 'us-central1' (a zone is always its region plus one letter suffix)."""
    return zone.rsplit("-", 1)[0]


def _subnet_name(region: str) -> str:
    return f"{NETWORK}-{region}"


class GCPClient:
    """Wrapper around the GCP Compute Engine API (google-cloud-compute)."""

    def __init__(self, project_id: str):
        self.project_id = (project_id or "").strip()
        self._instances: Optional[compute_v1.InstancesClient] = None
        self._firewalls: Optional[compute_v1.FirewallsClient] = None
        self._networks: Optional[compute_v1.NetworksClient] = None
        self._subnetworks: Optional[compute_v1.SubnetworksClient] = None
        self._zones: Optional[compute_v1.ZonesClient] = None
        self._machine_types: Optional[compute_v1.MachineTypesClient] = None
        self._images: Optional[compute_v1.ImagesClient] = None

    def is_configured(self) -> bool:
        return bool(self.project_id) and gcp_sa_key_path().exists()

    # Clients are created lazily (and only once credentials/project are known to be present) so that
    # constructing a GCPClient never itself fails when GCP simply isn't configured yet.
    @property
    def instances(self) -> compute_v1.InstancesClient:
        if self._instances is None:
            self._instances = compute_v1.InstancesClient()
        return self._instances

    @property
    def firewalls(self) -> compute_v1.FirewallsClient:
        if self._firewalls is None:
            self._firewalls = compute_v1.FirewallsClient()
        return self._firewalls

    @property
    def networks(self) -> compute_v1.NetworksClient:
        if self._networks is None:
            self._networks = compute_v1.NetworksClient()
        return self._networks

    @property
    def subnetworks(self) -> compute_v1.SubnetworksClient:
        if self._subnetworks is None:
            self._subnetworks = compute_v1.SubnetworksClient()
        return self._subnetworks

    @property
    def zones(self) -> compute_v1.ZonesClient:
        if self._zones is None:
            self._zones = compute_v1.ZonesClient()
        return self._zones

    @property
    def machine_types(self) -> compute_v1.MachineTypesClient:
        if self._machine_types is None:
            self._machine_types = compute_v1.MachineTypesClient()
        return self._machine_types

    @property
    def images(self) -> compute_v1.ImagesClient:
        if self._images is None:
            self._images = compute_v1.ImagesClient()
        return self._images

    def _call(self, fn, verbose_name: str):
        try:
            return fn()
        except NotFound:
            raise
        except GoogleAPIError as e:
            raise GCPAPIError(f"GCP API error during {verbose_name}: {e}") from e

    # ------------------------------------------------------------------
    # Preflight / dedicated network
    # ------------------------------------------------------------------
    def ensure_network(self) -> None:
        """Idempotent get-or-create of just the dedicated VPC itself (no subnet - firewall rules are
        network-scoped, not region-scoped, so this is all ensure_sensor_firewall needs)."""
        try:
            self.networks.get(project=self.project_id, network=NETWORK)
        except NotFound:
            operation = self.networks.insert(
                project=self.project_id,
                network_resource=compute_v1.Network(name=NETWORK, auto_create_subnetworks=False),
            )
            _wait_for_operation(operation, f"network '{NETWORK}' creation", timeout=60)

    def ensure_sensor_network(self, region: str, subnet_cidr: str = SUBNET_CIDR) -> None:
        """Idempotent get-or-create of the dedicated VPC and one subnet in `region`. Called before
        every instance create; cheap (a couple of GET calls) once both exist."""
        self.ensure_network()

        subnet = _subnet_name(region)
        try:
            self.subnetworks.get(project=self.project_id, region=region, subnetwork=subnet)
        except NotFound:
            operation = self.subnetworks.insert(
                project=self.project_id,
                region=region,
                subnetwork_resource=compute_v1.Subnetwork(
                    name=subnet, network=f"global/networks/{NETWORK}", ip_cidr_range=subnet_cidr,
                ),
            )
            _wait_for_operation(operation, f"subnet '{subnet}' creation", timeout=60)

    def destroy_sensor_network(self) -> None:
        """Manual teardown (not called automatically on any single sensor's destroy - the network and
        its subnets are shared infrastructure for the whole GCP sensor fleet, not per-instance). Deletes
        every subnet under the dedicated network, then the network itself. Safe to call when nothing
        depends on it any more; the caller (the admin API route) is what checks for that."""
        def _list_subnets():
            # No server-side filter (GCP's filter query syntax isn't worth relying on unverified here);
            # the network-name check inside the loop below does the real filtering.
            request = compute_v1.AggregatedListSubnetworksRequest(project=self.project_id)
            return list(self.subnetworks.aggregated_list(request=request))

        for scope, scoped_list in self._call(_list_subnets, "listing subnets"):
            for sn in (scoped_list.subnetworks or []):
                if sn.network.rsplit("/", 1)[-1] != NETWORK:
                    continue
                region = sn.region.rsplit("/", 1)[-1]
                try:
                    op = self.subnetworks.delete(project=self.project_id, region=region, subnetwork=sn.name)
                    _wait_for_operation(op, f"subnet '{sn.name}' deletion", timeout=60)
                except NotFound:
                    pass

        try:
            op = self.networks.delete(project=self.project_id, network=NETWORK)
            _wait_for_operation(op, f"network '{NETWORK}' deletion", timeout=60)
        except NotFound:
            pass

    def test_connection(self) -> Dict[str, Any]:
        """Cheap connectivity/credentials check, analogous to DOClient.test_connection()."""
        cached = _get_cache(self.project_id, "test_connection", 60.0)
        if cached is not None:
            return cached
        zones = self.list_zones()
        res = {"valid": True, "project_id": self.project_id, "zone_count": len(zones)}
        _set_cache(self.project_id, "test_connection", res)
        return res

    # ------------------------------------------------------------------
    # Instances
    # ------------------------------------------------------------------
    def create_instance(
        self,
        name: str,
        zone: str,
        machine_type: str,
        image: str,
        ssh_pubkey: str,
        network_tags: Optional[List[str]] = None,
        disk_size_gb: int = DEFAULT_DISK_SIZE_GB,
        wait_active: bool = True,
        on_poll: Optional[Callable[[dict], None]] = None,
    ) -> dict:
        """Create a bare instance: no startup script, no user-data. Ansible configures it afterwards."""
        region = region_from_zone(zone)
        self.ensure_sensor_network(region)
        tags = sorted(set(network_tags or [SENSOR_TAG]) | {SENSOR_TAG})

        disk = compute_v1.AttachedDisk(
            auto_delete=True,
            boot=True,
            initialize_params=compute_v1.AttachedDiskInitializeParams(
                source_image=image, disk_size_gb=disk_size_gb,
            ),
        )
        network_interface = compute_v1.NetworkInterface(
            network=f"global/networks/{NETWORK}",
            subnetwork=f"regions/{region}/subnetworks/{_subnet_name(region)}",
            access_configs=[compute_v1.AccessConfig(name="External NAT", type_="ONE_TO_ONE_NAT")],
        )
        metadata = compute_v1.Metadata(items=[
            compute_v1.Items(key="ssh-keys", value=f"{GCP_SSH_USER}:{ssh_pubkey}"),
            compute_v1.Items(key="enable-oslogin", value="FALSE"),
        ])
        instance = compute_v1.Instance(
            name=name,
            machine_type=f"zones/{zone}/machineTypes/{machine_type}",
            disks=[disk],
            network_interfaces=[network_interface],
            metadata=metadata,
            tags=compute_v1.Tags(items=tags),
            labels={"role": "tpot-sensor", "managed_by": "tpot-sensor-deployer"},
        )

        def _insert():
            return self.instances.insert(project=self.project_id, zone=zone, instance_resource=instance)

        operation = self._call(_insert, f"creating instance '{name}'")
        _wait_for_operation(operation, f"instance '{name}' creation", timeout=CREATE_INSTANCE_TIMEOUT)

        if on_poll:
            on_poll({"status": "creating", "name": name})

        result = self.get_instance(name, zone)
        if wait_active and not result.get("public_ip"):
            # The insert operation normally finishes with the ephemeral IP already assigned; this is
            # just a short safety-net poll, not the multi-minute wait DO sometimes needs.
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and not result.get("public_ip"):
                time.sleep(3)
                result = self.get_instance(name, zone)
                if on_poll:
                    on_poll(result)
        return result

    def get_instance(self, name: str, zone: str) -> dict:
        def _get():
            return self.instances.get(project=self.project_id, zone=zone, instance=name)
        instance = self._call(_get, f"getting instance '{name}'")
        return _instance_to_dict(instance, zone)

    def destroy_instance(self, name: str, zone: str) -> None:
        def _delete():
            return self.instances.delete(project=self.project_id, zone=zone, instance=name)
        try:
            operation = self._call(_delete, f"deleting instance '{name}'")
        except NotFound:
            return  # already gone - destroy is idempotent, same expectation as DOClient.destroy_droplet
        _wait_for_operation(operation, f"instance '{name}' deletion", timeout=CREATE_INSTANCE_TIMEOUT)

    def instance_action(self, name: str, zone: str, action_type: str) -> None:
        """action_type: 'reboot' -> reset, 'power_off' -> stop, 'power_on' -> start."""
        method = {"reboot": "reset", "power_off": "stop", "power_on": "start"}.get(action_type)
        if not method:
            raise GCPAPIError(f"Unsupported action '{action_type}'")
        fn = getattr(self.instances, method)

        def _act():
            return fn(project=self.project_id, zone=zone, instance=name)

        operation = self._call(_act, f"{action_type} on instance '{name}'")
        _wait_for_operation(operation, f"instance '{name}' {action_type}", timeout=120)

    def list_instances(self) -> List[dict]:
        """Project-wide instance list, shaped like DOClient.list_droplets()."""
        cached = _get_cache(self.project_id, "instances", 5.0)
        if cached is not None:
            return [dict(d) for d in cached]

        def _list():
            request = compute_v1.AggregatedListInstancesRequest(project=self.project_id)
            return list(self.instances.aggregated_list(request=request))

        pairs = self._call(_list, "listing instances")
        results: List[dict] = []
        for scope, scoped_list in pairs:
            if not scoped_list.instances:
                continue
            zone = _zone_from_url(scope) if scope.startswith("zones/") else scope
            for instance in scoped_list.instances:
                results.append(_instance_to_dict(instance, zone))
        _set_cache(self.project_id, "instances", results)
        return results

    # ------------------------------------------------------------------
    # Options: zones, machine types, images
    # ------------------------------------------------------------------
    def list_zones(self) -> List[dict]:
        cached = _get_cache(self.project_id, "zones", 600.0)
        if cached is not None:
            return cached

        def _list():
            return list(self.zones.list(project=self.project_id))

        zones = self._call(_list, "listing zones")
        res = [
            {"name": z.name, "region": z.region.rsplit("/", 1)[-1], "status": z.status}
            for z in zones if z.status == "UP"
        ]
        _set_cache(self.project_id, "zones", res)
        return res

    def list_machine_types(self, zone: str) -> List[dict]:
        cache_key = f"machine_types_{zone}"
        cached = _get_cache(self.project_id, cache_key, 600.0)
        if cached is not None:
            return cached

        def _list():
            return list(self.machine_types.list(project=self.project_id, zone=zone))

        types = self._call(_list, f"listing machine types in {zone}")
        res = sorted(
            (
                {
                    "name": t.name,
                    "description": t.description,
                    "vcpus": t.guest_cpus,
                    "memory_mb": t.memory_mb,
                }
                for t in types
                if t.name.startswith(("e2-", "n2-", "n1-"))  # curated: skip GPU/specialty families
            ),
            key=lambda x: (x["vcpus"], x["memory_mb"]),
        )
        _set_cache(self.project_id, cache_key, res)
        return res

    def list_images(self) -> List[dict]:
        """Curated Ubuntu 24.04 image reference from the public ubuntu-os-cloud project."""
        cached = _get_cache(self.project_id, "images", 600.0)
        if cached is not None:
            return cached
        res = [
            {
                "name": "ubuntu-2404-lts-amd64",
                "self_link": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
                "description": "Ubuntu 24.04 LTS",
            }
        ]
        _set_cache(self.project_id, "images", res)
        return res

    # ------------------------------------------------------------------
    # Firewall
    # ------------------------------------------------------------------
    def ensure_sensor_firewall(self, hive_ip: Optional[str] = None, admin_ssh_port: int = 64295,
                                restrict_ssh: bool = True) -> None:
        """
        Idempotent create-or-update of the sensor firewall rules on the dedicated network, targeted at
        instances tagged SENSOR_TAG. Same rule shape as terraform/gcp/main.tf's five firewall resources:
        admin SSH restricted to the Hive, all other TCP/UDP open, ICMP open, egress open.
        """
        self.ensure_network()  # firewall rules need the network to exist first; the region-specific
                                # subnet (ensure_sensor_network) is created later, in create_instance
        admin_source = [f"{hive_ip}/32"] if (restrict_ssh and hive_ip) else ["0.0.0.0/0"]
        rules = [
            (
                f"{SENSOR_TAG}-allow-admin-ssh", "INGRESS",
                [compute_v1.Allowed(I_p_protocol="tcp", ports=[str(admin_ssh_port)])],
                admin_source, None,
            ),
            (
                f"{SENSOR_TAG}-allow-tcp", "INGRESS",
                [compute_v1.Allowed(
                    I_p_protocol="tcp",
                    ports=[f"1-{admin_ssh_port - 1}", f"{admin_ssh_port + 1}-65535"],
                )],
                ["0.0.0.0/0"], None,
            ),
            (
                f"{SENSOR_TAG}-allow-udp", "INGRESS",
                [compute_v1.Allowed(I_p_protocol="udp", ports=["1-65535"])],
                ["0.0.0.0/0"], None,
            ),
            (
                f"{SENSOR_TAG}-allow-icmp", "INGRESS",
                [compute_v1.Allowed(I_p_protocol="icmp")],
                ["0.0.0.0/0"], None,
            ),
            (
                f"{SENSOR_TAG}-allow-egress", "EGRESS",
                [compute_v1.Allowed(I_p_protocol="all")],
                None, ["0.0.0.0/0"],
            ),
        ]
        for fw_name, direction, allowed, source_ranges, dest_ranges in rules:
            self._ensure_firewall_rule(fw_name, direction, allowed, source_ranges, dest_ranges)

    def _ensure_firewall_rule(self, name: str, direction: str, allowed, source_ranges, dest_ranges) -> None:
        firewall = compute_v1.Firewall(
            name=name,
            network=f"global/networks/{NETWORK}",
            direction=direction,
            allowed=allowed,
            target_tags=[SENSOR_TAG],
        )
        if source_ranges:
            firewall.source_ranges = source_ranges
        if dest_ranges:
            firewall.destination_ranges = dest_ranges

        try:
            self.firewalls.get(project=self.project_id, firewall=name)
            operation = self.firewalls.update(project=self.project_id, firewall=name, firewall_resource=firewall)
        except NotFound:
            operation = self.firewalls.insert(project=self.project_id, firewall_resource=firewall)
        _wait_for_operation(operation, f"firewall rule '{name}'", timeout=60)
