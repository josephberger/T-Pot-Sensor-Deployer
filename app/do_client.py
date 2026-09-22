import time
import requests
from typing import Dict, List, Optional, Any, Tuple

API_BASE = "https://api.digitalocean.com/v2"
REQUEST_TIMEOUT = 30
CREATE_DROPLET_TIMEOUT = 300

DEFAULT_OUTBOUND_RULES = [
    {"protocol": "tcp", "ports": "1-65535", "destinations": {"addresses": ["0.0.0.0/0", "::/0"]}},
    {"protocol": "udp", "ports": "1-65535", "destinations": {"addresses": ["0.0.0.0/0", "::/0"]}},
    {"protocol": "icmp", "destinations": {"addresses": ["0.0.0.0/0", "::/0"]}},
]


class DOAPIError(Exception):
    """Raised for any failure talking to the DigitalOcean API."""
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


# Token-scoped in-memory cache to prevent redundant HTTP roundtrips
_DO_CACHE: Dict[str, Dict[str, Tuple[float, Any]]] = {}


def _get_do_cache(token: str, key: str, ttl: float) -> Optional[Any]:
    now = time.time()
    token_cache = _DO_CACHE.get(token, {})
    if key in token_cache:
        ts, val = token_cache[key]
        if now - ts < ttl:
            return val
    return None


def _set_do_cache(token: str, key: str, val: Any):
    now = time.time()
    if token not in _DO_CACHE:
        _DO_CACHE[token] = {}
    _DO_CACHE[token][key] = (now, val)


def _invalidate_do_cache(token: str, prefix: Optional[str] = None):
    if token in _DO_CACHE:
        if prefix:
            keys_to_del = [k for k in _DO_CACHE[token] if k.startswith(prefix)]
            for k in keys_to_del:
                del _DO_CACHE[token][k]
        else:
            _DO_CACHE[token].clear()


