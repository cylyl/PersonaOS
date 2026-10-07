#!/bin/bash
# Backup OpenClaw workspace
# Usage: ./backup-openclaw.sh [--verify]

set -euo pipefail

BACKUP_DIR="/root/.openclaw/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
FILENAME="openclaw_backup_${TIMESTAMP}.tar.gz"
DEST="${BACKUP_DIR}/${FILENAME}"
OW_DIR="/root/.openclaw/workspace/oc-tools"

# Verify we're in oc-tools
if [[ "$(basename "$OW_DIR")" != "oc-tools" ]] && [[ "$(pwd)" != "$OW_DIR" ]]; then
    echo "⚠️  Run this script from within the oc-tools directory or adjust OW_DIR"
    exit 1
fi

# Change to openclaw root (not workspace - config/plugins/memory are at root level)
cd /root/.openclaw

mkdir -p "$BACKUP_DIR"

echo "📦 Creating OpenClaw backup: $FILENAME"

tar -czvf "$DEST" \
    --exclude='node_modules' \
    --exclude='.git' \
    --exclude='*.pyc' \
    --exclude='__pycache__' \
    .env \
    openclaw.json \
    config/ \
    data/ \
    plugins/ \
    memory/ \
    credentials/ \
    extensions/ \
    agents/ \
    flows/ \
    tasks/ \
    2>/dev/null || true

BACKUP_SIZE=$(du -h "$DEST" | cut -f1)
echo "✅ Backup created: $DEST ($BACKUP_SIZE)"

# Optional verify
if [[ "${1:-}" == "--verify" ]]; then
    echo "🔍 Verifying backup contents..."
    tar -tzf "$DEST" | head -20
fi

# Cleanup old backups (keep last 7)
cd "$BACKUP_DIR"
ls -t openclaw_backup_*.tar.gz 2>/dev/null | tail -n +8 | xargs rm -f 2>/dev/null || true

echo "🗑️  Old backups cleaned (kept last 7)"
echo "📋 $(ls -1 openclaw_backup_*.tar.gz 2>/dev/null | wc -l) backup(s) total"
