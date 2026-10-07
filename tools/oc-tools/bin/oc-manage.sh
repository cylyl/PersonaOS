#!/bin/bash

# ==============================================================================
# OpenClaw Workspace Management Script - cylyl-labs
# Based on AGENTS.md configuration and OpenClaw CLI specifications.
# ==============================================================================

# Determine script directory and workspace root
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE_ROOT="$(cd "$(dirname "$(dirname "$SCRIPT_DIR")")" && pwd)"
WORKSPACE="$WORKSPACE_ROOT"
SKILLS_DIR="$WORKSPACE/skills"
PENDING_SKILLS_DIR="$WORKSPACE/pending-skills"
CONFIG_FILE="$HOME/.openclaw/openclaw.json"

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Ensure we are in the workspace root
if [ ! -f "$WORKSPACE_ROOT/AGENTS.md" ] && [ "$1" != "help" ]; then
    echo -e "${RED}Error: AGENTS.md not found in workspace root.${NC}"
    exit 1
fi

# Change to workspace root for OpenClaw operations
cd "$WORKSPACE_ROOT"

# ------------------------------------------------------------------------------
# HELPER FUNCTIONS
# ------------------------------------------------------------------------------

function json_escape() {
    printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'
}

function jq_set() {
    local path="$1"
    local value="$2"
    local temp_file="/tmp/openclaw_config_$$.json"
    cp "$CONFIG_FILE" "$temp_file"
    python3 << PYEOF
import json, sys
with open("$temp_file", 'r') as f:
    d = json.load(f)

path = "$path".split('.')
val = json.loads('''$value''')

# Navigate to parent
obj = d
for p in path[:-1]:
    if p.isdigit():
        obj = obj[int(p)]
    else:
        obj = obj.setdefault(p, {})

# Set value
key = path[-1]
if key.isdigit():
    obj[int(key)] = val
else:
    obj[key] = val

with open("$CONFIG_FILE", 'w') as f:
    json.dump(d, f, indent=2)
PYEOF
    rm -f "$temp_file"
}

function jq_get() {
    local path="$1"
    python3 << PYEOF
import json, sys
with open("$CONFIG_FILE", 'r') as f:
    d = json.load(f)

path = "$path".split('.')
obj = d
for p in path:
    if p.isdigit():
        obj = obj[int(p)]
    else:
        obj = obj.get(p, {})

print(json.dumps(obj, indent=2) if isinstance(obj, dict) else obj)
PYEOF
}

function restart_gateway() {
    echo -e "${CYAN}Restarting gateway...${NC}"
    openclaw gateway restart 2>/dev/null || echo -e "${YELLOW}Gateway restart requested.${NC}"
}

# ------------------------------------------------------------------------------
# USAGE
# ------------------------------------------------------------------------------

