# DevOps Pipeline Workflow

Event-driven multi-agent software development pipeline.

## Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                         CTO (Architect)                         │
│                    spec_ready │ approved                        │
└──────┬──────────────────────────────────────────────────────────┘
       │ spec_ready
       ▼
┌─────────────────────────────────────────────────────────────────┐
│                    SOFTWARE_ENGINEER (Builder)                   │
│         build_done │ db_review_requested │ code_review          │
└──────┬────────┬────────────────┬────────────────────────────────┘
       │        │                │
       │        │                ▼
       │        │         ┌──────────┐
       │        │         │   DBA   │
       │        │         │db_approved
       │        │         └────┬─────┘
       │        │              │
       ▼        ▼              ▼
┌─────────────────────────────────────────────────────────────────┐
│                         DEVOPS                                  │
│            deploy_ready │ deployed_to_staging                   │
└──────┬────────────────┬─────────────────────────────────────────┘
       │                │
       ▼                ▼
┌─────────────────────────────────────────────────────────────────┐
│                           QA                                    │
│         tests_passed │ tests_failed │ qa_approved               │
└──────┬────────────────┬──────────────────────────────────────────┘
       │                │
       ▼                ▼
┌──────────────┐  ┌──────────────────┐
│   BUG LOOP   │  │   OPERATIONS     │
│              │  │  deployed_prod   │
│ soft_eng ◀───┤  │  hotfix_requested
└──────────────┘  └────────┬─────────┘
                            │
                            ▼
                     ┌──────────────┐
                     │    HOTFIX    │
                     │  devops → qa │
                     └──────────────┘
```

## Events Reference

| Event | Emitter | Consumers | Trigger |
|-------|---------|-----------|---------|
| `spec_ready` | cto | software_engineer | Design complete |
| `code_review_requested` | software_engineer | cto | Code needs review |
| `build_done` | software_engineer | devops | Build artifact ready |
| `db_review_requested` | software_engineer | dba | DB changes need review |
| `db_approved` | dba | software_engineer | DB changes approved |
| `deploy_ready` | devops | qa | Deployed to staging |
| `tests_passed` | qa | operations, cto | All tests green |
| `tests_failed` | qa | software_engineer | Tests failing |
| `qa_approved` | qa | operations | QA gate passed |
| `deployed_to_staging` | operations | qa | Staging deployment done |
| `deployed_to_production` | operations | cto | Prod deployment done |
| `hotfix_requested` | operations | devops | Production issue |
| `hotfix_deployed` | devops | qa, operations | Hotfix live |

## Routing Rules

| From | To | Purpose |
|------|----|---------|
| cto | software_engineer | Send specs to build |
| software_engineer | devops | Request deployment |
| software_engineer | dba | DB review requests |
| devops | qa | Send for testing |
| qa | software_engineer | Bug fixes loop |
| qa | operations | Approved for release |
| dba | software_engineer | DB review feedback |
| operations | devops | Hotfix requests |

## Normal Development Flow

```
1. CTO emits: spec_ready
   → software_engineer receives, starts coding
   
2. software_engineer emits: db_review_requested (if DB changes)
   → dba receives, reviews
   → dba emits: db_approved
   
3. software_engineer emits: build_done
   → devops receives, builds, deploys to staging
   → devops emits: deploy_ready
   
4. qa receives: deploy_ready
   → runs tests
   → if PASS: emits tests_passed + qa_approved
     → operations receives, deploys to production
   → if FAIL: emits tests_failed
     → software_engineer receives, fixes bugs
     → Loop back to step 3
```

## Hotfix Flow

```
1. operations detects issue
   → emits: hotfix_requested
   → devops receives, creates fix
   → emits: hotfix_deployed
   → qa receives, fast-track tests
   → operations receives, confirms deployment
```

## Agent Responsibilities

| Agent | Role | Primary Skills |
|-------|------|----------------|
| cto | Architect | architecture, technical-leadership |
| software_engineer | Builder | coding-agent, github |
| devops | Infrastructure | kubernetes, docker, ci-cd |
| dba | Database Admin | ansible-db-hardening, database-admin |
| qa | Tester | testing, automation |
| operations | Release Manager | management, operations |

## Files

- `config.json` - Full workflow configuration
- `routing.json` - Routing rules only
- `events.json` - Event definitions only
- `README.md` - This file
- `apply-workflow.sh` - Script to apply workflow to OpenClaw
