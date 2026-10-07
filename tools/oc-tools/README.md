# OpenClaw Tools (oc-tools)

Management toolkit for OpenClaw multi-agent workspace — covers agent setup, skill management, workflow orchestration, and ClawHub integration.

---

## Quick Start

```bash
cd /root/.openclaw/workspace/oc-tools

# List all agents and their current bindings
./bin/oc-manage.sh list-agents

# Bootstrap a new agent with skills
./bin/setup-agents.sh add cto

# Install a skill from pending-skills
./bin/oc-manage.sh install-skill kube-medic

# Run a workflow
./bin/run-workflow.sh devops

# Search ClawHub for a skill
./bin/clawhub-search.sh kubernetes
```

---

## Directory Structure

```
oc-tools/
├── bin/
│   ├── oc-manage.sh        # Agent/skill/channel management
│   ├── setup-agents.sh     # Bootstrap agents with role skills
│   ├── clawhub-search.sh   # Search & install from ClawHub
│   └── run-workflow.sh     # Launch workflow pipelines
├── docs/
│   ├── WORKFLOW.md         # Workflow design & execution guide
│   └── oc-tools-readme.md   # oc-tools overview
├── scripts/
│   ├── workflow-orchestrator.js   # DevOps pipeline orchestrator
│   ├── workflow-spawner.js       # Subagent spawning logic
│   ├── workflow-runner.js         # Event-driven workflow runner
│   ├── ppt-workflow-orchestrator.js
│   ├── ppt-workflow-spawner.js
│   └── ppt-workflow-runner.js
└── workflows/
    ├── devops/             # CTO→Engineer→DBA→DevOps→QA→Ops pipeline
    │   ├── config.json
    │   ├── events.json
    │   ├── routing.json
    │   └── workflow.sh
    └── ppt/                # CTO→BA→DevOps→QA pipeline
        ├── agents/AGENTS.md
        ├── config.json
        ├── events.json
        ├── routing.json
        └── workflow.sh
```

---

## bin/oc-manage.sh Commands

| Command | Description |
|---------|-------------|
| `list-agents` | Show all agents, skills, and channel bindings |
| `list-skills` | Show all installed skills (ready + needs setup) |
| `bindings` | Show current channel binding rules |
| `bind-telegram <agent> <accountId>` | Bind agent to Telegram bot |
| `bind-whatsapp <agent> <number>` | Bind agent to WhatsApp number |
| `add-telegram-account <id> <token>` | Add Telegram bot account |
| `install-skill <slug>` | Install skill from `pending-skills/` |
| `install-all-pending` | Install all skills from pending-skills/ |
| `remove-skill <slug>` | Remove skill from workspace |
| `skill-info <slug>` | Show skill name, description, category |
| `openai-models` | List models via OpenAI API |
| `ollama-models` | List models via Ollama API |
| `litellm-models` | List models via LiteLLM API |

Full help: `./bin/oc-manage.sh help`

---

## bin/setup-agents.sh

Bootstrap OpenClaw agents with role-specific skill assignments.

```bash
# Add/reinstall a specific agent
./bin/setup-agents.sh add cto

# List available agents and their current status
./bin/setup-agents.sh list

# Show default skill assignments
./bin/setup-agents.sh defaults
```

### Default Agent → Skills Mapping

| Agent | Skills |
|-------|--------|
| ceo | leadership, strategy, management |
| cto | architecture, technical-leadership, agent-memory |
| software_engineer | coding-agent, github |
| devops | kubernetes, agentic-devops, kube-medic, azure-devops |
| dba | ansible-db-hardening |
| qa | aegis-audit, ai-act-risk-check |
| operations | management, operations |
| hr | recruiter-assistant |
| marketing | pptx, 2slides |
| business_dev | conventional-commits |

---

## Workflows

### DevOps Pipeline

6-stage event-driven pipeline: **CTO → Software Engineer → DBA → DevOps → QA → Operations**

```bash
./bin/run-workflow.sh devops [event]
# e.g.: ./bin/run-workflow.sh devops spec_ready
```

### PPT Pipeline

4-stage content pipeline: **CTO (outline) → BA (content) → DevOps (format) → QA (review)**

```bash
./bin/run-workflow.sh ppt [event]
# e.g.: ./bin/run-workflow.sh ppt outline_ready
```

---

## Skill Installation

1. Skills download to `pending-skills/` (via clawhub-search or manual)
2. Install to workspace: `./bin/oc-manage.sh install-all-pending`
3. Or individual: `./bin/oc-manage.sh install-skill <slug>`

Currently installed workspace skills: **45** (✓ ready + ⚠️ needs setup)

---

## ClawHub Search

```bash
# Search for a skill
./bin/clawhub-search.sh kubernetes

# Search with pagination
./bin/clawhub-search.sh docker 2

# List all categories
./bin/clawhub-search.sh --list-categories
```

---

## Requirements

- OpenClaw 2026.3+
- Node.js 18+ (for workflow JS scripts)
- `gh` CLI authenticated to GitHub