function show_usage() {
    echo -e "${BLUE}OpenClaw Management Toolkit${NC}"
    echo ""
    echo -e "${GREEN}Agent Management:${NC}"
    echo "  ./oc-manage.sh list-agents                               List all agents"
    echo "  ./oc-manage.sh add-agent <name> [--model <model>]       Create a new agent"
    echo "  ./oc-manage.sh delete-agent <name>                       Delete an agent"
    echo ""
    echo -e "${GREEN}Channel Binding (WhatsApp-style):${NC}"
    echo "  ./oc-manage.sh bind <name> <channel>                     Bind agent to channel"
    echo "  ./oc-manage.sh unbind <name> <channel>                   Unbind agent from channel"
    echo ""
    echo -e "${GREEN}Telegram Binding (Account-style):${NC}"
    echo "  ./oc-manage.sh bind-telegram <agent> <accountId> [--bot-token <token>] [--dm-policy pairing|allowlist|open]"
    echo "  ./oc-manage.sh unbind-telegram <agent>                   Remove Telegram binding"
    echo "  ./oc-manage.sh list-telegram-accounts                    List configured Telegram accounts"
    echo "  ./oc-manage.sh add-telegram-account <accountId> <botToken> [--dm-policy] [--allow-from <chat_ids>]"
    echo "  ./oc-manage.sh remove-telegram-account <accountId>       Remove Telegram account"
    echo ""
    echo -e "${GREEN}Status & Health:${NC}"
    echo "  ./oc-manage.sh status                                    Show OpenClaw status"
    echo "  ./oc-manage.sh health                                    Show gateway health"
    echo "  ./oc-manage.sh bindings                                  Show all bindings"
    echo ""
    echo -e "${GREEN}Agent Tools (on/off per agent):${NC}"
    echo "  ./oc-manage.sh enable-tool <agent> <tool>                Enable a tool for agent"
    echo "  ./oc-manage.sh disable-tool <agent> <tool>               Disable a tool for agent"
    echo "  ./oc-manage.sh set-tools-profile <agent> <profile>       Set tools profile"
    echo "  ./oc-manage.sh list-agent-tools <agent>                   Show agent tool config"
    echo ""
    echo -e "${GREEN}Agent Skills (on/off per agent):${NC}"
    echo "  ./oc-manage.sh enable-skill <agent> <skill>              Enable skill for agent"
    echo "  ./oc-manage.sh disable-skill <agent> <skill>             Disable skill for agent"
    echo "  ./oc-manage.sh list-agent-skills <agent>                  Show agent skills"
    echo ""
    echo -e "${GREEN}Global Skill Management:${NC}"
    echo "  ./oc-manage.sh list-skills                               List all available skills"
    echo "  ./oc-manage.sh search-skills <query>                      Search ClawHub for skills"
    echo "  ./oc-manage.sh install-skill <slug>                       Install skill from pending-skills/"
    echo "  ./oc-manage.sh install-all-pending                       Install all pending skills"
    echo "  ./oc-manage.sh remove-skill <name>                       Remove a skill from workspace"
    echo "  ./oc-manage.sh skill-info <name>                         Show skill details"
    echo ""
    echo -e "${GREEN}Agent-to-Agent Communication:${NC}"
    echo "  ./oc-manage.sh agent-to-agent-status                     Show agentToAgent config"
    echo "  ./oc-manage.sh enable-agent-to-agent                     Enable cross-agent calls"
    echo ""
    echo -e "${GREEN}Help:${NC}"
    echo "  ./oc-manage.sh help                                      Show this help"
}

# ------------------------------------------------------------------------------
# AGENT MANAGEMENT
# ------------------------------------------------------------------------------

function list_agents() {
    echo -e "${GREEN}Listing all agents...${NC}"
    openclaw agents list
}

function add_agent() {
    local name="$1"
    local model="$2"
    echo -e "${GREEN}Adding agent: $name...${NC}"
    if [ -n "$model" ]; then
        openclaw agents add "$name" --workspace "/root/.openclaw/agents/$name" --model "$model"
    else
        openclaw agents add "$name" --workspace "/root/.openclaw/agents/$name"
    fi
}

function delete_agent() {
    local name="$1"
    echo -e "${GREEN}Deleting agent: $name...${NC}"
    openclaw agents delete "$name" --force
}

# ------------------------------------------------------------------------------
# BINDING (Legacy WhatsApp-style)
# ------------------------------------------------------------------------------

function bind_channel() {
    local name="$1"
    local channel="$2"
    echo -e "${GREEN}Binding $name to channel: $channel...${NC}"
    openclaw agents bind --agent "$name" --bind "$channel"
}

function unbind_channel() {
    local name="$1"
    local channel="$2"
    echo -e "${GREEN}Unbinding $name from channel: $channel...${NC}"
    openclaw agents unbind --agent "$name" --bind "$channel"
}

# ------------------------------------------------------------------------------
# TELEGRAM BINDING (Account-style)
# ------------------------------------------------------------------------------

# Parse --allow-from comma-separated values into JSON array
function parse_allow_from() {
    local input="$1"
    if [ -z "$input" ]; then
        echo "[]"
    else
        echo "$input" | awk -F',' '{for(i=1;i<=NF;i++) printf "\"%s\",", $i}' | sed 's/,$//' | awk 'BEGIN{printf "["} {printf} END{printf "]"}'
    fi
}

