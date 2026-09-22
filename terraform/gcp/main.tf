provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

locals {
  network_name      = var.create_network ? google_compute_network.sensor_vpc[0].name : var.network_name
  subnetwork_id     = var.create_network ? google_compute_subnetwork.sensor_subnet[0].id : null
  admin_ssh_sources = length(var.admin_allowed_cidrs) > 0 ? var.admin_allowed_cidrs : (var.hive_ip != "" ? ["${var.hive_ip}/32"] : ["0.0.0.0/0"])
}

# -------------------------------------------------------------
# VPC Network & Subnet (Optional creation)
# -------------------------------------------------------------
resource "google_compute_network" "sensor_vpc" {
  count                   = var.create_network ? 1 : 0
  name                    = var.network_name
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "sensor_subnet" {
  count         = var.create_network ? 1 : 0
  name          = "${var.network_name}-subnet"
  ip_cidr_range = var.subnet_cidr
  region        = var.region
  network       = google_compute_network.sensor_vpc[0].id
}

# -------------------------------------------------------------
# Cloud Firewall Rules
# -------------------------------------------------------------
# 1. Allow Inbound Admin Management SSH on alternate port (default 64295) strictly from Hive IP
resource "google_compute_firewall" "allow_admin_ssh" {
  name        = "${var.sensor_name}-allow-admin-ssh"
  network     = local.network_name
  description = "Permit administrative SSH on port ${var.admin_ssh_port} strictly from Hive IP"

  allow {
    protocol = "tcp"
    ports    = [tostring(var.admin_ssh_port)]
  }

  source_ranges = local.admin_ssh_sources
  target_tags   = ["tpot-sensor", "tpot-${var.sensor_type}-sensor"]
}

# 2. Allow Inbound TCP for all other ports (1-64294 and 64296-65535) from everywhere
resource "google_compute_firewall" "allow_all_tcp_honeypot" {
  name        = "${var.sensor_name}-allow-all-tcp"
  network     = local.network_name
  description = "Permit internet traffic to all TCP ports except alternate admin SSH port ${var.admin_ssh_port}"

  allow {
    protocol = "tcp"
    ports    = ["1-${var.admin_ssh_port - 1}", "${var.admin_ssh_port + 1}-65535"]
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["tpot-sensor", "tpot-${var.sensor_type}-sensor"]
}

# 3. Allow Inbound UDP for all ports from everywhere
resource "google_compute_firewall" "allow_all_udp_honeypot" {
  name        = "${var.sensor_name}-allow-all-udp"
  network     = local.network_name
  description = "Permit internet traffic to all UDP ports"

  allow {
    protocol = "udp"
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["tpot-sensor", "tpot-${var.sensor_type}-sensor"]
}

# 4. Allow ICMP Diagnostic Pings from everywhere
resource "google_compute_firewall" "allow_icmp" {
  name        = "${var.sensor_name}-allow-icmp"
  network     = local.network_name
  description = "Allow ICMP diagnostic pings from everywhere"

  allow {
    protocol = "icmp"
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["tpot-sensor", "tpot-${var.sensor_type}-sensor"]
}

# 5. Allow Outbound Traffic to Hive Logstash on port 64294 and general internet egress
resource "google_compute_firewall" "allow_hive_egress" {
  name        = "${var.sensor_name}-allow-hive-egress"
  network     = local.network_name
  direction   = "EGRESS"
  description = "Permit outbound log streaming to Hive and general internet egress"

  allow {
    protocol = "all"
  }

  destination_ranges = ["0.0.0.0/0"]
  target_tags        = ["tpot-sensor", "tpot-${var.sensor_type}-sensor"]
}


# -------------------------------------------------------------
# Compute Engine Instance (Honeypot Sensor)
# -------------------------------------------------------------
resource "google_compute_instance" "cowrie_sensor" {
  name         = var.sensor_name
  machine_type = var.machine_type
  zone         = var.zone
  tags         = ["tpot-${var.sensor_type}-sensor", "tpot-sensor"]
  labels       = var.labels

  boot_disk {
    initialize_params {
      image = var.boot_image
      size  = var.disk_size_gb
      type  = "pd-standard"
    }
  }

  network_interface {
    network    = var.create_network ? null : local.network_name
    subnetwork = local.subnetwork_id

    access_config {
      // Strictly dynamic ephemeral IP from GCP pool - zero IP reservations
    }
  }

  metadata = merge(
    var.ssh_public_key != "" ? {
      "ssh-keys" = "${var.ssh_user}:${var.ssh_public_key}"
    } : {},
    {
      "enable-oslogin" = "FALSE"
    }
  )

  metadata_startup_script = templatefile("${path.module}/scripts/startup.sh.tpl", {
    sensor_name     = var.sensor_name
    sensor_type     = var.sensor_type
    hive_ip         = var.hive_ip
    hive_port       = var.hive_port
    hive_cert       = var.hive_cert
    tpot_hive_user  = var.tpot_hive_user
    tpot_version    = var.tpot_version
    admin_ssh_port  = var.admin_ssh_port
    compose_content = var.compose_content
    required_dirs   = var.required_dirs
  })

  service_account {
    scopes = ["logging-write", "monitoring-write"]
  }

  lifecycle {
    ignore_changes = [
      metadata["ssh-keys"]
    ]
  }
}
