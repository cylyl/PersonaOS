#!/usr/bin/env bash
set -euo pipefail

echo "Stopping PersonaOS services..."
sudo systemctl stop personaos-worker@worker-dev-01 personaos-worker@se personaos-worker@worker-devops-01 personaos-socat-ipv6 personaos-api personaos.service
echo "All PersonaOS services stopped."
