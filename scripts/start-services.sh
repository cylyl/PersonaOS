#!/usr/bin/env bash
set -euo pipefail

echo "Starting PersonaOS services via systemd..."
sudo systemctl start personaos.service

echo "Checking status..."
sudo systemctl status personaos-api personaos-socat-ipv6 personaos-worker@worker-devops-01 personaos-worker@se personaos-worker@worker-dev-01 --no-pager
