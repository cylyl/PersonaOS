# PersonaOS — Getting Started Guide

A quick, practical guide to using PersonaOS right now.

---

## 1. What Is PersonaOS in 30 Seconds?

Instead of running one-off AI chat prompts that disappear when closed, PersonaOS gives you **persistent digital AI employees**:
- **Workers** (e.g. Developer, DevOps, QA) that have names, skills, and memory.
- A **Task Queue** where you give workers tasks (with deadlines, retries, and priorities).
- **Worker Runners** that pick up tasks, execute them safely, and save checkpoints.
- A **Web Dashboard** where you can see all your AI workers, monitor queue progress, and approve sensitive actions.

---

## 2. Open PersonaOS in Your Browser Right Now

The system is already running on machine `192.168.1.239`. Open these URLs in your web browser:

| Interface | URL | What It Is For |
|---|---|---|
| **Web Dashboard** | **[http://192.168.1.239:8000/dashboard/ui](http://192.168.1.239:8000/dashboard/ui)** | Live visual dashboard showing workers, active tasks, costs, and health |
| **Interactive API Docs** | **[http://192.168.1.239:8000/docs](http://192.168.1.239:8000/docs)** | Swagger UI to click and test every API endpoint directly in your browser |
| **Health Check** | **[http://192.168.1.239:8000/health](http://192.168.1.239:8000/health)** | Quick check to verify database and API are healthy |

---

## 3. Five-Minute Hands-On Walkthrough

### Step 1: See Your Active Workers
Open your terminal and run:
```bash
curl -s http://192.168.1.239:8000/workers | jq
```
You will see worker `worker-dev-01` (Alex Mercer — Senior Fullstack Engineer).

---

### Step 2: Give Your Worker a Task
Send a task to `worker-dev-01`:

```bash
curl -X POST http://192.168.1.239:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Analyze Repository Security",
    "objective": "Audit dependencies and check for outdated packages.",
    "worker_id": "worker-dev-01",
    "priority": "high",
    "side_effect_classification": "IDEMPOTENT"
  }'
```

---

### Step 3: Watch It Run and Complete
1. **Refresh your browser dashboard:**  
   Go to [http://192.168.1.239:8000/dashboard/ui](http://192.168.1.239:8000/dashboard/ui). You will see the task count increase under **Completed**.
2. **Or check from terminal:**  
   ```bash
   curl -s http://192.168.1.239:8000/tasks | jq '.[0]'
   ```
   You will see:
   - `"status": "completed"`
   - `"assigned_worker": "worker-dev-01"`
   - Timestamp when it was claimed and completed.

---

### Step 4: Add a Second Digital Worker (e.g. DevOps)
Onboard another worker with different skills:

```bash
curl -X POST http://192.168.1.239:8000/workers \
  -H "Content-Type: application/json" \
  -d '{
    "id": "worker-devops-01",
    "name": "Jordan Vance",
    "role": "devops",
    "description": "Kubernetes and CI/CD Specialist",
    "skills": ["docker", "kubernetes", "git"],
    "persona": {
      "style": "cautious",
      "communication": "concise",
      "autonomy": "high",
      "behavior": {"reliability": "strict"}
    }
  }'
```

Now `worker-devops-01` will show up in your Web Dashboard table.

---

## 4. How to Start, Stop, or Restart the Services

If you ever restart the machine or want to run everything yourself, here are the simple commands:

### A. One-Time Setup (if starting on a fresh machine)
```bash
# 1. Go to repository folder
cd /root/orca/workspaces/PersonaOS/yabby

# 2. Activate virtual environment
source .venv/bin/activate

# 3. Run database migrations to prepare tables
alembic -c src/personaos/db/migrations/alembic.ini upgrade head
```

### B. Start the Control Plane API (Port 8000)
```bash
# Run in foreground (or run with systemd / nohup):
.venv/bin/python -m personaos.api.app
```

### C. Start Worker Runner(s)
In another terminal, start a worker runner process that executes tasks for a worker:
```bash
# 1. Real LLM execution (LiteLLM proxy routing to Qwen 2.5 Coder 14B AWQ):
.venv/bin/personaos-worker \
  --worker-id se \
  --node-id node-se-01 \
  --adapter litellm \
  --model qwen2.5-coder-14b \
  --api-base http://localhost:4000/v1 \
  --api-key sk-master-dev-key

# 2. Simulated execution (50ms fast mocks for workflow/queue unit testing):
.venv/bin/personaos-worker --worker-id worker-dev-01 --node-id node-local-01 --adapter simulation
```
*(You can run multiple workers simultaneously for different workers or skills).*

---

## 5. How to Deploy to Your K3s Cluster on PVE2

If you want to run PersonaOS permanently in your existing K3s cluster on PVE2 (`cyl-dc`), the Kubernetes manifests are already prepared in `deploy/k8s/`:

```bash
# Apply all manifests to K3s cluster master (192.168.1.150):
ssh user@192.168.1.150 "sudo kubectl apply -k https://github.com/cylyl/PersonaOS.git/deploy/k8s?ref=main"
```

This will automatically:
- Run PostgreSQL on Longhorn persistent storage.
- Pin all API and worker pods strictly to PVE2 nodes (`k3s03` and `k3s04`).
- Expose the API and Dashboard through Traefik ingress at `http://192.168.1.150/dashboard/ui`.
