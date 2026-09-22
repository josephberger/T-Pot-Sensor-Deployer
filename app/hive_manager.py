import os
import re
import socket
import ssl
import random
import string
import base64
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.config import TPOT_DIR

# Curated tactical wordlists for random sensor naming
ADJECTIVES = [
    "amber", "azure", "bold", "brave", "calm", "clever", "cobalt", "crimson",
    "cyber", "dark", "eager", "fast", "fierce", "frost", "ghost", "golden",
    "grand", "gray", "hardy", "iron", "keen", "lunar", "neon", "noble",
    "polar", "prime", "quick", "rapid", "shadow", "sharp", "silent", "silver",
    "solar", "stealth", "steel", "swift", "tactical", "vivid", "wild", "zenith",
    # silly
    "caffeinated", "fluffy", "grumpy", "sassy", "sleepy", "soggy", "spicy", "wobbly",
    "unplugged", "overclocked", "suspicious", "haunted",
    # Halt and Catch Fire
    "halting", "burning", "smoldering", "combusting", "flaming", "undocumented",
    # Fallout
    "irradiated", "glowing", "feral", "dusty", "rusty", "mutated", "prewar", "scavenged"
]

NOUNS = [
    "badger", "beacon", "canary", "citadel", "condor", "crypto", "decoy",
    "drone", "eagle", "falcon", "fortress", "fox", "gargoyle", "harbor",
    "hawk", "hound", "lynx", "mantis", "matrix", "monitor", "nexus",
    "node", "outpost", "owl", "panther", "patrol", "proxy", "radar",
    "raven", "sentinel", "sentry", "shadow", "shield", "sonar", "spider",
    "tower", "tracer", "vanguard", "viper", "warden",
    # silly
    "goblin", "gremlin", "llama", "noodle", "pancake", "pickle", "toaster", "waffle", "walrus",
    # Halt and Catch Fire
    "bosworth", "cameron", "cardiff", "cinder", "comet", "donna", "ember", "gordon",
    "joe", "kernel", "mainframe", "mutiny", "opcode", "phoenix", "spark",
    # Fallout
    "bloatfly", "brahmin", "cazador", "codsworth", "dogmeat", "eyebot", "goodsprings",
    "megaton", "mentat", "mirelurk", "molerat", "novac", "primm", "protectron",
    "radstag", "securitron", "yaoguai"
]