function bind_telegram() {
    local agent="$1"
    local account_id="$2"
    shift 2
    
    local bot_token=""
    local dm_policy="pairing"
    local allow_from=""
    
    while [ $# -gt 0 ]; do
        case "$1" in
            --bot-token)
                bot_token="$2"
                shift 2
                ;;
            --dm-policy)
                dm_policy="$2"
                shift 2
                ;;
            --allow-from)
                allow_from="$2"
                shift 2
                ;;
            *)
                shift
                ;;
        esac
    done
    
    if [ -z "$agent" ] || [ -z "$account_id" ]; then
        echo -e "${RED}Error: Agent and accountId required${NC}"
        echo "Usage: bind-telegram <agent> <accountId> [--bot-token <token>] [--dm-policy pairing|allowlist|open]"
        exit 1
    fi
    
    echo -e "${GREEN}Binding agent '$agent' to Telegram account '$account_id'...${NC}"
    
    # If bot token provided, ensure account exists
    if [ -n "$bot_token" ]; then
        local allow_from_json="[]"
        if [ -n "$allow_from" ]; then
            allow_from_json="[$(echo "$allow_from" | sed 's/,/","/g')]"
        fi
        
        python3 << PYEOF
import json

with open("$CONFIG_FILE", 'r') as f:
    d = json.load(f)

# Ensure channels.telegram.accounts exists
if 'channels' not in d:
    d['channels'] = {}
if 'telegram' not in d['channels']:
    d['channels']['telegram'] = {'enabled': True}
if 'accounts' not in d['channels']['telegram']:
    d['channels']['telegram']['accounts'] = {}

# Add/update the account
d['channels']['telegram']['accounts']['$account_id'] = {
    'botToken': '$bot_token',
    'dmPolicy': '$dm_policy'
}
if '$allow_from' != '':
    d['channels']['telegram']['accounts']['$account_id']['allowFrom'] = $(echo "[$(
        echo "$allow_from" | sed 's/,/","/g'
    )]" | python3 -c 'import json,sys; print(json.dumps([x for x in sys.stdin.read().strip("[]").split(",") if x]))' 2>/dev/null || echo "[]")

with open("$CONFIG_FILE", 'w') as f:
    json.dump(d, f, indent=2)
PYEOF
    fi
    
    # Add binding
    python3 << PYEOF
import json

with open("$CONFIG_FILE", 'r') as f:
    d = json.load(f)

if 'bindings' not in d:
    d['bindings'] = []

# Remove existing binding for this agent if exists
d['bindings'] = [b for b in d.get('bindings', []) if b.get('agentId') != '$agent']

# Add new binding
d['bindings'].append({
    'agentId': '$agent',
    'match': {
        'channel': 'telegram',
        'accountId': '$account_id'
    }
})

with open("$CONFIG_FILE", 'w') as f:
    json.dump(d, f, indent=2)
PYEOF
    
    echo -e "${GREEN}✓ Agent '$agent' bound to Telegram account '$account_id'${NC}"
    restart_gateway
}

function unbind_telegram() {
    local agent="$1"
    
    if [ -z "$agent" ]; then
        echo -e "${RED}Error: Agent name required${NC}"
        echo "Usage: unbind-telegram <agent>"
        exit 1
    fi
    
    echo -e "${GREEN}Removing Telegram binding for agent '$agent'...${NC}"
    
    python3 << PYEOF
import json

with open("$CONFIG_FILE", 'r') as f:
    d = json.load(f)

# Remove Telegram bindings for this agent
d['bindings'] = [
    b for b in d.get('bindings', []) 
    if b.get('agentId') != '$agent' or b.get('match', {}).get('channel') != 'telegram'
]

with open("$CONFIG_FILE", 'w') as f:
    json.dump(d, f, indent=2)
PYEOF
    
    echo -e "${GREEN}✓ Telegram binding removed for '$agent'${NC}"
    restart_gateway
}

function list_telegram_accounts() {
    echo -e "${GREEN}Telegram Accounts:${NC}"
    
    python3 << 'PYEOF'
import json

with open("/root/.openclaw/openclaw.json", 'r') as f:
    d = json.load(f)

telegram = d.get('channels', {}).get('telegram', {})
accounts = telegram.get('accounts', {})

if not accounts:
    print("  No Telegram accounts configured")
else:
    for name, config in accounts.items():
        token = config.get('botToken', '')
        masked = token[:20] + "..." if len(token) > 20 else token
        dm = config.get('dmPolicy', 'pairing')
        allow_from = config.get('allowFrom', [])
        print(f"  {name}: {masked} (dmPolicy: {dm})")
        if allow_from:
            print(f"           allowFrom: {allow_from}")
PYEOF
}

