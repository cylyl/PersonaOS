#!/usr/bin/env node
/**
 * OpenClaw Workflow Runner v3 - With Real Agent Spawning
 * 
 * Uses the sessions_spawn tool to actually run steps with real agents.
 * Each pipeline step spawns a subagent session.
 * 
 * The main agent (this script runs in) coordinates the workflow.
 */

const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

// Configuration paths
const CONFIG_PATH = path.join(__dirname, 'workflow-devops', 'config.json');
const WORKSPACE = '/root/.openclaw/workspace';

// Event state
const eventState = {
  emitted: new Set(),
  context: {}
};

// ANSI colors
const C = {
  reset: '\x1b[0m',
  bright: '\x1b[1m',
  dim: '\x1b[2m',
  red: '\x1b[31m',
  green: '\x1b[32m',
  yellow: '\x1b[33m',
  blue: '\x1b[34m',
  magenta: '\x1b[35m',
  cyan: '\x1b[36m'
};

function log(step, message, color = 'reset') {
  const ts = new Date().toISOString().substr(11, 8);
  const tag = step ? `${C[color]}[${step}]${C.reset}` : '   ';
  console.log(`${C.dim}${ts}${C.reset} ${tag} ${message}`);
}

// Execute a step by spawning a real OpenClaw agent
async function executeStepWithAgent(stepName, config, context, openclawBin = 'openclaw') {
  const step = config.pipeline.find(s => s.step === stepName);
  if (!step) {
    throw new Error(`Step not found: ${stepName}`);
  }
  
  const { agent, receives, emits } = step;
  const agentConfig = config.agents[agent];
  
  log(stepName, `${C.cyan}Executing via ${agent} agent${C.reset}...`, 'cyan');
  
  // Build agent task prompt
  const task = buildAgentTask(stepName, agent, agentConfig, config, context);
  
  // Use openclaw agent command to execute
  // This spawns a real agent session with the appropriate agent identity
  return new Promise((resolve, reject) => {
    const args = [
      'agent',
      '--to', 'main',  // route back to main session
      '--message', task,
      '--deliver'
    ];
    
    log(stepName, `Spawning agent: ${agent}`, 'yellow');
    
    const proc = spawn(openclawBin, args, {
      cwd: WORKSPACE,
      stdio: ['pipe', 'pipe', 'pipe']
    });
    
    let stdout = '';
    let stderr = '';
    
    proc.stdout.on('data', (data) => {
      stdout += data.toString();
    });
    
    proc.stderr.on('data', (data) => {
      stderr += data.toString();
    });
    
    proc.on('close', (code) => {
      if (code === 0) {
        log(stepName, `${C.green}Agent completed${C.reset}`, 'green');
        resolve({ 
          success: true, 
          output: stdout,
          events: emits ? [].concat(emits) : []
        });
      } else {
        log(stepName, `${C.red}Agent failed: ${stderr || stdout}${C.reset}`, 'red');
        reject(new Error(`Agent ${agent} failed with code ${code}`));
      }
    });
    
    // Timeout after 3 minutes
    setTimeout(() => {
      proc.kill();
      reject(new Error(`Agent ${agent} timed out after 3 minutes`));
    }, 180000);
  });
}

function buildAgentTask(stepName, agentId, agentConfig, config, context) {
  const step = config.pipeline.find(s => s.step === stepName);
  
  let task = `You are the ${agentConfig.role} agent (${agentId}) in a DevOps pipeline.\n\n`;
  task += `Current step: ${stepName}\n`;
  task += `Your role: ${agentConfig.role}\n`;
  task += `Your skills: ${agentConfig.skills.join(', ')}\n\n`;
  
  task += `Project: ${context.projectName}\n`;
  task += `Project path: ${context.projectDir}\n\n`;
  
  // Add step-specific instructions
  task += getStepInstructions(stepName, context);
  
  task += `\n\nWhen done, respond with:
- Status: SUCCESS/FAILED
- Events to emit: <list from your step's emits field>
- Summary: <what you accomplished>`;
  
  return task;
}

function getStepInstructions(stepName, context) {
  const instructions = {
    'spec_ready': `
Review the dining-feedback project and create/update SPEC.md with architecture.

Project path: ${context.projectDir}
Tech stack: React (Vite) + Express + PostgreSQL + Supabase

Steps:
1. Check if SPEC.md exists in the project
2. If not, create SPEC.md with:
   - Architecture overview
   - API endpoints
   - Database schema summary
   - Authentication flow
   - Deployment notes
3. Report your findings
`,

    'implement': `
Implement the dining-feedback project features.

Project path: ${context.projectDir}

Steps:
1. Check backend/src for existing code
2. Check frontend/src for existing code  
3. Report current implementation status
4. Identify any missing components
`,

    'db_review': `
Review the database schema for the dining-feedback project.

Project path: ${context.projectDir}

Steps:
1. Check backend/prisma/schema.prisma
2. Review tables: Feedback, Admin, OAuthSession
3. Check for proper indexes and relations
4. Report any issues or approvals
`,

    'build': `
Build the dining-feedback project Docker images.

Project path: ${context.projectDir}

Steps:
1. Run: docker compose -f ${context.projectDir}/docker-compose.yml build
2. Verify both frontend and backend images built
3. Report build status
`,

    'deploy_staging': `
Deploy dining-feedback to staging.

Project path: ${context.projectDir}

Steps:
1. Check if containers are running: docker ps --filter "name=dining"
2. If not running: docker compose -f ${context.projectDir}/docker-compose.yml up -d
3. Wait 10 seconds
4. Check health: curl -s http://localhost:8081 | head -1
5. Report deployment status
`,

    'test': `
Run QA tests on dining-feedback.

Project path: ${context.projectDir}

Steps:
1. Check frontend: curl -s -o /dev/null -w "%{http_code}" http://localhost:8081
2. Check backend: curl -s -o /dev/null -w "%{http_code}" http://localhost:3010/health
3. Verify database: docker exec dining-db pg_isready -U dining_user
4. Report test results (PASSED if all return 200/ok, FAILED otherwise)
`,

    'hotfix': `
Address a production issue with dining-feedback.

Project path: ${context.projectDir}

Steps:
1. Check container logs: docker logs dining-backend --tail 20
2. Identify the issue
3. Fix and redeploy
4. Verify fix
`
  };
  
  return instructions[stepName] || `Execute step: ${stepName}`;
}

