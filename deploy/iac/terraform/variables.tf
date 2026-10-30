# Variables for PersonaOS deployment on PVE2

variable "proxmox_endpoint" {
  description = "Proxmox API endpoint for PVE2"
  type        = string
  default     = "https://192.168.11.252:8006/"
}

variable "proxmox_api_token" {
  description = "Proxmox API token for PVE2 (format: user@realm!tokenid=uuid)"
  type        = string
  default     = "root@pam!oc1=460a8e5b-2556-46e7-8e2b-f3808593d04b"
  sensitive   = true
}

variable "proxmox_insecure" {
  description = "Skip TLS certificate verification for self-signed Proxmox certificates"
  type        = bool
  default     = true
}

variable "target_node" {
  description = "Target Proxmox node name"
  type        = string
  default     = "pve2"
}

variable "datastore_id" {
  description = "Storage pool on PVE2 for VM cloud-init drive and root disks"
  type        = string
  default     = "local1"
}

variable "bridge" {
  description = "Network bridge interface on PVE2"
  type        = string
  default     = "vmbr0"
}

variable "template_vmid" {
  description = "Source VM template ID on PVE2 (Ubuntu 24.04 LTS cloud-init template)"
  type        = number
  default     = 104
}

variable "ssh_username" {
  description = "Default administrative username created by cloud-init"
  type        = string
  default     = "user"
}

variable "ssh_public_keys" {
  description = "List of SSH public keys authorized for the admin user"
  type        = list(string)
  default = [
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB5HDZjLiZygMdm2pJHzDm+AgD82j4q9PL0kXgBJEeK9 root@openclaw01"
  ]
}

variable "gateway_ipv4" {
  description = "Default IPv4 gateway for Site 2 subnet (192.168.11.0/24)"
  type        = string
  default     = "192.168.11.2"
}

# -----------------------------------------------------------------------------
# Deployment Topology Settings
# -----------------------------------------------------------------------------

variable "deploy_distributed" {
  description = "If true, deploy separate Control Plane and Worker VMs. If false, deploy single All-in-One VM."
  type        = bool
  default     = true
}

# Distributed Control Plane VM
variable "cp_vmid" {
  description = "VM ID for PersonaOS Control Plane node"
  type        = number
  default     = 250
}

variable "cp_ip" {
  description = "IPv4 address and CIDR for PersonaOS Control Plane node"
  type        = string
  default     = "192.168.11.246/24"
}

variable "cp_cores" {
  description = "CPU cores for Control Plane node"
  type        = number
  default     = 4
}

variable "cp_memory_mb" {
  description = "Memory in MB for Control Plane node"
  type        = number
  default     = 8192
}

# Distributed Worker VM
variable "worker_vmid" {
  description = "VM ID for PersonaOS Worker node"
  type        = number
  default     = 251
}

variable "worker_ip" {
  description = "IPv4 address and CIDR for PersonaOS Worker node"
  type        = string
  default     = "192.168.11.247/24"
}

variable "worker_cores" {
  description = "CPU cores for Worker node"
  type        = number
  default     = 4
}

variable "worker_memory_mb" {
  description = "Memory in MB for Worker node"
  type        = number
  default     = 8192
}

# All-in-One Appliance VM (used when deploy_distributed is false)
variable "aio_vmid" {
  description = "VM ID for PersonaOS All-in-One Appliance node"
  type        = number
  default     = 252
}

variable "aio_ip" {
  description = "IPv4 address and CIDR for PersonaOS All-in-One Appliance node"
  type        = string
  default     = "192.168.11.248/24"
}

variable "aio_cores" {
  description = "CPU cores for All-in-One node"
  type        = number
  default     = 6
}

variable "aio_memory_mb" {
  description = "Memory in MB for All-in-One node"
  type        = number
  default     = 12288
}
