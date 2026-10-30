# Outputs for PersonaOS Terraform Deployment on PVE2

output "control_plane_ip" {
  description = "IPv4 address of the Control Plane VM"
  value       = var.deploy_distributed ? proxmox_virtual_environment_vm.control_plane[0].initialization[0].ip_config[0].ipv4[0].address : null
}

output "worker_ip" {
  description = "IPv4 address of the Worker VM"
  value       = var.deploy_distributed ? proxmox_virtual_environment_vm.worker[0].initialization[0].ip_config[0].ipv4[0].address : null
}

output "all_in_one_ip" {
  description = "IPv4 address of the All-in-One Appliance VM"
  value       = var.deploy_distributed ? null : proxmox_virtual_environment_vm.all_in_one[0].initialization[0].ip_config[0].ipv4[0].address
}

output "ansible_inventory_snippet" {
  description = "Ansible inventory snippet for deploy/iac/ansible/inventory/hosts.ini"
  value = var.deploy_distributed ? format(
    <<-EOT
    [control_plane]
    personaos-cp01 ansible_host=%s

    [workers]
    personaos-worker01 ansible_host=%s

    [database]
    personaos-cp01 ansible_host=%s
    EOT
    ,
    split("/", var.cp_ip)[0],
    split("/", var.worker_ip)[0],
    split("/", var.cp_ip)[0]
  ) : format(
    <<-EOT
    [control_plane]
    personaos-aio ansible_host=%s

    [workers]
    personaos-aio ansible_host=%s

    [database]
    personaos-aio ansible_host=%s
    EOT
    ,
    split("/", var.aio_ip)[0],
    split("/", var.aio_ip)[0],
    split("/", var.aio_ip)[0]
  )
}