function add_telegram_account() {
    local account_id="$1"
    local bot_token="$2"
    shift 2
    
    local dm_policy="pairing"
    local allow_from=""
    
    while [ $# -gt 0 ]; do
        case "$1" in
            --dm-policy)
                dm_policy="$2"
                shift 2
                ;;
            --allow-from)
                allow_from="$2"
                shift 2
                ;;
            *)
                shift
                ;;
        esac
    done
    
    if [ -z "$account_id" ] || [ -z "$bot_token" ]; then
        echo -e "${RED}Error: accountId and botToken required${NC}"
        echo "Usage: add-telegram-account <accountId> <botToken> [--dm-policy pairing|allowlist|open] [--allow-from chat_id1,chat_id2]"
        exit 1
    fi
    
    echo -e "${GREEN}Adding Telegram account '$account_id'...${NC}"
    
    python3 << PYEOF
import json

with open("$CONFIG_FILE", 'r') as f:
    d = json.load(f)

if 'channels' not in d:
    d['channels'] = {}
if 'telegram' not in d['channels']:
    d['channels']['telegram'] = {'enabled': True}
if 'accounts' not in d['channels']['telegram']:
    d['channels']['telegram']['accounts'] = {}

account = {
    'botToken': '$bot_token',
    'dmPolicy': '$dm_policy'
}

allow_list = []
if '$allow_from' != '':
    allow_list = ['$_allow_from' for x in '$allow_from'.split(',')]
    account['allowFrom'] = allow_list

d['channels']['telegram']['accounts']['$account_id'] = account

with open("$CONFIG_FILE", 'w') as f:
    json.dump(d, f, indent=2)
PYEOF
    
    echo -e "${GREEN}✓ Telegram account '$account_id' added${NC}"
    restart_gateway
}

function remove_telegram_account() {
    local account_id="$1"
    
    if [ -z "$account_id" ]; then
        echo -e "${RED}Error: accountId required${NC}"
        echo "Usage: remove-telegram-account <accountId>"
        exit 1
    fi
    
    echo -e "${GREEN}Removing Telegram account '$account_id'...${NC}"
    
    python3 << PYEOF
import json

with open("$CONFIG_FILE", 'r') as f:
    d = json.load(f)

accounts = d.get('channels', {}).get('telegram', {}).get('accounts', {})
if '$account_id' in accounts:
    del accounts['$account_id']
    d['channels']['telegram']['accounts'] = accounts

# Also remove any bindings using this account
d['bindings'] = [
    b for b in d.get('bindings', [])
    if not (b.get('match', {}).get('accountId') == '$account_id' and b.get('match', {}).get('channel') == 'telegram')
]

with open("$CONFIG_FILE", 'w') as f:
    json.dump(d, f, indent=2)
PYEOF
    
    echo -e "${GREEN}✓ Telegram account '$account_id' removed${NC}"
    restart_gateway
}

function show_bindings() {
    echo -e "${GREEN}Current Bindings:${NC}"
    
    python3 << 'PYEOF'
import json

with open("/root/.openclaw/openclaw.json", 'r') as f:
    d = json.load(f)

bindings = d.get('bindings', [])
if not bindings:
    print("  No bindings configured")

for b in bindings:
    agent = b.get('agentId', 'unknown')
    match = b.get('match', {})
    channel = match.get('channel', 'unknown')
    
    if channel == 'whatsapp':
        peer = match.get('peer', {})
        peer_id = peer.get('id', 'unknown')
        print(f"  {agent} -> WhatsApp ({peer_id})")
    elif channel == 'telegram':
        account_id = match.get('accountId', 'unknown')
        peer_id = match.get('peer', {}).get('id', '')
        if peer_id:
            print(f"  {agent} -> Telegram ({peer_id})")
        else:
            print(f"  {agent} -> Telegram (account: {account_id})")
    else:
        print(f"  {agent} -> {channel}")

# Show Telegram accounts
telegram = d.get('channels', {}).get('telegram', {})
accounts = telegram.get('accounts', {})
if accounts:
    print("\nTelegram Accounts:")
    for name, cfg in accounts.items():
        print(f"  {name}: botToken=***, dmPolicy={cfg.get('dmPolicy', 'pairing')}")
PYEOF
}