function canExecuteStep(step) {
  const { receives } = step;
  if (!receives) return true;
  const required = [].concat(receives);
  return required.every(e => eventState.emitted.has(e));
}

async function runWorkflow(projectDir, options = {}) {
  console.log(`${C.bright}${C.cyan}`);
  console.log('╔═══════════════════════════════════════════════════════════╗');
  console.log('║  OpenClaw Workflow Runner v3 - Real Agent Execution       ║');
  console.log('╚═══════════════════════════════════════════════════════════╝');
  console.log(`${C.reset}\n`);
  
  const config = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8'));
  
  const context = {
    projectName: path.basename(projectDir),
    projectDir: projectDir,
    timestamp: new Date().toISOString()
  };
  
  log(null, `Workflow: ${config.name} v${config.version}`);
  log(null, `Project: ${context.projectName}`);
  log(null, `Steps: ${config.pipeline.length}`);
  log(null, `Mode: Real agent spawning\n`);
  
  // Filter pipeline if starting from a specific step
  let pipeline = config.pipeline;
  if (options.startStep) {
    const idx = pipeline.findIndex(s => s.step === options.startStep);
    if (idx >= 0) {
      pipeline = pipeline.slice(idx);
      log(null, `Starting from step: ${options.startStep}`, 'yellow');
    }
  }
  
  // Execute pipeline
  const results = [];
  
  for (const step of pipeline) {
    // Check if step can execute
    if (!canExecuteStep(step)) {
      log(step.agent, `Skipping ${step.step} - waiting for: ${step.receives}`, 'dim');
      continue;
    }
    
    try {
      log(step.step, `Starting step with agent: ${step.agent}`, 'yellow');
      
      // Execute step with real agent
      const result = await executeStepWithAgent(step.step, config, context);
      
      // Update event state
      if (result.events) {
        for (const event of result.events) {
          eventState.emitted.add(event);
          log(step.step, `${C.green}Emitted:${C.reset} ${event}`, 'green');
        }
      }
      
      results.push({ 
        step: step.step, 
        agent: step.agent,
        status: 'success',
        events: result.events
      });
      
    } catch (error) {
      log(step.step, `${C.red}Error: ${error.message}${C.reset}`, 'red');
      results.push({ 
        step: step.step, 
        agent: step.agent,
        status: 'failed', 
        error: error.message 
      });
      
      // Stop on failure if configured
      if (config.settings?.stop_on?.includes(step.step)) {
        log(null, `Pipeline stopped at: ${step.step}`, 'red');
        break;
      }
    }
  }
  
  // Summary
  console.log(`\n${C.bright}═══════════════════════════════════════════════════════════${C.reset}`);
  
  const succeeded = results.filter(r => r.status === 'success').length;
  const failed = results.filter(r => r.status === 'failed').length;
  
  log(null, `Pipeline complete: ${succeeded} succeeded, ${failed} failed`, 
      failed > 0 ? 'red' : 'green');
  
  console.log(`\n${C.dim}Events emitted:${C.reset}`);
  eventState.emitted.forEach(e => log(null, `  ${C.green}✓${C.reset} ${e}`, 'green'));
  
  console.log(`\n${C.bright}Step results:${C.reset}`);
  results.forEach(r => {
    const icon = r.status === 'success' ? `${C.green}✓` : `${C.red}✗`;
    log(null, `  ${icon}${C.reset} ${r.step} (${r.agent}): ${r.status}`);
  });
  
  console.log(`\n${C.bright}═══════════════════════════════════════════════════════════${C.reset}\n`);
  
  return { results, events: Array.from(eventState.emitted) };
}

// Main
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
${C.bright}OpenClaw Workflow Runner v3${C.reset}

Usage: node workflow-runner.js <project-dir> [options]

Options:
  --step <name>   Start from a specific step

Example:
  node workflow-runner.js /root/.openclaw/workspace/projects/dining-feedback
  node workflow-runner.js /root/.openclaw/workspace/projects/dining-feedback --step deploy_staging
`);
    process.exit(1);
  }
  
  if (!fs.existsSync(projectDir)) {
    console.log(`${C.red}Error: Project directory not found: ${projectDir}${C.reset}`);
    process.exit(1);
  }
  
  try {
    const result = await runWorkflow(projectDir, { startStep });
    process.exit(result.results.some(r => r.status === 'failed') ? 1 : 0);
  } catch (error) {
    console.log(`${C.red}Error: ${error.message}${C.reset}`);
    process.exit(1);
  }
}

main();