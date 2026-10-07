# OpenClaw Workflow System

> Orchestrate multi-agent DevOps pipelines with real subagent spawning

## Overview

The workflow system executes a defined pipeline of steps, each handled by a specialized agent. Steps can depend on events emitted by previous steps, creating a directed graph of tasks.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    Workflow Config                       │
│              (workflow/config.json)                      │
├──────────────────────────────────────────────────────────┤
│  Step 1: spec_ready    → CTO                             │
│  Step 2: implement     → Software Engineer → emits db_review_requested
│  Step 3: db_review     → DBA                            │
│  Step 4: build         → Software Engineer              │
│  Step 5: deploy_staging → DevOps                        │
│  Step 6: test          → QA                             │
│  Step 7: hotfix        → DevOps (on-demand)             │
└──────────────────────────────────────────────────────────┘
           │
           ▼ (via sessions_spawn)
┌──────────────────────────────────────────────────────────┐
│              OpenClaw Subagents                          │
│  cto | software_engineer | dba | devops | qa            │
└──────────────────────────────────────────────────────────┘
```

## Files

| File | Purpose |
|------|---------|
| `workflow/config.json` | Pipeline definition (steps, agents, events, dependencies) |
| `workflow-runner.js` | Orchestrator that executes steps via subagent spawning |
| `workflow-spawner.js` | Preview tool — shows what would be executed |
| `workflow-orchestrator.js` | Alternative orchestrator with task printing |

## Quick Start

### 1. Preview a workflow

```bash
node workflow-spawner.js /root/.openclaw/workspace/projects/dining-feedback
```

### 2. Run the full pipeline

```bash
node workflow-runner.js /root/.openclaw/workspace/projects/dining-feedback
```

### 3. Start from a specific step

```bash
node workflow-runner.js /root/.openclaw/workspace/projects/dining-feedback --step deploy_staging
```

## Workflow Config Schema

```json
{
  "name": "DevOps Pipeline",
  "version": "1.0.0",
  "pipeline": [
    {
      "step": "spec_ready",
      "agent": "cto",
      "emits": "spec_ready"
    },
    {
      "step": "implement",
      "agent": "software_engineer",
      "receives": "spec_ready",
      "emits": "db_review_requested"
    },
    {
      "step": "db_review",
      "agent": "dba",
      "receives": "db_review_requested",
      "emits": "db_approved"
    },
    {
      "step": "build",
      "agent": "software_engineer",
      "receives": "db_approved",
      "emits": "build_done"
    },
    {
      "step": "deploy_staging",
      "agent": "devops",
      "receives": "build_done",
      "emits": "deploy_ready"
    },
    {
      "step": "test",
      "agent": "qa",
      "receives": "deploy_ready",
      "emits": ["tests_passed", "tests_failed"]
    },
    {
      "step": "hotfix",
      "agent": "devops",
      "receives": "hotfix_requested",
      "emits": "hotfix_deployed"
    }
  ],
  "events": [
    {
      "name": "spec_ready",
      "emit_by": "cto",
      "consumed_by": ["software_engineer"],
      "description": "Architectural spec is ready"
    }
  ],
  "agents": {
    "cto": { "role": "architect", "skills": ["architecture", "technical-leadership"] },
    "software_engineer": { "role": "builder", "skills": ["coding-agent", "github"] },
    "dba": { "role": "dba", "skills": ["ansible-db-hardening", "database-admin"] },
    "devops": { "role": "devops", "skills": ["kubernetes", "docker", "ci-cd"] },
    "qa": { "role": "qa", "skills": ["testing", "automation"] }
  },
  "settings": {
    "stop_on": ["test"]
  }
}
```

## Workflow Runner (workflow-runner.js)

The main orchestrator. It:

1. Loads `config.json`
2. Filters pipeline to `startStep` if specified
3. Checks event dependencies for each step
4. Spawns real OpenClaw subagents via `sessions_spawn`
5. Tracks emitted events
6. Reports full pipeline results

### Usage

```bash
node workflow-runner.js <project-dir> [--step <step-name>]
```

### Options

| Option | Description |
|--------|-------------|
| `<project-dir>` | Path to project (required) |
| `--step <name>` | Start from specific step |

### Example

```bash
# Run full pipeline
node workflow-runner.js /root/.openclaw/workspace/projects/dining-feedback

# Resume from deploy_staging
node workflow-runner.js /root/.openclaw/workspace/projects/dining-feedback --step deploy_staging
```

## Workflow Spawner (workflow-spawner.js)

Shows what would be executed without running agents. Prints detailed task prompts for each step.

### Usage

```bash
node workflow-spawner.js <project-dir> [--step <step-name>]
```

## Event Dependencies

Each step declares `receives` (events it needs before executing) and `emits` (events it produces when done).

**Example dependency chain:**
```
spec_ready (CTO emits)
    ↓
implement (Software Engineer receives spec_ready, emits db_review_requested)
    ↓
db_review (DBA receives db_review_requested, emits db_approved)
    ↓
build (Software Engineer receives db_approved, emits build_done)
    ↓
deploy_staging (DevOps receives build_done, emits deploy_ready)
    ↓
test (QA receives deploy_ready, emits tests_passed OR tests_failed)
```

## Stop-on Configuration

Set `stop_on` in settings to halt pipeline on a specific step failure:

```json
"settings": {
  "stop_on": ["test"]
}
```

## Custom Workflows

To create a custom workflow:

1. Copy `workflow/config.json` to a new location
2. Modify pipeline steps, agents, and events
3. Run with the new config path (edit workflow-runner.js to use your path)

## Notes

- Subagent spawning uses `sessions_spawn` with `runtime: "subagent"` and `mode: "run"`
- Each subagent gets a task prompt with project context and step-specific instructions
- The main agent coordinates via `sessions_yield` and waits for completion events
- Pipeline state (emitted events) is tracked in memory during execution

## Running Workflows via Agent (Programmatic)

The workflow-runner.js is designed to be run BY the main OpenClaw agent, not standalone. Here's how the main agent executes it:

### Agent Code Pattern

```javascript
// 1. Load config
const config = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8'));

// 2. For each executable step, spawn a subagent
const result = await sessions_spawn({
  mode: "run",
  runtime: "subagent",
  runTimeoutSeconds: 180,
  task: `You are the ${step.agent} agent. 
    
Task: ${getStepInstructions(stepName, projectDir)}

Project: ${projectDir}

When done, respond with:
- Status: SUCCESS/FAILED
- Events emitted: <list>
- Summary: <what you accomplished>`
});

// 3. Wait for completion
await sessions_yield();

// 4. Continue to next step when completion event arrives
```

### Key Insight

The workflow-runner.js prints commands to the console. The main agent reads those commands and translates them into `sessions_spawn` calls. The subagents are real OpenClaw agents (cto, software_engineer, dba, devops, qa) — not simulated.

### Event Chain Fix

If a step should trigger the next step but doesn't, check the `receives` field in config.json:

```json
// WRONG — implement emits code_review_requested but db_review expects db_review_requested
{ "step": "implement", "emits": "code_review_requested" }
{ "step": "db_review", "receives": "db_review_requested" }

// RIGHT — events must match
{ "step": "implement", "emits": "db_review_requested" }
{ "step": "db_review", "receives": "db_review_requested" }
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Step skipped | Check that the `receives` event was actually emitted by previous step |
| Subagent timeout | Increase `runTimeoutSeconds` (default 180s) |
| Tables missing | Run init.sql: `docker exec -i dining-db psql -U dining_user -d dining_feedback < backend/sql/init.sql` |
| Auth errors | Expected — some endpoints require Supabase JWT |
