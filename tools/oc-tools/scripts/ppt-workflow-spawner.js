#!/usr/bin/env node
/**
 * Workflow Orchestrator - With Real Subagent Spawning
 * 
 * This is run by the main agent. It spawns subagents for each workflow step.
 * The main agent coordinates and tracks events between steps.
 * 
 * Usage: node workflow-spawner.js <project-dir> [--step <step-name>]
 */

const fs = require('fs');
const path = require('path');

const CONFIG_PATH = path.join(__dirname, 'workflow-ppt', 'config.json');

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

function log(step, msg, color = 'reset') {
  const ts = new Date().toISOString().substr(11, 8);
  const tag = step ? `${C[color]}[${step}]${C.reset}` : '   ';
  console.log(`${C.dim}${ts}${C.reset} ${tag} ${msg}`);
}

function getStepInstructions(stepName, projectDir) {
  const instructions = {
    'spec_ready': `You are the architect. Review the dining-feedback project.

Project: ${projectDir}
Tech: React + Express + PostgreSQL + Supabase

Tasks:
1. Check if SPEC.md exists at ${projectDir}/SPEC.md
2. If not, create SPEC.md with architecture overview
3. Verify tech stack components exist

Report: Architecture status (approved/needs work)
Emit: spec_ready when done`,

    'implement': `You are the builder. Review dining-feedback implementation.

Project: ${projectDir}

Tasks:
1. Check backend: ${projectDir}/backend/src
2. Check frontend: ${projectDir}/frontend/src  
3. Check docker-compose.yml

Report: Current implementation status, what's complete, what's missing
Emit: code_review_requested when done`,

    'db_review': `You are the DBA. Review database schema.

Project: ${projectDir}
Schema: ${projectDir}/backend/prisma/schema.prisma

Tasks:
1. Read the Prisma schema
2. Check tables: Feedback, Admin, OAuthSession
3. Verify indexes and security

Report: Approved or issues found
Emit: db_approved if approved`,

    'build': `You are the builder. Build Docker images.

Project: ${projectDir}

Tasks:
1. cd ${projectDir} && docker compose build
2. Wait for build complete
3. Verify images: docker images | grep dining

Report: Build success/failure
Emit: build_done if successful`,

    'deploy_staging': `You are DevOps. Deploy to staging.

Project: ${projectDir}

Tasks:
1. Check containers: docker ps --filter "name=dining"
2. If not all running: cd ${projectDir} && docker compose up -d
3. Wait 15 seconds
4. Check health: curl -s -o /dev/null -w "%{http_code}" http://localhost:8081

Report: Deployment status (deployed/failed)
Emit: deploy_ready if healthy`,

    'test': `You are QA. Run verification tests.

Project: ${projectDir}

Tasks:
1. Frontend test: curl -s -o /dev/null -w "Frontend: %{http_code}\n" http://localhost:8081
2. Backend test: curl -s http://localhost:3010/health
3. DB test: docker exec dining-db pg_isready -U dining_user

Report: All PASS or FAIL with details
Emit: tests_passed if all pass, tests_failed if any fail`,

    'hotfix': `You are DevOps. Fix production issue.

Project: ${projectDir}

Tasks:
1. Check logs: docker logs dining-backend --tail 30
2. Identify issue
3. Apply fix
4. Redeploy and verify

Report: Fix applied, verification result
Emit: hotfix_deployed`
  };
  
  return instructions[stepName] || `Execute step: ${stepName}`;
}

async function main() {
  const args = process.argv.slice(2);
  
  let projectDir = null;
  let startStep = null;
  
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--step' && args[i + 1]) {
      startStep = args[i + 1];
      i++;
    } else if (!args[i].startsWith('--')) {
      projectDir = args[i];
    }
  }
  
  if (!projectDir) {
    console.log(`
${C.bright}Workflow Spawner${C.reset}
Usage: node workflow-spawner.js <project-dir> [--step <step-name>]

Note: This script is designed to be run by the main OpenClaw agent.
The agent will spawn subagents for each step using sessions_spawn.
`);
    process.exit(1);
  }
  
  console.log(`${C.cyan}
╔═══════════════════════════════════════════════════════════╗
║  OpenClaw Workflow Spawner                                ║
╚═══════════════════════════════════════════════════════════╝${C.reset}\n`);
  
  console.log(`${C.yellow}Project:${C.reset} ${projectDir}`);
  console.log(`${C.yellow}Start:${C.reset} ${startStep || 'from beginning'}\n`);
  
  const config = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8'));
  const emittedEvents = new Set();
  
  // Determine starting point
  let pipeline = config.pipeline;
  if (startStep) {
    const idx = pipeline.findIndex(s => s.step === startStep);
    if (idx >= 0) {
      pipeline = pipeline.slice(idx);
      console.log(`${C.yellow}Resuming from:${C.reset} ${startStep}\n`);
    }
  }
  
  // Print spawn commands for each step
  // (The main agent will execute these via sessions_spawn)
  console.log(`${C.bright}Pipeline Steps:${C.reset}\n`);
  
  for (const step of pipeline) {
    // Check if step can run
    if (step.receives) {
      const required = [].concat(step.receives);
      const canRun = required.every(e => emittedEvents.has(e));
      if (!canRun) {
        log(step.step, `SKIP - waiting for: ${step.receives}`, 'dim');
        continue;
      }
    }
    
    const emits = step.emits ? [].concat(step.emits).join(', ') : 'none';
    const task = getStepInstructions(step.step, projectDir);
    
    console.log(`${C.cyan}═══ Step: ${step.step} ═══${C.reset}`);
    console.log(`${C.yellow}Agent:${C.reset} ${step.agent}`);
    console.log(`${C.yellow}Emits:${C.reset} ${emits}`);
    console.log(`${C.green}Task:${C.reset} ${task.substring(0, 200)}...`);
    console.log('');
    
    // Mark events as would be emitted
    if (step.emits) {
      [].concat(step.emits).forEach(e => emittedEvents.add(e));
    }
    
    // Print the sessions_spawn command that would be used
    console.log(`${C.dim}→ Spawn subagent with task:${C.reset}`);
    console.log(`   ${task.substring(0, 150)}...\n`);
  }
  
  console.log(`${C.bright}═══════════════════════════════════════════════════════════${C.reset}`);
  console.log(`${C.green}Summary: ${pipeline.length} steps to execute${C.reset}`);
  console.log(`${C.dim}Events that will be emitted:${C.reset} ${Array.from(emittedEvents).join(', ')}`);
  console.log(`${C.bright}═══════════════════════════════════════════════════════════${C.reset}\n`);
  
  console.log(`${C.yellow}To execute this workflow, the main agent should:${C.reset}`);
  console.log(`1. Call sessions_spawn for each step`);
  console.log(`2. Wait for completion via sessions_yield`);
  console.log(`3. Track emitted events`);
  console.log(`4. Continue to next step\n`);
}

main().catch(e => {
  console.error('Error:', e.message);
  process.exit(1);
});