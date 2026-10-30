#!/usr/bin/env bash
# PersonaOS IaC Automation Runner for PVE2
# Standard runner script patterned after cyl-dc
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TERRAFORM_DIR="${SCRIPT_DIR}/../terraform"
ANSIBLE_DIR="${SCRIPT_DIR}"

usage() {
  cat <<EOF
Usage: $0 [command]

Commands:
  plan           Run Terraform plan against PVE2
  apply          Run Terraform apply against PVE2 (provisions VMs)
  destroy        Destroy Terraform-managed VMs on PVE2
  bootstrap      Run full Ansible configuration and deployment playbook
  deploy         Complete end-to-end: Terraform apply + Ansible bootstrap
  status         Check PersonaOS service and health status on remote nodes
  help           Show this help message

EOF
  exit 1
}

cmd_plan() {
  echo "==> Running Terraform plan for PVE2..."
  cd "${TERRAFORM_DIR}"
  terraform init -upgrade
  terraform plan
}

cmd_apply() {
  echo "==> Provisioning PersonaOS VMs on PVE2..."
  cd "${TERRAFORM_DIR}"
  terraform init -upgrade
  terraform apply -auto-approve
  echo "==> Waiting 30s for cloud-init and QEMU guest agents to initialize..."
  sleep 30
}

cmd_destroy() {
  echo "==> Destroying PersonaOS VMs on PVE2..."
  cd "${TERRAFORM_DIR}"
  terraform destroy -auto-approve
}

cmd_bootstrap() {
  echo "==> Running Ansible deployment playbook..."
  cd "${ANSIBLE_DIR}"
  ansible-playbook -i inventory/hosts.ini playbooks/deploy.yml
}

cmd_deploy() {
  cmd_apply
  cmd_bootstrap
  cmd_status
}

cmd_status() {
  echo "==> Querying PersonaOS Control Plane Health..."
  CP_HOST=$(grep -A 1 '\[control_plane\]' "${ANSIBLE_DIR}/inventory/hosts.ini" | grep ansible_host | awk -F'=' '{print $2}')
  if [ -n "${CP_HOST}" ]; then
    curl -sf "http://${CP_HOST}:8000/health" | jq . || true
  else
    echo "Control plane host not found in inventory."
  fi
}

case "${1:-help}" in
  plan)      cmd_plan ;;
  apply)     cmd_apply ;;
  destroy)   cmd_destroy ;;
  bootstrap) cmd_bootstrap ;;
  deploy)    cmd_deploy ;;
  status)    cmd_status ;;
  help|*)    usage ;;
esac