# ------------------------------------------------------------------------------
# TOOL MANAGEMENT
# ------------------------------------------------------------------------------

function get_agent_idx() {
    local agent="$1"
    python3 -c "
import json
with open('$CONFIG_FILE') as f:
    d = json.load(f)
for i, a in enumerate(d.get('agents', {}).get('list', [])):
    if a.get('id') == '$agent':
        print(i)
        break
" 2>/dev/null
}

function enable_tool() {
    local agent="$1"
    local tool="$2"
    if [ -z "$agent" ] || [ -z "$tool" ]; then
        echo -e "${RED}Error: Agent and tool required${NC}"
        exit 1
    fi
    local idx=$(get_agent_idx "$agent")
    if [ -z "$idx" ]; then
        echo -e "${RED}Agent '$agent' not found${NC}"
        exit 1
    fi
    echo -e "${GREEN}Enabling tool '$tool' for agent '$agent'...${NC}"
    openclaw config set "agents.list[$idx].tools.allow" --json "[ \"$tool\" ]" 2>/dev/null || \
    echo -e "${RED}Failed${NC}"
}

function disable_tool() {
    local agent="$1"
    local tool="$2"
    if [ -z "$agent" ] || [ -z "$tool" ]; then
        echo -e "${RED}Error: Agent and tool required${NC}"
        exit 1
    fi
    local idx=$(get_agent_idx "$agent")
    if [ -z "$idx" ]; then
        echo -e "${RED}Agent '$agent' not found${NC}"
        exit 1
    fi
    echo -e "${GREEN}Disabling tool '$tool' for agent '$agent'...${NC}"
    openclaw config set "agents.list[$idx].tools.deny" --json "[ \"$tool\" ]" 2>/dev/null || \
    echo -e "${RED}Failed${NC}"
}

function set_tools_profile() {
    local agent="$1"
    local profile="$2"
    case "$profile" in
        minimal|coding|messaging|full) ;;
        *)
            echo -e "${RED}Invalid profile. Use: minimal, coding, messaging, full${NC}"
            exit 1
            ;;
    esac
    local idx=$(get_agent_idx "$agent")
    if [ -z "$idx" ]; then
        echo -e "${RED}Agent '$agent' not found${NC}"
        exit 1
    fi
    echo -e "${GREEN}Setting tools profile '$profile' for agent '$agent'...${NC}"
    openclaw config set "agents.list[$idx].tools.profile" "$profile"
}

function list_agent_tools() {
    local agent="$1"
    local idx=$(get_agent_idx "$agent")
    if [ -z "$idx" ]; then
        echo -e "${RED}Agent '$agent' not found${NC}"
        exit 1
    fi
    echo -e "${GREEN}Tool configuration for agent '$agent':${NC}"
    openclaw config get "agents.list[$idx].tools" 2>/dev/null || \
    echo -e "${YELLOW}No explicit tool config${NC}"
}

# ------------------------------------------------------------------------------
# SKILL MANAGEMENT
# ------------------------------------------------------------------------------

function enable_agent_skill() {
    local agent="$1"
    local skill="$2"
    if [ -z "$agent" ] || [ -z "$skill" ]; then
        echo -e "${RED}Error: Agent and skill required${NC}"
        exit 1
    fi
    local idx=$(get_agent_idx "$agent")
    if [ -z "$idx" ]; then
        echo -e "${RED}Agent '$agent' not found${NC}"
        exit 1
    fi
    echo -e "${GREEN}Enabling skill '$skill' for agent '$agent'...${NC}"
    openclaw config set "agents.list[$idx].skills" --json "[ \"$skill\" ]" 2>/dev/null || \
    echo -e "${RED}Failed${NC}"
}

