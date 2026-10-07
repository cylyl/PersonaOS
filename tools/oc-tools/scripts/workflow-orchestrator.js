#!/usr/bin/env node
/**
 * Workflow Orchestrator - Simplified version
 * 
 * This script is run via the main agent to execute the devops pipeline.
 * It prints out the commands to run each step.
 * 
 * Usage: node workflow-orchestrator.js <project-dir> [--start-step <step>]
 */

const fs = require('fs');
const path = require('path');

const CONFIG_PATH = path.join(__dirname, 'workflow-devops', 'config.json');

// ANSI colors
const C = {
  reset: '\x1b[0m',
  bright: '\x1b[1m',
  dim: '\x1b[2m',
  red: '\x1b[31m',
  green: '\x1b[32m',
  yellow: '\x1b[33m',
  cyan: '\x1b[36m'
};

function log(msg, color = 'reset') {
  console.log(`${C[color]}${msg}${C.reset}`);
}

async function loadConfig() {
  return JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8'));
}

function getStepTask(stepName, step, agentConfig, context) {
  const instructions = {
    'spec_ready': `Review project SPEC.md at ${context.projectDir}. Create if missing.
Report: Architecture status, tech stack verification, any recommendations.
Emit: spec_ready`,

    'implement': `Review implementation status at ${context.projectDir}.
Check: backend/src, frontend/src, docker-compose.yml
Report: What exists, what's missing, current status.
Emit: code_review_requested`,

    'db_review': `Review database schema at ${context.projectDir}/backend/prisma/schema.prisma
Check: tables (Feedback, Admin, OAuthSession), indexes, security
Report: Approval or issues found.
Emit: db_approved`,

    'build': `Build project at ${context.projectDir}:
1. cd ${context.projectDir} && docker compose build
2. Report: Build success/failure, images created
Emit: build_done`,

    'deploy_staging': `Deploy to staging at ${context.projectDir}:
1. docker ps --filter "name=dining" (check running)
2. If not running: docker compose up -d
3. Wait 10s, check http://localhost:8081
Report: Deployment status, services healthy?
Emit: deploy_ready`,

    'test': `QA verification at ${context.projectDir}:
1. curl -s -o /dev/null -w "Frontend: %{http_code}" http://localhost:8081
2. curl -s -o /dev/null -w "Backend: %{http_code}" http://localhost:3010/health
3. docker exec dining-db pg_isready -U dining_user
Report: All tests PASSED or FAILED with details.
Emit: tests_passed (if all ok) or tests_failed (if any fail)`,

    'hotfix': `Fix production issue:
1. docker logs dining-backend --tail 30
2. Identify and fix issue
3. Redeploy and verify
Report: Issue found, fix applied, verification result.
Emit: hotfix_deployed`
  };
  
  return instructions[stepName] || `Execute step: ${stepName}`;
}

function canExecuteStep(step, emittedEvents) {
  if (!step.receives) return true;
  const required = [].concat(step.receives);
  return required.every(e => emittedEvents.has(e));
}

async function main() {
  const args = process.argv.slice(2);
  
  // Parse args
  let projectDir = null;
  let startStep = null;
  
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--start-step' && args[i + 1]) {
      startStep = args[i + 1];
      i++;
    } else if (!args[i].startsWith('--')) {
      projectDir = args[i];
    }
  }
  
  if (!projectDir) {
    log(`
${C.bright}Workflow Orchestrator${C.reset}
Usage: node workflow-orchestrator.js <project-dir> [--start-step <step>]

Example:
  node workflow-orchestrator.js /root/.openclaw/workspace/projects/dining-feedback
`, 'cyan');
    process.exit(1);
  }
  
  log(`
╔═══════════════════════════════════════════════════════════╗
║  OpenClaw Workflow Orchestrator                          ║
╚═══════════════════════════════════════════════════════════╝
`, 'cyan');
  
  log(`Project: ${projectDir}\n`, 'yellow');
  
  const config = await loadConfig();
  const emittedEvents = new Set();
  const context = {
    projectName: path.basename(projectDir),
    projectDir: projectDir
  };
  
  // Determine pipeline
  let pipeline = config.pipeline;
  if (startStep) {
    const idx = pipeline.findIndex(s => s.step === startStep);
    if (idx >= 0) {
      pipeline = pipeline.slice(idx);
      log(`Starting from step: ${startStep}\n`, 'yellow');
    }
  }
  
  log(`Pipeline: ${pipeline.length} steps\n`, 'dim');
  
  // Print spawn commands for each executable step
  for (const step of pipeline) {
    if (!canExecuteStep(step, emittedEvents)) {
      log(`[SKIP] ${step.step} - waiting for: ${step.receives}`, 'dim');
      continue;
    }
    
    const agentConfig = config.agents[step.agent];
    const task = getStepTask(step.step, step, agentConfig, context);
    const emits = step.emits ? [].concat(step.emits).join(', ') : 'none';
    
    log(`${C.bright}--- Step: ${step.step} ---${C.reset}`, 'cyan');
    log(`Agent: ${step.agent} (${agentConfig.role})`);
    log(`Receives: ${step.receives || 'none'}`);
    log(`Emits: ${emits}`);
    log(`Task: ${task}`);
    log('');
    
    // Mark events as would-be-emitted
    if (step.emits) {
      [].concat(step.emits).forEach(e => emittedEvents.add(e));
    }
  }
  
  log(`${C.bright}--- To Execute ---${C.reset}`, 'green');
  log('Run this to execute via subagent spawning:', 'dim');
  log(`node ${__filename} ${projectDir} --start-step <step-name>\n`, 'dim');
  
  log(`
${C.bright}To run the actual workflow, use this agent's sessions_spawn tool.${C.reset}
The orchestrator prints what WOULD happen. To execute for real,
I (the main agent) will spawn subagents for each step using sessions_spawn.
`, 'yellow');
}

main().catch(e => {
  console.error('Error:', e.message);
  process.exit(1);
});