class DOClient:
    """Wrapper around the DigitalOcean REST API v2."""

    def __init__(self, token: str, base_url: str = API_BASE, timeout: int = REQUEST_TIMEOUT):
        self.token = token.strip() if token else ""
        self.base_url = base_url
        self.timeout = timeout
        self.session = requests.Session()
        if self.token:
            self.session.headers.update({
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            })

    def is_configured(self) -> bool:
        return bool(self.token)

    def invalidate_cache(self, prefix: Optional[str] = None):
        """Invalidate in-memory cache for this client token."""
        _invalidate_do_cache(self.token, prefix=prefix)

    def _raise_for_status(self, resp):
        if resp.status_code == 401:
            raise DOAPIError("Authentication failed: Invalid or expired DigitalOcean API token.", status_code=401)
        if not resp.ok:
            message = resp.reason
            try:
                data = resp.json()
                message = data.get("message", message)
            except Exception:
                pass
            raise DOAPIError(f"DigitalOcean API error ({resp.status_code}): {message}", status_code=resp.status_code)


    def _request(self, method: str, path: str, params: Optional[dict] = None, json: Optional[dict] = None) -> dict:
        if not self.token:
            raise DOAPIError("No DigitalOcean API token configured.")
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.request(method, url, params=params, json=json, timeout=self.timeout)
        except requests.RequestException as e:
            raise DOAPIError(f"Network error communicating with DigitalOcean API: {e}") from e

        self._raise_for_status(resp)
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    def _get_paginated(self, path: str, key: str, params: Optional[dict] = None) -> list:
        items = []
        url = f"{self.base_url}{path}"
        request_params = dict(params or {})
        request_params.setdefault("per_page", 200)

        while url:
            try:
                resp = self.session.get(url, params=request_params, timeout=self.timeout)
            except requests.RequestException as e:
                raise DOAPIError(f"Network error communicating with DigitalOcean API: {e}") from e

            self._raise_for_status(resp)
            data = resp.json() if resp.content else {}
            items.extend(data.get(key, []))

            url = data.get("links", {}).get("pages", {}).get("next")
            request_params = None

        return items

    def test_connection(self) -> Dict[str, Any]:
        """Verify token and fetch account details with 60-second caching."""
        cached = _get_do_cache(self.token, "account", 60.0)
        if cached is not None:
            return cached
        data = self._request("GET", "/account")
        account = data.get("account", {})
        res = {
            "valid": True,
            "email": account.get("email"),
            "uuid": account.get("uuid"),
            "droplet_limit": account.get("droplet_limit"),
            "status": account.get("status"),
            "status_message": account.get("status_message")
        }
        _set_do_cache(self.token, "account", res)
        return res

    def list_droplets(self, tag_name: Optional[str] = None, force_refresh: bool = False) -> List[dict]:
        """List droplets with short 5-second caching to prevent concurrent polling pileup."""
        cache_key = f"droplets_{tag_name or 'all'}"
        if not force_refresh:
            cached = _get_do_cache(self.token, cache_key, 5.0)
            if cached is not None:
                return [dict(d) for d in cached]

        params = {"tag_name": tag_name} if tag_name else None
        droplets = self._get_paginated("/droplets", "droplets", params=params)
        # Parse IPs for UI convenience
        for d in droplets:
            v4_nets = d.get("networks", {}).get("v4", [])
            pub_ip = next((net["ip_address"] for net in v4_nets if net.get("type") == "public"), None)
            priv_ip = next((net["ip_address"] for net in v4_nets if net.get("type") == "private"), None)
            d["public_ip"] = pub_ip
            d["private_ip"] = priv_ip
        _set_do_cache(self.token, cache_key, droplets)
        return droplets

    def get_droplet(self, droplet_id: int) -> dict:
        data = self._request("GET", f"/droplets/{droplet_id}")
        droplet = data.get("droplet", {})
        v4_nets = droplet.get("networks", {}).get("v4", [])
        droplet["public_ip"] = next((net["ip_address"] for net in v4_nets if net.get("type") == "public"), None)
        return droplet

    def create_droplet(
        self,
        name: str,
        region: str,
        size: str,
        image: str,
        ssh_keys: List[Any],
        tags: Optional[List[str]] = None,
        user_data: Optional[str] = None,
        vpc_uuid: Optional[str] = None,
        wait_active: bool = True,
        on_poll=None
    ) -> dict:
        actual_tags = tags or ["tpot-cowrie-sensor"]
        self.ensure_tags(actual_tags)

        body = {
            "name": name,
            "region": region,
            "size": size,
            "image": image,
            "ssh_keys": ssh_keys,
            "backups": False,
            "tags": actual_tags
        }
        if user_data:
            body["user_data"] = user_data
        if vpc_uuid:
            body["vpc_uuid"] = vpc_uuid

        data = self._request("POST", "/droplets", json=body)
        self.invalidate_cache("droplets_")
        droplet = data.get("droplet", {})
        droplet_id = droplet.get("id")

        if not wait_active:
            return droplet

        deadline = time.monotonic() + CREATE_DROPLET_TIMEOUT
        while True:
            cur = self.get_droplet(droplet_id)
            if cur.get("status") == "active" and cur.get("public_ip"):
                self.invalidate_cache("droplets_")
                return cur
            if time.monotonic() > deadline:
                raise DOAPIError(f"Timed out waiting for droplet '{name}' (ID {droplet_id}) to become active.")
            if on_poll:
                on_poll(cur)
            time.sleep(3)

    def destroy_droplet(self, droplet_id: int):
        self._request("DELETE", f"/droplets/{droplet_id}")
        self.invalidate_cache("droplets_")

    def droplet_action(self, droplet_id: int, action_type: str) -> dict:
        res = self._request("POST", f"/droplets/{droplet_id}/actions", json={"type": action_type}).get("action", {})
        self.invalidate_cache("droplets_")
        return res

    def list_regions(self) -> List[dict]:
        """Fetch available droplet regions with 10-minute caching."""
        cached = _get_do_cache(self.token, "regions", 600.0)
        if cached is not None:
            return cached
        all_regions = self._get_paginated("/regions", "regions")
        # Filter regions that are available for droplet creation
        res = [r for r in all_regions if r.get("available", False)]
        _set_do_cache(self.token, "regions", res)
        return res

    def list_sizes(self) -> List[dict]:
        """Fetch available droplet sizes with 10-minute caching."""
        cached = _get_do_cache(self.token, "sizes", 600.0)
        if cached is not None:
            return cached
        all_sizes = self._get_paginated("/sizes", "sizes")
        # Filter available standard/basic sizes
        standard = [
            s for s in all_sizes
            if s.get("available", False) and ("s-" in s.get("slug", "") or "basic" in s.get("description", "").lower())
        ]
        res = sorted(standard, key=lambda x: x.get("price_monthly", 999))
        _set_do_cache(self.token, "sizes", res)
        return res

    def list_images(self, distribution: str = "Ubuntu") -> List[dict]:
        """Fetch available standard distribution images with 10-minute caching."""
        cached = _get_do_cache(self.token, "images", 600.0)
        if cached is not None:
            return cached
        all_images = self._get_paginated("/images?type=distribution", "images")
        filtered = []
        for img in all_images:
            if img.get("status") == "available" and img.get("public", False):
                distro = img.get("distribution", "")
                name = img.get("name", "")
                if distribution.lower() in distro.lower() or "ubuntu" in name.lower():
                    filtered.append({
                        "id": img.get("id"),
                        "slug": img.get("slug") or str(img.get("id")),
                        "name": name,
                        "distribution": distro or "Ubuntu",
                        "description": img.get("description") or name,
                        "min_disk_size": img.get("min_disk_size", 0)
                    })
        filtered.sort(key=lambda x: str(x.get("name", "")), reverse=True)
        res = filtered[:15]
        _set_do_cache(self.token, "images", res)
        return res

    def list_ssh_keys(self) -> List[dict]:
        """Fetch configured SSH keys with 2-minute caching."""
        cached = _get_do_cache(self.token, "ssh_keys", 120.0)
        if cached is not None:
            return cached
        res = self._get_paginated("/account/keys", "ssh_keys")
        _set_do_cache(self.token, "ssh_keys", res)
        return res

    def create_ssh_key(self, name: str, public_key: str) -> dict:
        body = {"name": name, "public_key": public_key.strip()}
        res = self._request("POST", "/account/keys", json=body).get("ssh_key", {})
        self.invalidate_cache("ssh_keys")
        return res

    def ensure_ssh_key(self, name: str, public_key: str) -> str:
        """Find or upload an SSH public key matching the given key material."""
        clean_key = public_key.strip().split()
        if len(clean_key) >= 2:
            key_body = clean_key[1]
        else:
            key_body = public_key.strip()

        existing_keys = self.list_ssh_keys()
        for k in existing_keys:
            k_pub = k.get("public_key", "").strip()
            if key_body in k_pub:
                return str(k["id"])

        # Not found, upload it
        new_key = self.create_ssh_key(name, public_key.strip())
        return str(new_key["id"])

    # ------------------------------------------------------------------
    # Tags Management & Auto-Creation
    # ------------------------------------------------------------------
    def list_tags(self) -> List[dict]:
        """List all tags in the DigitalOcean account with 60-second caching."""
        cached = _get_do_cache(self.token, "tags", 60.0)
        if cached is not None:
            return cached
        res = self._get_paginated("/tags", "tags")
        _set_do_cache(self.token, "tags", res)
        return res

    def create_tag(self, name: str) -> dict:
        """Create a new DigitalOcean tag."""
        clean_name = str(name).strip()
        body = {"name": clean_name}
        res = self._request("POST", "/tags", json=body).get("tag", {})
        self.invalidate_cache("tags")
        return res

    def ensure_tag(self, tag_name: str) -> dict:
        """Ensure a DigitalOcean tag exists. If it does not exist, create it automatically."""
        if not tag_name or not str(tag_name).strip():
            return {}
        clean_tag = str(tag_name).strip()
        try:
            return self.create_tag(clean_tag)
        except DOAPIError as e:
            # If tag already exists (422 Unprocessable Entity or 409 Conflict), fetch or return cleanly
            if e.status_code in (422, 409) or "already" in str(e).lower() or "exist" in str(e).lower():
                try:
                    data = self._request("GET", f"/tags/{clean_tag}")
                    return data.get("tag", {"name": clean_tag})
                except Exception:
                    return {"name": clean_tag}
            raise

    def ensure_tags(self, tags: Optional[List[str]]) -> List[dict]:
        """Ensure that all provided DigitalOcean tags exist, auto-creating any that are missing."""
        if not tags:
            return []
        ensured = []
        seen = set()
        for t in tags:
            if t and str(t).strip() and str(t).strip() not in seen:
                clean = str(t).strip()
                seen.add(clean)
                try:
                    ensured.append(self.ensure_tag(clean))
                except Exception as e:
                    # Log warning but do not crash the deployment flow if tag creation gives an unexpected non-fatal response
                    pass
        return ensured

    def list_firewalls(self) -> List[dict]:
        return self._get_paginated("/firewalls", "firewalls")

    def create_firewall(self, name: str, inbound_rules: list, outbound_rules: list, tags: Optional[List[str]] = None) -> dict:
        body = {
            "name": name,
            "inbound_rules": inbound_rules,
            "outbound_rules": outbound_rules,
        }
        if tags:
            self.ensure_tags(tags)
            body["tags"] = tags
        return self._request("POST", "/firewalls", json=body).get("firewall", {})

    def update_firewall(
        self,
        firewall_id: str,
        name: str,
        inbound_rules: list,
        outbound_rules: list,
        tags: Optional[List[str]] = None,
        droplet_ids: Optional[List[int]] = None
    ) -> dict:
        """Update an existing DigitalOcean Cloud Firewall."""
        body = {
            "name": name,
            "inbound_rules": inbound_rules,
            "outbound_rules": outbound_rules,
        }
        if tags is not None:
            self.ensure_tags(tags)
            body["tags"] = tags
        if droplet_ids is not None:
            body["droplet_ids"] = droplet_ids
        return self._request("PUT", f"/firewalls/{firewall_id}", json=body).get("firewall", {})

    def ensure_sensor_firewall(
        self,
        sensor_type: str = "cowrie",
        name: Optional[str] = None,
        tag: Optional[str] = None,
        hive_ip: Optional[str] = None,
        restrict_ssh: bool = True,
        admin_ssh_port: int = 64295,
        open_all_ports: bool = True
    ) -> str:
        """Create or locate the dedicated honeypot firewall for the fleet.

        Rules:
        - All TCP ports (1-64294 & 64296-65535) open from everywhere (0.0.0.0/0, ::/0).
        - Alternate admin SSH port (default 64295) open ONLY from Hive IP (<hive_ip>/32).
        - All UDP ports (1-65535) open from everywhere.
        - ICMP open from everywhere.
        - Outbound all traffic permitted.
        - Attached to tags: 'tpot-sensor', 'tpot-cowrie-sensor' (auto-applied to all sensor droplets).
        """
        fw_name = name or "tpot-sensor-firewall"
        fw_tags = sorted(list(set(filter(None, ["tpot-sensor", "tpot-cowrie-sensor", tag or f"tpot-{sensor_type}-sensor"]))))
        self.ensure_tags(fw_tags)

        admin_ssh_source = {"addresses": [f"{hive_ip}/32"]} if (restrict_ssh and hive_ip) else {"addresses": ["0.0.0.0/0", "::/0"]}

        if open_all_ports:
            inbound_rules = [
                # 1. Lower TCP port range (1 to admin_ssh_port - 1)
                {
                    "protocol": "tcp",
                    "ports": f"1-{admin_ssh_port - 1}",
                    "sources": {"addresses": ["0.0.0.0/0", "::/0"]}
                },
                # 2. Upper TCP port range (admin_ssh_port + 1 to 65535)
                {
                    "protocol": "tcp",
                    "ports": f"{admin_ssh_port + 1}-65535",
                    "sources": {"addresses": ["0.0.0.0/0", "::/0"]}
                },
                # 3. Alternate Admin SSH (Port 64295) - Hive IP only
                {
                    "protocol": "tcp",
                    "ports": str(admin_ssh_port),
                    "sources": admin_ssh_source
                },
                # 4. All UDP ports open from everywhere
                {
                    "protocol": "udp",
                    "ports": "1-65535",
                    "sources": {"addresses": ["0.0.0.0/0", "::/0"]}
                },
                # 5. ICMP Ping from everywhere
                {
                    "protocol": "icmp",
                    "sources": {"addresses": ["0.0.0.0/0", "::/0"]}
                }
            ]
        else:
            from app.sensor_types import get_sensor_type
            st = get_sensor_type(sensor_type)
            inbound_rules = []
            for p in st.get("ports", []):
                inbound_rules.append({
                    "protocol": p["proto"].lower(),
                    "ports": str(p["port"]),
                    "sources": {"addresses": ["0.0.0.0/0", "::/0"]}
                })
            inbound_rules.append({
                "protocol": "tcp",
                "ports": str(admin_ssh_port),
                "sources": admin_ssh_source
            })
            inbound_rules.append({
                "protocol": "icmp",
                "sources": {"addresses": ["0.0.0.0/0", "::/0"]}
            })

        # Check existing firewalls
        existing_fws = self.list_firewalls()
        for fw in existing_fws:
            fw_existing_tags = fw.get("tags", [])
            # Match by name or if already tagged with tpot-sensor
            if fw.get("name") == fw_name or "tpot-sensor" in fw_existing_tags or fw.get("name") == f"tpot-{sensor_type}-sensor-fw":
                merged_tags = sorted(list(set(fw_existing_tags + fw_tags)))
                updated = self.update_firewall(
                    firewall_id=fw["id"],
                    name=fw.get("name") or fw_name,
                    inbound_rules=inbound_rules,
                    outbound_rules=DEFAULT_OUTBOUND_RULES,
                    tags=merged_tags
                )
                return updated.get("id") or fw["id"]

        fw = self.create_firewall(
            name=fw_name,
            inbound_rules=inbound_rules,
            outbound_rules=DEFAULT_OUTBOUND_RULES,
            tags=fw_tags
        )
        return fw["id"]

    def ensure_cowrie_firewall(
        self,
        name: str = "tpot-sensor-firewall",
        tag: str = "tpot-sensor",
        hive_ip: Optional[str] = None,
        restrict_ssh: bool = True
    ) -> str:
        """Backward compatible wrapper for sensor firewall."""
        return self.ensure_sensor_firewall(
            sensor_type="cowrie",
            name=name,
            tag=tag,
            hive_ip=hive_ip,
            restrict_ssh=restrict_ssh
        )


