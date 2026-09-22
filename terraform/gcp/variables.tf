variable "project_id" {
  type        = string
  description = "The GCP Project ID where resources will be provisioned."
  default     = "tpot-hive-project"
}

variable "region" {
  type        = string
  description = "The GCP region to deploy the sensor in."
  default     = "us-central1"
}

variable "zone" {
  type        = string
  description = "The GCP zone for the compute instance."
  default     = "us-central1-a"
}

variable "sensor_name" {
  type        = string
  description = "Unique name for the Cowrie sensor instance."
  default     = "tpot-cowrie-sensor-gcp"
}

variable "machine_type" {
  type        = string
  description = "GCP Compute Engine machine type (e2-micro, e2-small, e2-medium)."
  default     = "e2-small"
}

variable "boot_image" {
  type        = string
  description = "Operating system image family and project."
  default     = "ubuntu-os-cloud/ubuntu-2404-lts"
}

variable "disk_size_gb" {
  type        = number
  description = "Root persistent disk size in GB."
  default     = 30
}

variable "create_network" {
  type        = bool
  description = "Whether to create a dedicated VPC network. If false, uses existing network."
  default     = true
}

variable "network_name" {
  type        = string
  description = "Name of the VPC network."
  default     = "tpot-sensor-vpc"
}

variable "subnet_cidr" {
  type        = string
  description = "CIDR range for the dedicated subnet."
  default     = "10.10.0.0/24"
}

variable "ssh_user" {
  type        = string
  description = "Username for host administration SSH."
  default     = "adminuser"
}

variable "ssh_public_key" {
  type        = string
  description = "SSH public key content to inject into the instance."
  default     = ""
}

variable "admin_ssh_port" {
  type        = number
  description = "Management SSH port (relocated away from port 22)."
  default     = 64295
}

variable "admin_allowed_cidrs" {
  type        = list(string)
  description = "List of IPv4 CIDR blocks allowed to connect to admin SSH port 64295. Defaults to Hive IP (/32) if empty."
  default     = []
}

variable "hive_ip" {
  type        = string
  description = "Public IP or FQDN of the central T-Pot Hive host."
  default     = "203.0.113.10"
}

variable "hive_port" {
  type        = number
  description = "Hive Logstash ingestion port."
  default     = 64294
}

variable "hive_cert" {
  type        = string
  description = "PEM certificate content of the Hive's nginx.crt for TLS authentication."
  default     = ""
}

variable "tpot_hive_user" {
  type        = string
  description = "Base64 encoded Basic Auth credentials (username:password) for Logstash."
  default     = ""
}

variable "tpot_version" {
  type        = string
  description = "T-Pot container tag version."
  default     = "24.04"
}

variable "labels" {
  type        = map(string)
  description = "Key-value labels to assign to GCP resources."
  default = {
    role        = "tpot-sensor"
    managed_by  = "terraform"
    environment = "honeypot"
  }
}

variable "sensor_type" {
  type        = string
  description = "Honeypot sensor type (cowrie, dionaea, conpot, elasticpot, mailoney, heralding, ciscoasa, multi_sensor, etc.)."
  default     = "cowrie"
}

variable "sensor_ports_tcp" {
  type        = list(string)
  description = "TCP ports opened for the honeypot sensor."
  default     = ["22", "23"]
}

variable "sensor_ports_udp" {
  type        = list(string)
  description = "UDP ports opened for the honeypot sensor."
  default     = []
}

variable "compose_content" {
  type        = string
  description = "Complete docker-compose.yml content generated for the target sensor type."
  default     = ""
}

variable "required_dirs" {
  type        = list(string)
  description = "List of directories under /data required by the sensor."
  default     = []
}

