# PersonaOS VM definitions on Proxmox VE 2 (PVE2)
# Follows cyl-dc practices for VM cloning, cloud-init provisioning, and resource allocation.

# -----------------------------------------------------------------------------
# Distributed Mode: Dedicated Control Plane Node
# -----------------------------------------------------------------------------
resource "proxmox_virtual_environment_vm" "control_plane" {
  count     = var.deploy_distributed ? 1 : 0
  name      = "personaos-cp01"
  node_name = var.target_node
  vm_id     = var.cp_vmid

  clone {
    vm_id   = var.template_vmid
    full    = true
    retries = 1
  }

  cpu {
    cores = var.cp_cores
    type  = "x86-64-v2-AES"
  }

  memory {
    dedicated = var.cp_memory_mb
  }

  initialization {
    datastore_id = var.datastore_id

    ip_config {
      ipv4 {
        address = var.cp_ip
        gateway = var.gateway_ipv4
      }
    }

    user_account {
      username = var.ssh_username
      keys     = var.ssh_public_keys
    }
  }

  agent {
    enabled = true
    type    = "virtio"
  }

  network_device {
    bridge   = var.bridge
    firewall = false
  }
}

# -----------------------------------------------------------------------------
# Distributed Mode: Dedicated Worker Node
# -----------------------------------------------------------------------------
resource "proxmox_virtual_environment_vm" "worker" {
  count     = var.deploy_distributed ? 1 : 0
  name      = "personaos-worker01"
  node_name = var.target_node
  vm_id     = var.worker_vmid

  clone {
    vm_id   = var.template_vmid
    full    = true
    retries = 1
  }

  cpu {
    cores = var.worker_cores
    type  = "x86-64-v2-AES"
  }

  memory {
    dedicated = var.worker_memory_mb
  }

  initialization {
    datastore_id = var.datastore_id

    ip_config {
      ipv4 {
        address = var.worker_ip
        gateway = var.gateway_ipv4
      }
    }

    user_account {
      username = var.ssh_username
      keys     = var.ssh_public_keys
    }
  }

  agent {
    enabled = true
    type    = "virtio"
  }

  network_device {
    bridge   = var.bridge
    firewall = false
  }
}

# -----------------------------------------------------------------------------
# Appliance Mode: Single All-in-One Node (Control Plane + Worker + Database)
# -----------------------------------------------------------------------------
resource "proxmox_virtual_environment_vm" "all_in_one" {
  count     = var.deploy_distributed ? 0 : 1
  name      = "personaos-aio"
  node_name = var.target_node
  vm_id     = var.aio_vmid

  clone {
    vm_id   = var.template_vmid
    full    = true
    retries = 1
  }

  cpu {
    cores = var.aio_cores
    type  = "x86-64-v2-AES"
  }

  memory {
    dedicated = var.aio_memory_mb
  }

  initialization {
    datastore_id = var.datastore_id

    ip_config {
      ipv4 {
        address = var.aio_ip
        gateway = var.gateway_ipv4
      }
    }

    user_account {
      username = var.ssh_username
      keys     = var.ssh_public_keys
    }
  }

  agent {
    enabled = true
    type    = "virtio"
  }

  network_device {
    bridge   = var.bridge
    firewall = false
  }
}
