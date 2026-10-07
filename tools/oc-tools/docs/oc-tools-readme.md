# OpenClaw Tools

Management scripts for OpenClaw multi-agent workspace.

## Scripts

| Script | Description |
|--------|-------------|
| `oc-manage.sh` | Main workspace management toolkit |
| `setup-agents.sh` | Create and configure agents with role-specific skills |
| `clawhub-search.sh` | Search and browse skills from ClawHub |

## oc-manage.sh

OpenClaw management toolkit for agents, tools, skills, and workspace operations.

### Usage
```bash
./oc-manage.sh <command> [options]
```

### Agent Commands
```bash
./oc-manage.sh list-agents                    # List all agents
./oc-manage.sh add-agent <name> [--model <model>]  # Create new agent
./oc-manage.sh delete-agent <name>            # Delete an agent
./oc-manage.sh bind <name> <channel>         # Bind agent to channel
./oc-manage.sh unbind <name> <channel>       # Unbind agent from channel
```

### Per-Agent Tool Commands
```bash
./oc-manage.sh enable-tool <agent> <tool>            # Enable a tool for agent
./oc-manage.sh disable-tool <agent> <tool>           # Disable a tool for agent
./oc-manage.sh set-tools-profile <agent> <profile>  # Set tools profile
./oc-manage.sh list-agent-tools <agent>              # Show agent tool config
```

**Tool Profiles:** `minimal` | `coding` | `messaging` | `full`

### Per-Agent Skill Commands
```bash
./oc-manage.sh enable-skill <agent> <skill>    # Enable skill for agent
./oc-manage.sh disable-skill <agent> <skill>   # Disable skill for agent
./oc-manage.sh list-agent-skills <agent>        # Show agent skills
```

### Global Skill Commands
```bash
./oc-manage.sh list-skills                    # List all available skills
./oc-manage.sh search-skills <query>          # Search ClawHub for skills
./oc-manage.sh install-skill <name>           # Install a skill from ClawHub
./oc-manage.sh remove-skill <name>            # Remove a skill from workspace
./oc-manage.sh skill-info <name>             # Show skill details
./oc-manage.sh check-skills                   # Check skill requirements
./oc-manage.sh update-skills                  # Update installed skills
```

### Workspace Commands
```bash
./oc-manage.sh status              # Show OpenClaw status
./oc-manage.sh health             # Show gateway health
./oc-manage.sh safety-check       # Verify 'Red Line' safety aliases
./oc-manage.sh dream              # Run memory consolidation
./oc-manage.sh help               # Show this help
```

## setup-agents.sh

Creates agents with per-agent tools and skills configuration.

### Usage
```bash
./setup-agents.sh
```

### Default Agents Created

| Agent | Role | Tools Profile | Skills |
|-------|------|---------------|--------|
| CEO | Overall leadership and strategy | full | leadership, strategy |
| HR | People management and recruitment | full | communication, hr |
| Business_Dev | Partnerships and client acquisition | full | sales, negotiation |
| IT_Sales | Technology product sales and pre-sales | full | sales, technical |
| Marketing | Brand and digital marketing | full | marketing, content |
| Finance | Financial management and accounting | full | accounting, analytics |
| Operations | Daily operations and logistics | full | management, operations |
| CTO | Technical strategy and architecture | full | architecture, technical-leadership |
| DBA | Database administration and hardening | full | ansible-db-hardening, database-admin |
| Software_Engineer | Software development and code implementation | full | coding-agent, github |
| DevOps | Infrastructure and CI/CD operations | full | kubernetes, docker, ci-cd |
| QA | Testing and quality assurance | full | testing, automation |

### Features

- **Per-agent skill filtering** - Each agent gets only the skills matching their role
- **Skills installed once** - Tracked in associative array to avoid duplicates
- **Existing agents preserved** - Updates configuration without recreating
- **Identity auto-set** - Name and emoji set via `openclaw agents set-identity`

## Config Paths

OpenClaw agent configuration is stored in `~/.openclaw/openclaw.json`:

```json
{
  "agents": {
    "list": [
      {
        "id": "tony",
        "name": "tony",
        "workspace": "/root/.openclaw/agents/tony",
        "tools": {
          "profile": "full"
        },
        "skills": [
          "coding-agent",
          "lossless-claw"
        ]
      }
    ]
  }
}
```

## clawhub-search.sh

Search and browse skills from [ClawHub](https://clawhub.ai).

### Usage
```bash
./clawhub-search.sh <query>          # Search for skills
./clawhub-search.sh list             # List all available skills
./clawhub-search.sh info <name>      # Show detailed info for a skill
./clawhub-search.sh install <name>   # Install a skill
./clawhub-search.sh categories      # Show skill categories
```

### Examples
```bash
./clawhub-search.sh kubernetes      # Search Kubernetes skills
./clawhub-search.sh info ansible    # Show ansible skill details
./clawhub-search.sh install k3s     # Install k3s skill
./clawhub-search.sh categories      # List popular categories
```

## workflow/

Event-driven multi-agent pipeline workflow with routing and events.

### Usage
```bash
./workflow/workflow.sh status        # Show workflow status
./workflow/workflow.sh list-agents   # List workflow agents
./workflow/workflow.sh list-routes   # List routing rules
./workflow/workflow.sh list-events   # List event definitions
./workflow/workflow.sh validate      # Validate configuration
./workflow/workflow.sh diagram      # Show ASCII flow diagram
./workflow/workflow.sh apply        # Apply workflow to OpenClaw
```

### Files
- `config.json` - Full workflow configuration (routing + events + agents)
- `routing.json` - Routing rules only
- `events.json` - Event definitions only
- `workflow.sh` - Workflow manager script

### Flow
```
CTO → software_engineer → devops → qa → operations
                         ↓              ↓
                        dba          bug_loop
                                         ↓
                                  software_engineer
```

See `workflow/README.md` for full documentation.

## Notes

- Scripts require `openclaw` CLI to be installed and in PATH
- Agent skills are installed globally to `/root/.openclaw/workspace/skills/`
- Per-agent skill configuration in openclaw.json controls which skills each agent can use
- Run `openclaw gateway restart` after config changes for changes to take effect