function list_agent_skills() {
    local agent="$1"
    local idx=$(get_agent_idx "$agent")
    if [ -z "$idx" ]; then
        echo -e "${RED}Agent '$agent' not found${NC}"
        exit 1
    fi
    echo -e "${GREEN}Skills for agent '$agent':${NC}"
    openclaw config get "agents.list[$idx].skills" 2>/dev/null || \
    echo -e "${YELLOW}No skills configured${NC}"
}

function list_skills() {
    echo -e "${GREEN}Available skills...${NC}"
    openclaw skills list
}

# ------------------------------------------------------------------------------
# SKILL INSTALL FROM LOCAL / WORKSPACE MANAGEMENT
# ------------------------------------------------------------------------------

function install_skill() {
    local slug="$1"
    local source_dir="$PENDING_SKILLS_DIR/$slug"

    if [ -z "$slug" ]; then
        echo -e "${RED}Error: Skill slug required${NC}"
        echo "Usage: $0 install-skill <slug>"
        echo ""
        echo "Available slugs in pending-skills/:"
        ls "$PENDING_SKILLS_DIR/" 2>/dev/null | sed 's/^/  /'
        exit 1
    fi

    if [ ! -d "$source_dir" ]; then
        echo -e "${RED}Error: Skill '$slug' not found in pending-skills/${NC}"
        echo "Available:"
        ls "$PENDING_SKILLS_DIR/" 2>/dev/null | sed 's/^/  /'
        exit 1
    fi

    local skill_md="$source_dir/SKILL.md"
    local readme_md="$source_dir/README.md"

    if [ ! -f "$skill_md" ] && [ ! -f "$readme_md" ]; then
        echo -e "${RED}Error: Neither SKILL.md nor README.md found for '$slug'${NC}"
        exit 1
    fi

    # Determine dest: workspace/skills/<slug>
    local dest_dir="$SKILLS_DIR/$slug"

    # Check if skill already installed
    if [ -d "$dest_dir" ]; then
        echo -e "${YELLOW}Skill '$slug' already installed at $dest_dir${NC}"
        echo -e "${YELLOW}Use --force to overwrite, or remove it first with:${NC}"
        echo "  rm -rf $dest_dir"
        exit 1
    fi

    echo -e "${GREEN}Installing skill: $slug${NC}"
    echo "  From: $source_dir"
    echo "  To:   $dest_dir"

    mkdir -p "$dest_dir"

    # Copy all files preserving structure
    if cp -r "$source_dir"/* "$dest_dir/" 2>/dev/null; then
        echo "  Files copied."
    else
        echo -e "${RED}Error: Failed to copy files${NC}"
        exit 1
    fi

    # Ensure SKILL.md exists (promote README if no SKILL.md)
    if [ ! -f "$dest_dir/SKILL.md" ] && [ -f "$dest_dir/README.md" ]; then
        echo "  Note: No SKILL.md found; README.md will be used as description."
    fi

    # Verify
    if ls "$dest_dir"/*.{md,js,sh,py,json} 2>/dev/null | head -1 | grep -q .; then
        echo -e "${GREEN}  ✓ Skill installed successfully${NC}"
        echo "  Contents: $(ls "$dest_dir" | tr '\n' ' ')"
    else
        echo -e "${YELLOW}  Warning: Skill dir may be empty${NC}"
    fi

    echo ""
    echo -e "${GREEN}Skill '$slug' is now available.${NC}"
    echo "  Location: $dest_dir"
    echo ""
    echo "Note: OpenClaw may need a gateway restart to pick up the new skill:"
    echo "  $ openclaw gateway restart"
}

function remove_skill() {
    local slug="$1"
    if [ -z "$slug" ]; then
        echo -e "${RED}Error: Skill slug required${NC}"
        exit 1
    fi
    local dest_dir="$SKILLS_DIR/$slug"
    if [ ! -d "$dest_dir" ]; then
        echo -e "${RED}Skill '$slug' not found in workspace${NC}"
        exit 1
    fi
    echo -e "${YELLOW}Removing skill '$slug'...${NC}"
    rm -rf "$dest_dir"
    echo -e "${GREEN}Removed: $dest_dir${NC}"
}

function skill_info() {
    local slug="$1"
    if [ -z "$slug" ]; then
        echo -e "${RED}Error: Skill slug required${NC}"
        exit 1
    fi

    # Check workspace first
    local dir="$SKILLS_DIR/$slug"
    if [ -d "$dir" ]; then
        echo -e "${GREEN}=== Skill: $slug (workspace) ===${NC}"
        if [ -f "$dir/SKILL.md" ]; then
            head -30 "$dir/SKILL.md"
        elif [ -f "$dir/README.md" ]; then
            head -30 "$dir/README.md"
        fi
        echo ""
        echo "Files: $(ls "$dir" | tr '\n' ' ')"
        return
    fi

    # Fall back to openclaw skills info
    openclaw skills info "$slug" 2>/dev/null || \
        echo -e "${RED}Skill '$slug' not found${NC}"
}

function install_pending_skills() {
    echo -e "${GREEN}Installing all pending skills to workspace...${NC}"
    echo "  Source: $PENDING_SKILLS_DIR/"
    echo "  Dest:   $SKILLS_DIR/"
    echo ""

    local count=0
    for dir in "$PENDING_SKILLS_DIR"/*/; do
        [ -d "$dir" ] || continue
        local slug=$(basename "$dir")
        if [ -d "$SKILLS_DIR/$slug" ]; then
            echo -e "${YELLOW}  Skip: $slug (already installed)${NC}"
        else
            echo -e "${BLUE}  Installing: $slug${NC}"
            mkdir -p "$SKILLS_DIR/$slug"
            cp -r "$dir"/* "$SKILLS_DIR/$slug/" 2>/dev/null && \
                echo -e "${GREEN}    ✓ $slug installed${NC}" || \
                echo -e "${RED}    ✗ $slug failed${NC}"
            count=$((count+1))
        fi
    done
    echo ""
    echo -e "${GREEN}Installed $count new skills. Restart gateway to activate.${NC}"
}

# ------------------------------------------------------------------------------
# AGENT-TO-AGENT
# ------------------------------------------------------------------------------

function enable_agent_to_agent() {
    echo -e "${GREEN}Enabling agent-to-agent communication...${NC}"
    openclaw config set "tools.agentToAgent.enabled" "true"
}

# ------------------------------------------------------------------------------
# ROUTER
# ------------------------------------------------------------------------------

case "$1" in
    list-agents)
        list_agents
        ;;
    add-agent)
        add_agent "$2" "$3"
        ;;
    delete-agent)
        delete_agent "$2"
        ;;
    bind)
        bind_channel "$2" "$3"
        ;;
    unbind)
        unbind_channel "$2" "$3"
        ;;
    bind-telegram)
        shift
        bind_telegram "$@"
        ;;
    unbind-telegram)
        unbind_telegram "$2"
        ;;
    list-telegram-accounts)
        list_telegram_accounts
        ;;
    add-telegram-account)
        shift
        add_telegram_account "$@"
        ;;
    remove-telegram-account)
        remove_telegram_account "$2"
        ;;
    status)
        openclaw status
        ;;
    health)
        openclaw health
        ;;
    bindings)
        show_bindings
        ;;
    enable-tool)
        enable_tool "$2" "$3"
        ;;
    disable-tool)
        disable_tool "$2" "$3"
        ;;
    set-tools-profile)
        set_tools_profile "$2" "$3"
        ;;
    list-agent-tools)
        list_agent_tools "$2"
        ;;
    enable-skill)
        enable_agent_skill "$2" "$3"
        ;;
    disable-skill)
        echo -e "${YELLOW}Edit ~/.openclaw/openclaw.json manually to remove skills${NC}"
        ;;
    list-agent-skills)
        list_agent_skills "$2"
        ;;
    list-skills)
        list_skills
        ;;
    install-skill)
        install_skill "$2"
        ;;
    install-all-pending)
        install_pending_skills
        ;;
    remove-skill)
        remove_skill "$2"
        ;;
    skill-info)
        skill_info "$2"
        ;;
    agent-to-agent-status)
        openclaw config get "tools.agentToAgent" 2>/dev/null
        ;;
    enable-agent-to-agent)
        enable_agent_to_agent
        ;;
    help|--help|-h)
        show_usage
        ;;
    *)
        show_usage
        ;;
esac