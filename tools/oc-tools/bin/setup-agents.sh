#!/bin/bash

# ==============================================================================
# OpenClaw Agent Setup Script - cylyl-labs
# Creates agents with per-agent tools and skills configuration
# ==============================================================================

set -e

# Determine script directory and workspace root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE="$(dirname "$(dirname "$SCRIPT_DIR")")"
AGENTS_DIR="/root/.openclaw/agents"
CONFIG_FILE="$WORKSPACE/oc-tools/workflows/devops/config.json"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${BLUE}==============================================${NC}"
echo -e "${BLUE}OpenClaw Agent Setup${NC}"
echo -e "${BLUE}==============================================${NC}"

# ------------------------------------------------------------------------------
# Default Agent Definitions (fallback if config.json not found)
# Each agent: name|role|tools_profile|skills (comma-separated)
# ------------------------------------------------------------------------------

declare -a DEFAULT_AGENTS=(
    "CEO|Overall leadership and strategy|full|leadership,strategy"
    "Operations|Daily operations and logistics|full|management,operations,gakkiismywife-recruiter-assistant"
    "CTO|Technical strategy and architecture|full|architecture,technical-leadership,dennis-da-menace-agent-memory,elite-longterm-memory"
    "DBA|Database administration and hardening|full|ansible-db-hardening,database-admin"
    "Software_Engineer|Software development and code implementation|full|coding-agent,github,bastos-conventional-commits,arnarsson-git-essentials"
    "DevOps|Infrastructure and CI/CD operations|full|tkuehnl-kube-medic,tkuehnl-agentic-devops,pals-software-azure-devops,cougz-arcane-docker-manager"
    "QA|Testing and quality assurance|full|testing,automation,sanguineseal-aegis-audit,bluesbell-ai-act-risk-check"
)

# Track installed skills to avoid reinstalling
declare -A INSTALLED_SKILLS

# ------------------------------------------------------------------------------
# Load agents from workflow config.json
# ------------------------------------------------------------------------------

declare -a AGENTS_FROM_CONFIG=()

