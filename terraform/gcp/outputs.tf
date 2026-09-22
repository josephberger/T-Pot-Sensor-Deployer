output "sensor_public_ip" {
  description = "The dynamic public IP address of the GCP Cowrie sensor (add to Palo Alto EDL)."
  value       = google_compute_instance.cowrie_sensor.network_interface[0].access_config[0].nat_ip
}

output "sensor_private_ip" {
  description = "The internal VPC IP address of the instance."
  value       = google_compute_instance.cowrie_sensor.network_interface[0].network_ip
}

output "sensor_name" {
  description = "The name of the deployed sensor instance."
  value       = google_compute_instance.cowrie_sensor.name
}

output "ssh_admin_command" {
  description = "SSH command to connect to the host management interface."
  value       = "ssh -p ${var.admin_ssh_port} ${var.ssh_user}@${google_compute_instance.cowrie_sensor.network_interface[0].access_config[0].nat_ip}"
}

output "test_cowrie_command" {
  description = "Command to simulate an attacker connecting to the Cowrie honeypot."
  value       = "ssh root@${google_compute_instance.cowrie_sensor.network_interface[0].access_config[0].nat_ip} -p 22"
}

output "palo_alto_edl_entry" {
  description = "Plaintext line entry for the Palo Alto Networks External Dynamic List."
  value       = "${google_compute_instance.cowrie_sensor.network_interface[0].access_config[0].nat_ip}  # GCP Sensor - ${var.sensor_name}"
}

output "target_hive" {
  description = "The T-Pot Hive receiving the Logstash sensor telemetry."
  value       = "${var.hive_ip}:${var.hive_port}"
}
