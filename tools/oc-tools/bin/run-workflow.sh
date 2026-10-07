#!/bin/bash
# OpenClaw Workflow Runner - Shell wrapper
# Usage: ./run-workflow.sh <project-dir> [--event <event>]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKFLOW_DIR="$SCRIPT_DIR"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

echo -e "${CYAN}"
echo "╔═══════════════════════════════════════════════════════════╗"
echo "║         OpenClaw Workflow Runner - DevOps Pipeline      ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo -e "${NC}"

# Check Node.js
if ! command -v node &> /dev/null; then
    echo -e "${RED}Error: Node.js is required but not installed.${NC}"
    exit 1
fi

# Show help if no args
if [ $# -lt 1 ]; then
    echo -e "${YELLOW}Usage:${NC} $0 <project-dir> [--event <event-name>]"
    echo ""
    echo "Options:"
    echo "  <project-dir>    Path to the project directory"
    echo "  --event <name>   Start from a specific event (skip earlier steps)"
    echo ""
    echo "Examples:"
    echo "  $0 /root/.openclaw/workspace/projects/dining-feedback"
    echo "  $0 /root/.openclaw/workspace/projects/dining-feedback --event build_done"
    exit 1
fi

PROJECT_DIR="$1"
shift

EVENT_FLAG=""
if [ "$1" = "--event" ]; then
    EVENT_FLAG="--event"
    EVENT_NAME="$2"
    shift 2
fi

# Run the workflow runner
echo -e "${YELLOW}Starting workflow for:${NC} $PROJECT_DIR"
if [ -n "$EVENT_FLAG" ]; then
    echo -e "${YELLOW}Starting from event:${NC} $EVENT_NAME"
fi
echo ""

node "$WORKFLOW_DIR/workflow-runner.js" "$PROJECT_DIR" $EVENT_FLAG "$EVENT_NAME"

exit $?