class HiveManager:
    """Manages integration with the local T-Pot Hive installation."""

    def __init__(self, tpot_dir: Optional[Path] = None):
        self.tpot_dir = Path(tpot_dir or TPOT_DIR)
        self.env_file = self.tpot_dir / ".env"
        self.cert_file = self.tpot_dir / "data" / "nginx" / "cert" / "nginx.crt"
        self.lswebpasswd_file = self.tpot_dir / "data" / "nginx" / "conf" / "lswebpasswd"
        self._cached_ip: Optional[str] = None
        self._cached_cert: Optional[str] = None
        self._is_hive: Optional[bool] = None

    def is_hive_installed(self) -> bool:
        """Check if T-Pot directory exists and is configured as Hive."""
        if self._is_hive is not None:
            return self._is_hive
        if not self.env_file.exists():
            self._is_hive = False
            return False
        try:
            content = self.env_file.read_text(encoding="utf-8")
            self._is_hive = "TPOT_TYPE=HIVE" in content
            return self._is_hive
        except Exception:
            self._is_hive = False
            return False

    def detect_hive_ip(self, force_refresh: bool = False) -> str:
        """Detect Hive public or local IP address with caching."""
        if not force_refresh and self._cached_ip:
            return self._cached_ip

        # Check environment variable first
        env_ip = os.environ.get("TPOT_HIVE_IP")
        if env_ip:
            self._cached_ip = env_ip
            return env_ip

        # Try to parse CN from certificate if available
        cert_text = self.get_hive_certificate()
        if cert_text:
            match = re.search(r"CN\s*=\s*([0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3})", cert_text)
            if match:
                self._cached_ip = match.group(1)
                return self._cached_ip

        # Query public IP via web services
        for url in ["https://api.ipify.org", "https://ifconfig.me/ip"]:
            try:
                import requests
                resp = requests.get(url, timeout=2)
                if resp.status_code == 200 and resp.text.strip():
                    self._cached_ip = resp.text.strip()
                    return self._cached_ip
            except Exception:
                pass

        # Fallback to local default route IP
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                self._cached_ip = s.getsockname()[0]
                return self._cached_ip
        except Exception:
            self._cached_ip = "127.0.0.1"
            return self._cached_ip

    def get_hive_certificate(self) -> str:
        """Read the Hive Nginx SSL Certificate with caching."""
        if self._cached_cert is not None:
            return self._cached_cert
        try:
            if self.cert_file.exists():
                self._cached_cert = self.cert_file.read_text(encoding="utf-8")
                return self._cached_cert
        except Exception:
            pass
        return ""

    load_hive_certificate = get_hive_certificate

    def list_registered_sensors(self) -> List[str]:
        """List usernames of sensors currently registered in lswebpasswd."""
        sensors = []
        try:
            if self.lswebpasswd_file.exists():
                with open(self.lswebpasswd_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and ":" in line:
                            user = line.split(":", 1)[0]
                            sensors.append(user)
        except Exception:
            pass
        return sensors

    def generate_sensor_credentials(self, name_prefix: str = "sensor") -> Dict[str, str]:
        """Generate a random sensor username, password, htpasswd hash, and base64 tokens."""
        adj = random.choice(ADJECTIVES)
        noun = random.choice(NOUNS)
        username = f"{name_prefix}-{adj}-{noun}"

        # 32-character secure random password
        chars = string.ascii_letters + string.digits
        password = "".join(random.choice(chars) for _ in range(32))

        # Generate htpasswd hash using standard htpasswd binary
        htpasswd_line = ""
        try:
            res = subprocess.run(
                ["htpasswd", "-b", "-n", username, password],
                capture_output=True,
                text=True,
                check=True
            )
            htpasswd_line = res.stdout.strip()
        except Exception:
            # Fallback if htpasswd binary fails: simple base64 placeholder
            htpasswd_line = f"{username}:{password}"

        # Base64 tokens
        ls_web_user_enc_b64 = base64.b64encode(htpasswd_line.encode("utf-8")).decode("utf-8")
        tpot_hive_user_b64 = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("utf-8")

        return {
            "username": username,
            "password": password,
            "htpasswd_line": htpasswd_line,
            "ls_web_user_enc_b64": ls_web_user_enc_b64,
            "tpot_hive_user": tpot_hive_user_b64
        }

    def register_sensor(self, creds: Dict[str, str]) -> Tuple[bool, str]:
        """Register the sensor credentials into Hive's lswebpasswd and .env."""
        if not self.is_hive_installed():
            return False, f"T-Pot Hive installation not found at {self.tpot_dir}"

        username = creds["username"]
        htpasswd_line = creds["htpasswd_line"]
        b64_val = creds["ls_web_user_enc_b64"]

        try:
            # 1. Update lswebpasswd
            existing_lines = []
            if self.lswebpasswd_file.exists():
                with open(self.lswebpasswd_file, "r", encoding="utf-8") as f:
                    existing_lines = [l.strip() for l in f if l.strip()]

            # Avoid duplicates
            filtered_lines = [l for l in existing_lines if not l.startswith(f"{username}:")]
            filtered_lines.append(htpasswd_line)

            self.lswebpasswd_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.lswebpasswd_file, "w", encoding="utf-8") as f:
                f.write("\n".join(filtered_lines) + "\n")

            # 2. Update .env (LS_WEB_USER)
            if self.env_file.exists():
                content = self.env_file.read_text(encoding="utf-8")
                # Look for LS_WEB_USER=...
                match = re.search(r"^LS_WEB_USER=(.*)$", content, re.MULTILINE)
                if match:
                    current_tokens = match.group(1).strip().split()
                    if b64_val not in current_tokens:
                        current_tokens.append(b64_val)
                    new_line = f"LS_WEB_USER={' '.join(current_tokens)}"
                    new_content = re.sub(r"^LS_WEB_USER=.*$", new_line, content, flags=re.MULTILINE)
                else:
                    new_content = content + f"\nLS_WEB_USER={b64_val}\n"

                self.env_file.write_text(new_content, encoding="utf-8")

            return True, f"Successfully registered sensor '{username}' to Hive"

        except Exception as e:
            return False, f"Failed to register sensor on Hive: {str(e)}"

    def deregister_sensor(self, username: str) -> Tuple[bool, str]:
        """Remove a sensor from lswebpasswd and .env."""
        try:
            if self.lswebpasswd_file.exists():
                with open(self.lswebpasswd_file, "r", encoding="utf-8") as f:
                    lines = [l.strip() for l in f if l.strip() and not l.startswith(f"{username}:")]
                with open(self.lswebpasswd_file, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines) + ("\n" if lines else ""))

            # In .env, we decode each token to check if it matches the username
            if self.env_file.exists():
                content = self.env_file.read_text(encoding="utf-8")
                match = re.search(r"^LS_WEB_USER=(.*)$", content, re.MULTILINE)
                if match:
                    current_tokens = match.group(1).strip().split()
                    retained_tokens = []
                    for token in current_tokens:
                        try:
                            decoded = base64.b64decode(token).decode("utf-8")
                            if not decoded.startswith(f"{username}:"):
                                retained_tokens.append(token)
                        except Exception:
                            retained_tokens.append(token)
                    new_line = f"LS_WEB_USER={' '.join(retained_tokens)}"
                    new_content = re.sub(r"^LS_WEB_USER=.*$", new_line, content, flags=re.MULTILINE)
                    self.env_file.write_text(new_content, encoding="utf-8")

            return True, f"Sensor '{username}' deregistered from Hive"
        except Exception as e:
            return False, f"Failed to deregister sensor: {str(e)}"

    def test_hive_port(self, hive_ip: Optional[str] = None, port: int = 64294, timeout: float = 3.0) -> Dict[str, any]:
        """Test TLS connection to the Hive Logstash endpoint."""
        target_ip = hive_ip or self.detect_hive_ip()
        result = {
            "ip": target_ip,
            "port": port,
            "reachable": False,
            "tls_handshake": False,
            "message": ""
        }

        try:
            sock = socket.create_connection((target_ip, port), timeout=timeout)
            result["reachable"] = True

            # Attempt TLS handshake
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with ctx.wrap_socket(sock, server_hostname=target_ip) as ssock:
                result["tls_handshake"] = True
                cipher = ssock.cipher()
                result["message"] = f"Connected successfully via TLS ({cipher[0]} - {cipher[1]})"
            return result
        except Exception as e:
            result["message"] = f"Connection failed: {str(e)}"
            return result
