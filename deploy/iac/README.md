# PersonaOS Infrastructure as Code (IaC) — PVE2 Deployment

This directory contains the automated Infrastructure as Code (IaC) for deploying PersonaOS on **Proxmox VE 2 (PVE2)**.

The architecture and automation patterns strictly follow the infrastructure standards and conventions established in `cyl-dc`:
- **Terraform / OpenTofu:** VM provisioning via the `bpg/proxmox` provider (v0.66.0).
- **Proxmox Node:** `pve2` (Site 2, `192.168.11.0/24` subnet, gateway `192.168.11.2`).
- **Cloud-Init Base Template:** Ubuntu 24.04 LTS (Template VMID `104` on storage `local1`).
- **Ansible Automation:** Disk expansion (`disk-resize.yml`), PostgreSQL 16 setup, Alembic migrations, and systemd service management.

---

## 1. Target Topology on PVE2

| Role | Hostname | VMID | vCPUs | RAM | Storage | IP Address | Subnet |
|---|---|---|---|---|---|---|---|
| **Control Plane** | `personaos-cp01` | `250` | 4 | 8 GB | `local1` (50 GB) | `192.168.11.246` | `192.168.11.0/24` |
| **Worker Node** | `personaos-worker01` | `251` | 4 | 8 GB | `local1` (50 GB) | `192.168.11.247` | `192.168.11.0/24` |
| **All-in-One Appliance (Optional)** | `personaos-aio` | `252` | 6 | 12 GB | `local1` (80 GB) | `192.168.11.248` | `192.168.11.0/24` |

- **Gateway:** `192.168.11.2`
- **DNS Server:** `192.168.11.2`
- **Bridge:** `vmbr0`
- **Default User:** `user`
- **Authorized SSH Key:** `ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB5HDZjLiZygMdm2pJHzDm+AgD82j4q9PL0kXgBJEeK9 root@openclaw01`

---

## 2. Directory Structure

```text
deploy/iac/
├── README.md                      # This documentation
├── terraform/
│   ├── provider.tf                # Proxmox bpg/proxmox provider for PVE2
│   ├── variables.tf               # VM specifications, IPs, and PVE2 parameters
│   ├── main.tf                    # Proxmox VM resources cloned from template 104
│   ├── outputs.tf                 # VM IPs and generated Ansible inventory
│   └── terraform.tfvars.example   # Example variable overrides
└── ansible/
    ├── ansible.cfg                # Ansible configuration (StrictHostKeyChecking=no, pipelining)
    ├── inventory/
    │   └── hosts.ini              # Cluster inventory and credentials
    ├── playbooks/
    │   ├── deploy.yml             # Master deployment playbook
    │   ├── disk-resize.yml        # Root partition & LVM growpart expansion
    │   ├── install-prereqs.yml    # Python 3.12, PostgreSQL 16, system packages
    │   ├── setup-database.yml     # PostgreSQL 16 user, database, and pg_hba.conf
    │   └── setup-personaos.yml    # Alembic migrations & systemd services
    ├── templates/
    │   ├── personaos.env.j2       # Environment file (/etc/personaos/personaos.env)
    │   ├── personaos-api.service.j2    # Control Plane systemd service
    │   └── personaos-worker.service.j2 # Worker Runner systemd service
    └── run.sh                     # Automated CLI orchestration wrapper
```

---

## 3. Prerequisites

1. **Proxmox API Token:**
   - Token ID: `root@pam!oc1`
   - Secret UUID: `460a8e5b-2556-46e7-8e2b-f3808593d04b`
   - Set via `terraform.tfvars` or `export TF_VAR_proxmox_api_token="root@pam!oc1=..."`.

2. **Template VM on PVE2:**
   - Template VMID `104` must exist on node `pve2` with cloud-init and `qemu-guest-agent` installed.

3. **Management Machine Tools:**
   - `terraform` (or `tofu`) >= 1.5.0
   - `ansible` >= 2.15.0
   - `ssh` key `~/.ssh/id_ed25519`

---

## 4. Deployment Execution

### Automated One-Click Deployment (Recommended)

Run the orchestration script:

```bash
cd deploy/iac/ansible
./run.sh deploy
```

This will sequentially:
1. Initialize and apply Terraform to clone and boot the VMs on `pve2`.
2. Wait 30 seconds for cloud-init and QEMU guest agents to initialize.
3. Run Ansible to expand root disks, configure PostgreSQL 16, apply all 7 Alembic migrations, and start `personaos-api` and `personaos-worker` systemd services.
4. Verify HTTP health status against `http://192.168.11.246:8000/health`.

---

### Step-by-Step Manual Deployment

#### Step 1: Terraform VM Provisioning

```bash
cd deploy/iac/terraform

# 1. Copy variable template if customizing
cp terraform.tfvars.example terraform.tfvars

# 2. Initialize provider
terraform init

# 3. Review execution plan
terraform plan

# 4. Provision VMs on PVE2
terraform apply -auto-approve
```

#### Step 2: Ansible Configuration & Orchestration

```bash
cd deploy/iac/ansible

# 1. Verify SSH connectivity to provisioned nodes
ansible personaos_cluster -m ping

# 2. Expand root disk partitions (growpart + pvresize + lvextend)
ansible-playbook -i inventory/hosts.ini playbooks/disk-resize.yml

# 3. Install prerequisites (Python 3.12, build tools, PostgreSQL 16)
ansible-playbook -i inventory/hosts.ini playbooks/install-prereqs.yml

# 4. Configure PostgreSQL database and cluster access
ansible-playbook -i inventory/hosts.ini playbooks/setup-database.yml

# 5. Deploy PersonaOS runtime, apply migrations, and launch services
ansible-playbook -i inventory/hosts.ini playbooks/setup-personaos.yml
```

---

## 5. Verification & Health Probes

```bash
# Verify Control Plane API health
curl -s http://192.168.11.246:8000/health | jq .

# Verify cluster node membership
curl -s http://192.168.11.246:8000/api/v1/cluster/nodes | jq .

# Access Web Dashboard
# Navigate to: http://192.168.11.246:8000/dashboard
```

### Inspecting Systemd Services on Nodes

```bash
# On personaos-cp01 (Control Plane)
ssh user@192.168.11.246 "sudo systemctl status personaos-api"
ssh user@192.168.11.246 "journalctl -u personaos-api -f"

# On personaos-worker01 (Worker Runner)
ssh user@192.168.11.247 "sudo systemctl status personaos-worker"
ssh user@192.168.11.247 "journalctl -u personaos-worker -f"
```

---

## 6. Day-2 Operations & Teardown

### Graceful Worker Drain & Maintenance
```bash
curl -X POST http://192.168.11.246:8000/api/v1/cluster/nodes/node-personaos-worker01/drain \
  -H 'Content-Type: application/json' \
  -d '{"reason": "routine_maintenance"}'
```

### VM Teardown & Cleanup
```bash
cd deploy/iac/ansible
./run.sh destroy
```
