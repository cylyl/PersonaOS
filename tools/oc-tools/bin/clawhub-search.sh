#!/bin/bash

# ==============================================================================
# ClawHub Skill Search Script
# Search and browse skills from clawhub.ai
# ==============================================================================

# Determine script directory and workspace root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(dirname "$SCRIPT_DIR")"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# Ensure OpenClaw is available
if ! command -v openclaw &> /dev/null; then
    echo -e "${RED}Error: OpenClaw CLI not found.${NC}"
    exit 1
fi

function show_help() {
    echo -e "${BOLD}ClawHub Skill Search${NC}"
    echo ""
    echo "Usage:"
    echo "  $0 <query>                    Search for skills"
    echo "  $0 list                       List all available skills"
    echo "  $0 info <name>                Show detailed info for a skill"
    echo "  $0 install <name>             Install a skill"
    echo "  $0 categories                List skill categories"
    echo ""
    echo "Examples:"
    echo "  $0 kubernetes                 Search for Kubernetes skills"
    echo "  $0 info ansible               Show ansible skill details"
    echo "  $0 install ansible-db-hardening"
    echo ""
    echo "Categories: kubernetes, docker, ansible, aws, azure, gcp, gitops, monitoring"
}

function search_skills() {
    local query="$1"
    if [ -z "$query" ]; then
        echo -e "${RED}Error: Search query required.${NC}"
        echo "Usage: $0 <query>"
        exit 1
    fi
    
    echo -e "${GREEN}${BOLD}Searching ClawHub for:${NC} $query"
    echo ""
    
    # Use openclaw skills search
    openclaw skills search "$query"
}

function list_all_skills() {
    echo -e "${GREEN}${BOLD}Available Skills on ClawHub${NC}"
    echo ""
    
    openclaw skills list
}

function show_skill_info() {
    local name="$1"
    if [ -z "$name" ]; then
        echo -e "${RED}Error: Skill name required.${NC}"
        echo "Usage: $0 info <skill-name>"
        exit 1
    fi
    
    echo -e "${GREEN}${BOLD}Skill Details:${NC} $name"
    echo ""
    
    openclaw skills info "$name" 2>/dev/null || \
    echo -e "${YELLOW}Skill '$name' not found locally.${NC}"
    echo ""
    echo -e "${CYAN}To install:${NC} $0 install $name"
}

function install_skill() {
    local name="$1"
    if [ -z "$name" ]; then
        echo -e "${RED}Error: Skill name required.${NC}"
        echo "Usage: $0 install <skill-name>"
        exit 1
    fi
    
    echo -e "${GREEN}Installing skill:${NC} $name"
    echo ""
    
    if [ -d "$WORKSPACE_ROOT/skills/$name" ]; then
        echo -e "${YELLOW}Skill '$name' is already installed.${NC}"
        return 0
    fi
    
    openclaw skills install "$name"
}

function show_categories() {
    echo -e "${GREEN}${BOLD}Skill Categories on ClawHub${NC}"
    echo ""
    echo "Popular categories:"
    echo "  ${CYAN}kubernetes${NC}   - K8s, K3s, Helm, Ingress"
    echo "  ${CYAN}docker${NC}      - Container, Docker Compose, Registry"
    echo "  ${CYAN}ansible${NC}     - Ansible roles, automation"
    echo "  ${CYAN}cloud${NC}       - AWS, Azure, GCP"
    echo "  ${CYAN}gitops${NC}      - ArgoCD, Flux, GitOps"
    echo "  ${CYAN}monitoring${NC}  - Prometheus, Grafana, Loki"
    echo "  ${CYAN}database${NC}    - PostgreSQL, MySQL, Redis, MongoDB"
    echo "  ${CYAN}security${NC}    - Vault, Cert-manager, Security scanning"
    echo "  ${CYAN}ci-cd${NC}       - GitHub Actions, Jenkins, GitLab CI"
    echo ""
    echo "Search by category: $0 <category-name>"
}

# Main command routing
case "$1" in
    list)
        list_all_skills
        ;;
    info)
        show_skill_info "$2"
        ;;
    install)
        install_skill "$2"
        ;;
    categories|category)
        show_categories
        ;;
    help|--help|-h)
        show_help
        ;;
    "")
        show_help
        ;;
    *)
        search_skills "$1"
        ;;
esac
