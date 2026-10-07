#!/bin/bash

# ==============================================================================
# PPT Creation Workflow Manager
# Apply and manage routing + event-driven workflow for multi-agent pipeline
# ==============================================================================

# Determine script directory and workspace root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKFLOW_DIR="$SCRIPT_DIR"
CONFIG_FILE="$WORKFLOW_DIR/config.json"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

function show_help() {
    echo -e "${BOLD}PPT Creation Workflow Manager${NC}"
    echo ""
    echo "Usage: $0 <command>"
    echo ""
    echo "Commands:"
    echo "  status        Show current workflow status"
    echo "  list-agents   List configured workflow agents"
    echo "  list-routes   List routing rules"
    echo "  list-events   List event definitions"
    echo "  settings      Show workflow settings"
    echo "  validate      Validate configuration files"
    echo "  apply         Apply workflow to OpenClaw (creates bindings)"
    echo "  diagram       Generate ASCII flow diagram"
    echo "  help          Show this help"
    echo ""
}

function load_config() {
    if [ ! -f "$CONFIG_FILE" ]; then
        echo -e "${RED}Error: Config file not found at $CONFIG_FILE${NC}"
        exit 1
    fi
}

function cmd_status() {
    echo -e "${GREEN}${BOLD}PPT Creation Workflow Status${NC}"
    echo ""
    
    load_config
    
    # Count agents, routes, events using python3 for proper JSON parsing
    local agent_count=$(python3 -c "
import json
with open('$CONFIG_FILE') as f:
    config = json.load(f)
print(len(config.get('agents', {})))
" 2>/dev/null)
    
    local route_count=$(python3 -c "
import json
with open('$CONFIG_FILE') as f:
    config = json.load(f)
print(len(config.get('routing', [])))
" 2>/dev/null)
    
    local event_count=$(python3 -c "
import json
with open('$CONFIG_FILE') as f:
    config = json.load(f)
print(len(config.get('events', [])))
" 2>/dev/null)
    
    echo "Agents configured: $agent_count"
    echo "Routing rules: $route_count"
    echo "Events defined: $event_count"
    echo ""
    
    # Show workflow settings
    echo -e "${CYAN}Workflow Settings:${NC}"
    python3 -c "
import json
with open('$CONFIG_FILE') as f:
    config = json.load(f)
settings = config.get('settings', {})
loop = settings.get('loop', False)
max_iter = settings.get('max_iterations', 'unlimited')
stop_on = ', '.join(settings.get('stop_on', [])) or 'none'
print(f'  Loop enabled:     {loop}')
print(f'  Max iterations:   {max_iter}')
print(f'  Stop on events:   {stop_on}')
" 2>/dev/null
    echo ""
    
    # Check which workflow agents exist in OpenClaw
    echo -e "${CYAN}Checking agent availability in OpenClaw...${NC}"
    
    # Get list of workflow agent IDs from config
    local workflow_agents=$(python3 -c "
import json
with open('$CONFIG_FILE') as f:
    config = json.load(f)
print(' '.join(config.get('agents', {}).keys()))
" 2>/dev/null)
    
    local available=0
    local total=0
    for agent in $workflow_agents; do
        ((total++))
        if openclaw agents list 2>/dev/null | grep -q "^- $agent "; then
            ((available++))
        fi
    done
    
    echo "Workflow agents found in OpenClaw: $available / $total"
    echo ""
    
    if [ "$available" -eq "$total" ] && [ "$total" -gt 0 ]; then
        echo -e "${GREEN}✓ All workflow agents are available${NC}"
    else
        echo -e "${YELLOW}⚠ Some workflow agents are missing in OpenClaw${NC}"
        echo "  Run ./setup-agents.sh to create missing agents"
    fi
}

function cmd_list_agents() {
    echo -e "${GREEN}${BOLD}Workflow Agents${NC}"
    echo ""
    
    load_config
    
    echo "| Agent | Role | Skills |"
    echo "|-------|------|--------|"
    
    python3 -c "
import json
with open('$CONFIG_FILE') as f:
    config = json.load(f)

agents = config.get('agents', {})
for agent_id, agent_data in agents.items():
    role = agent_data.get('role', 'Unknown')
    skills = ','.join(agent_data.get('skills', []))
    sends_to = ','.join(agent_data.get('sends_to', []))
    receives_from = ','.join(agent_data.get('receives_from', []))
    print(f'{agent_id}|{role}|{skills}')
" 2>/dev/null | while IFS='|' read -r agent role skills; do
        echo "| $agent | $role | $skills |"
    done
}

function cmd_list_routes() {
    echo -e "${GREEN}${BOLD}Routing Rules${NC}"
    echo ""
    
    if [ -f "$WORKFLOW_DIR/routing.json" ]; then
        echo "| From | To | Description |"
        echo "|------|----|-------------|"
        grep -E '"from"|"to"|"description"' "$WORKFLOW_DIR/routing.json" | paste - - - | sed 's/"//g' | sed 's/,//g' | sed 's/  */ /g' | head -20
    else
        echo "routing.json not found"
    fi
}

function cmd_list_events() {
    echo -e "${GREEN}${BOLD}Event Definitions${NC}"
    echo ""
    
    if [ -f "$WORKFLOW_DIR/events.json" ]; then
        echo "| Event | Emitter | Consumers | Description |"
        echo "|-------|---------|-----------|-------------|"
        grep -E '"name"|"emit_by"|"consumed_by"|"description"' "$WORKFLOW_DIR/events.json" | paste - - - - | sed 's/"//g' | sed 's/,/:/g' | sed 's/  */ /g' | head -20
    else
        echo "events.json not found"
    fi
}

function cmd_settings() {
    echo -e "${GREEN}${BOLD}Workflow Settings${NC}"
    echo ""
    
    load_config
    
    python3 -c "
import json
with open('$CONFIG_FILE') as f:
    config = json.load(f)

settings = config.get('settings', {})
loop = settings.get('loop', False)
max_iter = settings.get('max_iterations', 'unlimited')
stop_on = settings.get('stop_on', [])

print(f'Loop enabled:    {loop}')
print(f'Max iterations: {max_iter}')
print()
print('Stop on events:')
if stop_on:
    for event in stop_on:
        print(f'  - {event}')
else:
    print('  (none - runs until stopped)')
print()

# Show agents and their connections
print('Agent routing:')
agents = config.get('agents', {})
for agent_id, agent_data in agents.items():
    sends = ', '.join(agent_data.get('sends_to', [])) or 'none'
    receives = ', '.join(agent_data.get('receives_from', [])) or 'none'
    print(f'  {agent_id}:')
    print(f'    sends_to:      {sends}')
    print(f'    receives_from: {receives}')
" 2>/dev/null
}

function cmd_validate() {
    echo -e "${GREEN}${BOLD}Validating Workflow Configuration${NC}"
    echo ""
    
    local errors=0
    
    # Check config file exists
    if [ ! -f "$CONFIG_FILE" ]; then
        echo -e "${RED}✗ config.json not found${NC}"
        ((errors++))
    else
        echo -e "${GREEN}✓ config.json exists${NC}"
    fi
    
    # Check routing.json exists
    if [ ! -f "$WORKFLOW_DIR/routing.json" ]; then
        echo -e "${RED}✗ routing.json not found${NC}"
        ((errors++))
    else
        echo -e "${GREEN}✓ routing.json exists${NC}"
    fi
    
    # Check events.json exists
    if [ ! -f "$WORKFLOW_DIR/events.json" ]; then
        echo -e "${RED}✗ events.json not found${NC}"
        ((errors++))
    else
        echo -e "${GREEN}✓ events.json exists${NC}"
    fi
    
    # Validate JSON syntax
    if command -v python3 &> /dev/null; then
        for f in "$CONFIG_FILE" "$WORKFLOW_DIR/routing.json" "$WORKFLOW_DIR/events.json"; do
            if [ -f "$f" ]; then
                python3 -c "import json; json.load(open('$f'))" 2>/dev/null
                if [ $? -eq 0 ]; then
                    echo -e "${GREEN}✓ $f is valid JSON${NC}"
                else
                    echo -e "${RED}✗ $f has invalid JSON${NC}"
                    ((errors++))
                fi
            fi
        done
    fi
    
    echo ""
    if [ $errors -eq 0 ]; then
        echo -e "${GREEN}All validations passed${NC}"
    else
        echo -e "${RED}$errors validation(s) failed${NC}"
    fi
}

function cmd_apply() {
    echo -e "${GREEN}${BOLD}Applying PPT Creation Workflow${NC}"
    echo ""
    
    echo -e "${YELLOW}Note: This workflow uses routing and events patterns.${NC}"
    echo "Currently OpenClaw does not have native routing/events config."
    echo ""
    echo "This script will:"
    echo "1. Validate the configuration"
    echo "2. Show how to manually configure agent bindings"
    echo "3. Provide instructions for event-driven automation"
    echo ""
    
    read -p "Continue? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Aborted."
        return
    fi
    
    cmd_validate
    
    echo ""
    echo -e "${CYAN}To enable event-driven workflow, you can:${NC}"
    echo ""
    echo "1. Bind agents to channels for human oversight:"
    echo "   openclaw agents bind <agent> --channel whatsapp"
    echo ""
    echo "2. Use hooks for automation:"
    echo "   - session:end → trigger next agent in pipeline"
    echo "   - hook:event → emit event to consuming agents"
    echo ""
    echo "3. Use cron jobs for scheduled pipeline runs"
    echo ""
    echo -e "${GREEN}Workflow configuration is ready at:${NC}"
    echo "  $CONFIG_FILE"
}

function cmd_diagram() {
    echo -e "${GREEN}${BOLD}PPT Creation Flow Diagram${NC}"
    echo ""
    cat << 'EOF'
                         ┌─────────────────────────────────────────┐
                         │              CTO (Architect)             │
                         │   spec_ready  │  code_review_requested  │
                         └───────────────┬─────────────────────────┘
                                         │ spec_ready
                                         ▼
                         ┌─────────────────────────────────────────┐
                         │         SOFTWARE_ENGINEER (Builder)      │
                         │  build_done │ db_review │ code_review   │
                         └──────┬──────┬───────────┬───────────────┘
                                │      │           │
                                │      │           ▼
                                │      │    ┌──────────┐
                                │      │    │   DBA   │
                                │      │    │db_approved
                                │      │    └────┬─────┘
                                │      │         │
                                ▼      ▼         ▼
                         ┌─────────────────────────────────────────┐
                         │              DEVOPS                     │
                         │        deploy_ready │ deployed         │
                         └───────────────┬─────────────────────────┘
                                         │
                                         ▼
                         ┌─────────────────────────────────────────┐
                         │                 QA                      │
                         │   tests_passed │ tests_failed │ approved │
                         └───────┬────────┬────────────────────────┘
                                 │        │
            ┌────────────────────┘        └────────────────────┐
            │                                                │
            ▼                                                ▼
    ┌───────────────┐                              ┌─────────────────┐
    │   BUG LOOP    │                              │   OPERATIONS    │
    │               │                              │  deployed_prod  │
    │ soft_eng ◀────┤                              │ hotfix_requested
    └───────────────┘                              └────────┬────────┘
                                                               │
                                                               ▼
                                                      ┌──────────────┐
                                                      │   HOTFIX     │
                                                      │ devops → qa  │
                                                      └──────────────┘

LEGEND:
  ──── Normal Flow ────
  ─ ─ ─ Bug Loop ─ ─ ─
  ····· Hotfix ······
EOF
}

# Main command routing
case "$1" in
    status)
        cmd_status
        ;;
    list-agents)
        cmd_list_agents
        ;;
    list-routes)
        cmd_list_routes
        ;;
    list-events)
        cmd_list_events
        ;;
    settings)
        cmd_settings
        ;;
    validate)
        cmd_validate
        ;;
    apply)
        cmd_apply
        ;;
    diagram)
        cmd_diagram
        ;;
    help|--help|-h|"")
        show_help
        ;;
    *)
        echo -e "${RED}Unknown command: $1${NC}"
        show_help
        exit 1
        ;;
esac
