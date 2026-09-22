import os
import time
import json
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Any, Optional, Callable, Tuple

from app.config import PROJECT_ROOT, CONFIG_DIR

# Read-only module source shipped in the image.
TERRAFORM_MODULE_DIR = PROJECT_ROOT / "terraform" / "gcp"
# Working directory (tfstate, workspaces, .terraform, tfvars) lives in the persistent data volume.
TERRAFORM_DIR = CONFIG_DIR / "terraform" / "gcp"
TERRAFORM_BIN = shutil.which("terraform") or "terraform"
_MODULE_FILES = ("*.tf", "*.tpl", ".terraform.lock.hcl", "terraform.tfvars.example")


def sync_terraform_module(src: Path, dest: Path) -> None:
    """Copy module definition files (not state) from the image into the working dir."""
    if not src.exists() or src.resolve() == dest.resolve():
        return
    dest.mkdir(parents=True, exist_ok=True)
    for pattern in _MODULE_FILES:
        for f in src.rglob(pattern):
            if ".terraform" in f.relative_to(src).parts[:-1]:
                continue
            target = dest / f.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)


class GCPTerraformManager:
    """Manages GCP Cowrie sensor deployment via Terraform."""

    def __init__(self, tf_dir: Path = TERRAFORM_DIR, tf_bin: str = TERRAFORM_BIN):
        self.tf_dir = tf_dir
        self.tf_bin = tf_bin
        sync_terraform_module(TERRAFORM_MODULE_DIR, self.tf_dir)
        self._is_installed: Optional[bool] = None
        self._version: Optional[str] = None
        self._outputs_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    def invalidate_cache(self):
        """Invalidate in-memory output caches."""
        self._outputs_cache.clear()

    def is_terraform_installed(self) -> bool:
        if self._is_installed is not None:
            return self._is_installed
        if shutil.which(self.tf_bin):
            self._is_installed = True
            return True
        self._is_installed = os.path.exists(self.tf_bin) and os.access(self.tf_bin, os.X_OK)
        return self._is_installed

    def get_terraform_version(self) -> str:
        if self._version is not None:
            return self._version
        if not self.is_terraform_installed():
            self._version = "Not installed"
            return self._version
        try:
            res = subprocess.run([self.tf_bin, "version"], capture_output=True, text=True, timeout=5)
            lines = res.stdout.strip().splitlines()
            self._version = lines[0] if lines else "Unknown version"
            return self._version
        except Exception as e:
            return f"Error checking version: {str(e)}"

    def ensure_init(self) -> Tuple[bool, str]:
        """Ensure terraform init has run."""
        if not self.is_terraform_installed():
            return False, f"Terraform executable not found at {self.tf_bin}"

        if not (self.tf_dir / ".terraform").exists():
            try:
                init_res = subprocess.run(
                    [self.tf_bin, "init", "-backend=false"],
                    cwd=str(self.tf_dir),
                    capture_output=True,
                    text=True,
                    timeout=45
                )
                if init_res.returncode != 0:
                    return False, f"Terraform init failed: {init_res.stderr or init_res.stdout}"
            except Exception as e:
                return False, f"Init error: {str(e)}"
        return True, "Initialized"

    def run_validate(self) -> Dict[str, Any]:
        """Runs 'terraform validate -json' in the terraform/gcp directory."""
        ok, msg = self.ensure_init()
        if not ok:
            return {"valid": False, "error": msg, "diagnostics": []}

        try:
            res = subprocess.run(
                [self.tf_bin, "validate", "-json"],
                cwd=str(self.tf_dir),
                capture_output=True,
                text=True,
                timeout=15
            )
            try:
                data = json.loads(res.stdout)
                return {
                    "valid": data.get("valid", False),
                    "error_count": data.get("error_count", 0),
                    "warning_count": data.get("warning_count", 0),
                    "diagnostics": data.get("diagnostics", []),
                    "raw_output": res.stdout
                }
            except json.JSONDecodeError:
                return {
                    "valid": res.returncode == 0,
                    "raw_output": res.stdout,
                    "error": res.stderr if res.returncode != 0 else None
                }
        except Exception as e:
            return {"valid": False, "error": str(e), "diagnostics": []}

    def select_workspace(self, workspace_name: str = "default") -> Tuple[bool, str]:
        """Switches or creates a workspace for isolating sensor state."""
        ok, msg = self.ensure_init()
        if not ok:
            return False, msg
        try:
            res = subprocess.run(
                [self.tf_bin, "workspace", "select", "-or-create", workspace_name],
                cwd=str(self.tf_dir),
                capture_output=True,
                text=True,
                timeout=15
            )
            if res.returncode != 0:
                return False, res.stderr or res.stdout
            return True, f"Selected workspace {workspace_name}"
        except Exception as e:
            return False, str(e)

    def list_workspaces(self) -> List[str]:
        """List all configured Terraform workspaces without subprocess overhead."""
        workspaces = ["default"]
        ws_dir = self.tf_dir / "terraform.tfstate.d"
        if ws_dir.exists() and ws_dir.is_dir():
            for child in ws_dir.iterdir():
                if child.is_dir() and child.name not in workspaces:
                    workspaces.append(child.name)
        return workspaces

    def write_tfvars(self, vars_dict: Dict[str, Any]) -> Path:
        """Writes variables to terraform.tfvars.json for execution."""
        tfvars_path = self.tf_dir / "terraform.tfvars.json"
        with open(tfvars_path, "w", encoding="utf-8") as f:
            json.dump(vars_dict, f, indent=2)
        return tfvars_path

    def get_outputs(self, workspace: Optional[str] = None) -> Dict[str, Any]:
        """Reads terraform output values as a dictionary, using direct state file inspection and caching."""
        ws = workspace or "default"
        now = time.time()
        if ws in self._outputs_cache:
            ts, data = self._outputs_cache[ws]
            if now - ts < 30.0:
                return data

        # Check for state file on disk
        if ws == "default":
            state_file = self.tf_dir / "terraform.tfstate"
        else:
            state_file = self.tf_dir / "terraform.tfstate.d" / ws / "terraform.tfstate"

        if not state_file.exists():
            self._outputs_cache[ws] = (now, {})
            return {}

        try:
            with open(state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                raw_outputs = data.get("outputs", {})
                parsed = {k: v.get("value") for k, v in raw_outputs.items() if isinstance(v, dict) and "value" in v}
                self._outputs_cache[ws] = (now, parsed)
                return parsed
        except Exception:
            pass

        # Fallback to terraform output command if needed
        if not self.is_terraform_installed():
            return {}
        if workspace and workspace != "default":
            self.select_workspace(workspace)
        try:
            res = subprocess.run(
                [self.tf_bin, "output", "-json"],
                cwd=str(self.tf_dir),
                capture_output=True,
                text=True,
                timeout=10
            )
            if res.returncode == 0 and res.stdout.strip():
                data = json.loads(res.stdout)
                parsed = {k: v.get("value") for k, v in data.items() if isinstance(v, dict) and "value" in v}
                self._outputs_cache[ws] = (now, parsed)
                return parsed
        except Exception:
            pass
        self._outputs_cache[ws] = (now, {})
        return {}

    def get_sensor_info(self, workspace: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Returns details of currently provisioned GCP sensor if present."""
        outputs = self.get_outputs(workspace=workspace)
        pub_ip = outputs.get("sensor_public_ip")
        if not pub_ip:
            return None

        return {
            "name": outputs.get("sensor_name", "tpot-cowrie-sensor-gcp"),
            "public_ip": pub_ip,
            "private_ip": outputs.get("sensor_private_ip"),
            "ssh_command": outputs.get("ssh_admin_command"),
            "test_command": outputs.get("test_cowrie_command"),
            "target_hive": outputs.get("target_hive"),
            "provider": "gcp",
            "workspace": workspace or "default",
            "status": "active"
        }

    def run_apply(
        self,
        on_log: Callable[[str, Optional[int], Optional[str]], None],
        vars_dict: Optional[Dict[str, Any]] = None,
        workspace: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Executes 'terraform apply -auto-approve -no-color' asynchronously, streaming logs
        via the on_log callback.
        """
        ok, msg = self.ensure_init()
        if not ok:
            raise RuntimeError(msg)

        if workspace:
            ws_ok, ws_msg = self.select_workspace(workspace)
            if not ws_ok:
                raise RuntimeError(f"Failed to switch workspace: {ws_msg}")
            on_log(f"📁 Switched to Terraform workspace: {workspace}", 5, "info")

        if vars_dict:
            self.write_tfvars(vars_dict)

        cmd = [self.tf_bin, "apply", "-auto-approve", "-no-color"]
        on_log(f"🚀 Launching Terraform Apply in {self.tf_dir}...", 10, "info")

        process = subprocess.Popen(
            cmd,
            cwd=str(self.tf_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        for line in iter(process.stdout.readline, ""):
            line_clean = line.strip()
            if line_clean:
                pct = None
                level = "info"
                if "Creating..." in line_clean:
                    pct = 40
                elif "Still creating..." in line_clean:
                    pct = 60
                elif "Creation complete" in line_clean:
                    pct = 85
                    level = "success"
                elif "Error:" in line_clean:
                    level = "error"
                on_log(line_clean, pct, level)

        process.stdout.close()
        return_code = process.wait()

        if return_code != 0:
            raise RuntimeError(f"Terraform apply failed with exit code {return_code}")

        outputs = self.get_outputs()
        on_log("✅ Terraform apply completed successfully!", 100, "success")
        return outputs

    def run_destroy(self, on_log: Callable[[str, Optional[int], Optional[str]], None], workspace: Optional[str] = None) -> bool:
        """
        Executes 'terraform destroy -auto-approve -no-color' asynchronously, streaming logs.
        """
        ok, msg = self.ensure_init()
        if not ok:
            raise RuntimeError(msg)

        if workspace:
            ws_ok, ws_msg = self.select_workspace(workspace)
            if not ws_ok:
                raise RuntimeError(f"Failed to switch workspace: {ws_msg}")
            on_log(f"📁 Switched to Terraform workspace: {workspace}", 5, "info")

        cmd = [self.tf_bin, "destroy", "-auto-approve", "-no-color"]
        on_log("🗑️ Running Terraform Destroy...", 20, "warn")

        process = subprocess.Popen(
            cmd,
            cwd=str(self.tf_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        for line in iter(process.stdout.readline, ""):
            line_clean = line.strip()
            if line_clean:
                level = "error" if "Error:" in line_clean else "info"
                on_log(line_clean, None, level)

        process.stdout.close()
        return_code = process.wait()

        if return_code != 0:
            raise RuntimeError(f"Terraform destroy failed with exit code {return_code}")

        on_log("✅ GCP Sensor resources destroyed successfully.", 100, "success")
        return True