load_agents_from_config() {
    if [ ! -f "$CONFIG_FILE" ]; then
        echo -e "${YELLOW}Warning: Config file not found at $CONFIG_FILE${NC}"
        echo "Using default agent definitions."
        return 1
    fi
    
    echo -e "${GREEN}Loading agents from: $CONFIG_FILE${NC}"
    
    # Use python3 to parse JSON and extract agent definitions
    local agents_json=$(python3 -c "
import json
with open('$CONFIG_FILE') as f:
    config = json.load(f)

agents = config.get('agents', {})
for agent_id, agent_data in agents.items():
    role = agent_data.get('role', 'Unknown')
    skills = ','.join(agent_data.get('skills', []))
    sends_to = ','.join(agent_data.get('sends_to', []))
    receives_from = ','.join(agent_data.get('receives_from', []))
    print(f'{agent_id}|{role}|full|{skills}|{sends_to}|{receives_from}')
" 2>/dev/null)
    
    if [ -z "$agents_json" ]; then
        echo -e "${YELLOW}Warning: Failed to parse agents from config${NC}"
        return 1
    fi
    
    # Convert to array
    while IFS= read -r line; do
        AGENTS_FROM_CONFIG+=("$line")
    done <<< "$agents_json"
    
    echo -e "${GREEN}Loaded ${#AGENTS_FROM_CONFIG[@]} agents from config${NC}"
    return 0
}

# ------------------------------------------------------------------------------
# Functions
# ------------------------------------------------------------------------------

install_skill_if_needed() {
    local skill="$1"
    
    # Skip if already installed this session
    if [[ -n "${INSTALLED_SKILLS[$skill]}" ]]; then
        return 0
    fi
    
    # Check if skill directory already exists in workspace
    if [ -d "$WORKSPACE/skills/$skill" ]; then
        INSTALLED_SKILLS[$skill]=1
        return 0
    fi
    
    # Check if skill exists in pending-skills (local install)
    if [ -d "$WORKSPACE/pending-skills/$skill" ]; then
        echo -e "${BLUE}    Installing skill from pending-skills: $skill${NC}"
        mkdir -p "$WORKSPACE/skills/$skill"
        cp -r "$WORKSPACE/pending-skills/$skill"/* "$WORKSPACE/skills/$skill/" 2>/dev/null
        if [ $? -eq 0 ]; then
            INSTALLED_SKILLS[$skill]=1
            echo -e "${GREEN}    Skill '$skill' installed from pending-skills${NC}"
            return 0
        fi
    fi
    
    # Fall back to ClawHub
    echo -e "${BLUE}    Installing skill from ClawHub: $skill${NC}"
    if openclaw skills install "$skill" 2>/dev/null; then
        INSTALLED_SKILLS[$skill]=1
        echo -e "${GREEN}    Skill '$skill' installed successfully${NC}"
    else
        echo -e "${YELLOW}    Warning: Failed to install skill '$skill' (not found in ClawHub)${NC}"
    fi
}

configure_agent() {
    local name="$1"
    local tools_profile="$2"
    local skills="$3"
    
    echo -e "${BLUE}  Configuring agent tools and skills...${NC}"
    
    # Find agent index (required since agents.list is an array)
    local agent_idx=$(cat ~/.openclaw/openclaw.json | python3 -c "
import json, sys
d = json.load(sys.stdin)
for i, a in enumerate(d.get('agents', {}).get('list', [])):
    if a.get('id') == '$name':
        print(i)
        break
" 2>/dev/null)
    
    if [ -z "$agent_idx" ]; then
        echo -e "${RED}    Error: Could not find agent index for '$name'${NC}"
        return 1
    fi
    
    # Set tools profile using numeric index
    if [ -n "$tools_profile" ]; then
        echo -e "${GREEN}    Setting tools profile: $tools_profile${NC}"
        openclaw config set "agents.list[$agent_idx].tools.profile" "$tools_profile" 2>/dev/null || \
            echo -e "${YELLOW}    Warning: Failed to set tools profile${NC}"
    fi
    
    # Set skills (as JSON array) using numeric index
    if [ -n "$skills" ]; then
        # Convert comma-separated skills to JSON array
        local skills_json="["
        IFS=',' read -ra SKILL_ARRAY <<< "$skills"
        local comma=""
        for skill in "${SKILL_ARRAY[@]}"; do
            skill=$(echo "$skill" | xargs)  # trim whitespace
            if [ -n "$skill" ]; then
                skills_json+="$comma\"$skill\""
                comma=","
            fi
        done
        skills_json+="]"
        
        echo -e "${GREEN}    Setting skills: $skills_json${NC}"
        
        # Find agent index and set skills using numeric index
        local agent_idx=$(cat ~/.openclaw/openclaw.json | python3 -c "
import json, sys
d = json.load(sys.stdin)
for i, a in enumerate(d.get('agents', {}).get('list', [])):
    if a.get('id') == '$name':
        print(i)
        break
" 2>/dev/null)
        
        if [ -n "$agent_idx" ]; then
            openclaw config set "agents.list[$agent_idx].skills" --json "$skills_json" 2>/dev/null || \
                echo -e "${YELLOW}    Warning: Failed to set skills${NC}"
        else
            echo -e "${RED}    Error: Could not find agent index for '$name'${NC}"
        fi
    fi
}

setup_agent() {
    local name="$1"
    local role="$2"
    local tools_profile="$3"
    local skills="$4"
    local sends_to="$5"
    local receives_from="$6"
    
    echo ""
    echo -e "${GREEN}Setting up agent: $name${NC}"
    echo "  Role: $role"
    echo "  Tools Profile: $tools_profile"
    echo "  Skills: $skills"
    [ -n "$sends_to" ] && echo "  Sends to: $sends_to"
    [ -n "$receives_from" ] && echo "  Receives from: $receives_from"
    
    # Check if agent already exists (use lowercase for matching since OpenClaw normalizes IDs)
    local normalized_name=$(echo "$name" | tr '[:upper:]' '[:lower:]' | tr ' ' '_')
    local agent_exists=false
    if openclaw agents list 2>/dev/null | grep -q "^- $normalized_name "; then
        agent_exists=true
        echo -e "${YELLOW}  Agent '$name' already exists - updating configuration${NC}"
    else
        # Create agent workspace directory
        local agent_dir="$AGENTS_DIR/$normalized_name"
        mkdir -p "$agent_dir"
        
        # Create agent using CLI
        echo -e "${BLUE}  Creating agent...${NC}"
        if openclaw agents add "$name" --workspace "$agent_dir" --non-interactive 2>/dev/null; then
            echo -e "${GREEN}  Agent created successfully${NC}"
            agent_exists=false
        else
            # Agent might already exist with normalized name
            if openclaw agents list 2>/dev/null | grep -q "^- $normalized_name "; then
                echo -e "${YELLOW}  Agent '$normalized_name' already exists - updating configuration${NC}"
                agent_exists=true
            else
                echo -e "${RED}  Error: Failed to create agent '$name'${NC}"
                return 1
            fi
        fi
    fi
    
    # Configure tools and skills (always call for both new and existing agents)
    configure_agent "$normalized_name" "$tools_profile" "$skills"
    
    # Set agent identity
    local emoji="🤖"
    case "$normalized_name" in
        ceo) emoji="👔" ;;
        hr) emoji="👥" ;;
        cto) emoji="🏛️" ;;
        dba) emoji="🗄️" ;;
        software_engineer) emoji="👨‍💻" ;;
        devops) emoji="🛠️" ;;
        qa) emoji="🔍" ;;
        marketing) emoji="📢" ;;
        finance) emoji="💰" ;;
        operations) emoji="⚙️" ;;
        business_dev|it_sales) emoji="💼" ;;
    esac
    
    openclaw agents set-identity --agent "$normalized_name" --name "$name" --emoji "$emoji" 2>/dev/null || true
    
    # Install skills for this agent
    echo -e "${BLUE}  Installing agent-specific skills...${NC}"
    IFS=',' read -ra SKILL_ARRAY <<< "$skills"
    for skill in "${SKILL_ARRAY[@]}"; do
        skill=$(echo "$skill" | xargs)  # trim whitespace
        if [ -n "$skill" ]; then
            install_skill_if_needed "$skill"
        fi
    done
    
    echo -e "${GREEN}  Agent '$name' setup complete${NC}"
}

# ------------------------------------------------------------------------------
# Main Execution
# ------------------------------------------------------------------------------

echo ""
echo -e "${BLUE}--- Loading Agent Definitions ---${NC}"

# Try to load from config, fall back to defaults
if ! load_agents_from_config; then
    echo -e "${YELLOW}Falling back to default agent definitions${NC}"
    AGENTS_FROM_CONFIG=()
fi

# Use agents from config or defaults
if [ ${#AGENTS_FROM_CONFIG[@]} -gt 0 ]; then
    AGENTS=("${AGENTS_FROM_CONFIG[@]}")
else
    AGENTS=("${DEFAULT_AGENTS[@]}")
fi

echo ""
echo -e "${BLUE}--- Creating Agents with Per-Agent Tools & Skills ---${NC}"

for agent in "${AGENTS[@]}"; do
    # Handle extended format from config: name|role|tools_profile|skills|sends_to|receives_from
    # Or simple format: name|role|tools_profile|skills
    IFS='|' read -r name role tools_profile skills sends_to receives_from <<< "$agent"
    setup_agent "$name" "$role" "$tools_profile" "$skills" "${sends_to:-}" "${receives_from:-}" || true
done

echo ""
echo -e "${GREEN}==============================================${NC}"
echo -e "${GREEN}Setup Complete!${NC}"
echo -e "${GREEN}==============================================${NC}"
echo ""
echo "Installed skills: ${!INSTALLED_SKILLS[*]}"
echo ""
echo "Run 'openclaw agents list' to see all agents."
echo "Run './oc-manage.sh list-agents' for workspace management."
echo "Run './oc-manage.sh list-agent-tools <agent>' to see agent tool config."
echo "Run './oc-manage.sh list-agent-skills <agent>' to see agent skills."
echo ""
echo "Agent definitions loaded from: ${CONFIG_FILE}"
