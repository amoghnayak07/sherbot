# Sherbot — Claude Code Guide

Sherbot is a locally-hosted Kubernetes observability platform that uses a LangGraph
multi-agent system to automatically investigate and produce root cause analysis reports
for incidents in a distributed application running on k3d.

**For each phase, read only the two docs for that phase:**

```
docs/0N-<phase>.md           → architecture, decisions, completion criteria
docs/0N-<phase>-spec.md      → dependencies, env vars, signatures, gotchas
```

---

## Build Phases

Work in this order. Do not jump phases.

| Phase | Scope                                                                              | Docs        |
| ----- | ---------------------------------------------------------------------------------- | ----------- |
| 1     | k3d cluster + backend service + OTel Collector + ClickHouse                        | `docs/01-*` |
| 2     | Orchestrator — webhook receiver + PostgreSQL registry + dedup + logging            | `docs/02-*` |
| 3     | pgvector + chunking + embedding service                                            | `docs/03-*` |
| 4     | Metrics agent + RCA agent                                                          | `docs/04-*` |
| 5     | Log agent                                                                          | `docs/05-*` |
| 6     | App behavior agent                                                                 | `docs/06-*` |
| 7     | React dashboard                                                                    | `docs/07-*` |
| 8+    | Frontend service, blast radius agent, alerting pipeline, full RBAC, network policy | TBD         |

### What is active per phase

| Component                                | Active from phase |
| ---------------------------------------- | ----------------- |
| k3d cluster                              | 1                 |
| Backend service (FastAPI)                | 1                 |
| OTel Collector                           | 1                 |
| ClickHouse                               | 1                 |
| Orchestrator + agents image + PostgreSQL | 2                 |
| Embeddings service + pgvector            | 3                 |
| Metrics agent + RCA agent                | 4                 |
| Log agent                                | 5                 |
| App behavior agent                       | 6                 |
| Dashboard                                | 7                 |
| Frontend, blast radius, alerting         | 8+                |

---

## Core Commands

### Install only — Claude Code may run these

```powershell
# Python — always install into the component's local venv, never globally
cd <component>
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install --no-cache-dir -r requirements.txt

# Node
npm ci --prefix dashboard
```

### Everything else — human runs only

```powershell
# Cluster
k3d cluster create sherbot
k3d cluster delete sherbot
k3d image import <image> -c sherbot

# Docker builds
docker build -t <image>:latest ./<component>

# Kubernetes
kubectl apply -f k8s/<manifest>.yaml
kubectl get pods -n <namespace>
kubectl get pods -A
kubectl logs -n <namespace> <pod> -f
kubectl logs -n <namespace> -l app=<label> -f
kubectl describe pod -n <namespace> <pod>
kubectl exec -n <namespace> -it <pod> -- <command>
kubectl port-forward -n <namespace> svc/<svc> <local>:<remote>

# Scenario triggers
curl http://localhost:<port>/<endpoint>
```

---

## Project Structure

```
services/       → demo distributed app (backend first, frontend in phase 8+)
agents/         → orchestrator + all LangGraph agents in one image (phase 2+)
embeddings/     → sentence-transformers HTTP service + chunkers (phase 3+)
dashboard/      → React (Vite + Tailwind) + FastAPI API (phase 7+)
tools/          → shared clients — clickhouse, postgres, k8s (grows each phase)
k8s/            → all Kubernetes manifests
scenarios/      → scenario trigger docs
docs/           → phase-scoped design + spec docs
```

---

## Code Style Rules

- **Python:** 3.11. Type hints on all function signatures. `async/await` throughout — no sync blocking calls.
- **venv:** Every Python component has its own `.venv/`. Never install packages globally. Dockerfiles do not use venv — containers are the isolation layer.
- **Logging:** JSON formatter to stdout on every component. Use `log.info/warning/error` with structured `extra={}` fields. No `print()`.
- **Pydantic:** Use `model_validate()` not `parse_obj()`. All agent outputs validated against schema before writing to PostgreSQL.
- **Environment variables:** Always via `os.getenv()` with a sensible default. Never hardcoded.
- **SQL:** Raw SQL via SQLAlchemy `text()` for PostgreSQL. `clickhouse-connect` async client for ClickHouse. No ORM models.
- **Error handling:** All `asyncio.gather` calls use `return_exceptions=True`. Check `isinstance(result, Exception)` before accessing result fields.
- **Kubernetes client:** `config.load_incluster_config()` inside pods. Never assume kubeconfig exists.
- **React:** Functional components only. TailwindCSS only — no inline styles, no CSS files.
- **PowerShell:** Use `Select-String` instead of `grep`. Use `.venv\Scripts\Activate.ps1` not `source .venv/bin/activate`.

---

## Project Gotchas

- **One PostgreSQL instance** in `rca-system` — agent state + embeddings only. No demo PostgreSQL. DB bottleneck scenario is simulated via `asyncio.sleep(4)` inside a properly attributed span — no real database needed.
- **OTel Collector image.** Must use `otel/opentelemetry-collector-contrib` — not the core image. Core image has no ClickHouse exporter.
- **Single image for orchestrator + all agents.** Everything lives in `agents/` — one `Dockerfile`, one `requirements.txt`. The orchestrator is `agents/orchestrator.py`. Kubernetes `command` field selects which module runs per pod. Do not create separate Dockerfiles.
- **`os._exit(1)` in crash endpoint (phase 8+).** Intentional — `sys.exit()` is caught by FastAPI and returns a 500 without restarting the pod.
- **`FastAPIInstrumentor.instrument_app(app)`** must be called after `app = FastAPI()` and before any route decorators.
- **`SQLAlchemyInstrumentor().instrument(engine=engine)`** must be called after the engine is created.
- **ClickHouse TTL is 24 hours.** Data expires. Run scenarios and investigations in the same session.
- **pgvector index is HNSW (phase 3+).** Do not change to IVFFlat — silently fails on small datasets.
- **`sending_queue` not `batch` processor** before ClickHouse exporter. Double batching causes instability.
- **Schema changes:** Delete the PVC and restart the pod. There is no migration tooling.
- **Chunking and embedding happens once per incident** in the orchestrator before agent fan-out. Agents only retrieve from pgvector — they never embed.
- **Metrics are not embedded.** Metrics agent queries ClickHouse directly and summarizes structured numbers. Only logs and traces go through pgvector.

---

## Kill Criteria

Stop and flag to the human immediately if any of the following are true:

- Any agent pod queries ClickHouse and passes raw rows directly to an LLM prompt — must chunk, embed, retrieve first
- Any manifest applies `create`, `update`, `delete`, or `patch` verbs to any RBAC rule — strictly `get`, `list`, `watch`
- Any secret or API key hardcoded in any file that could be committed — all secrets via Kubernetes Secret or `.env`
- Any agent receives raw ClickHouse rows directly in its LLM prompt — must chunk, embed, retrieve first
- `GROQ_API_KEY` appears anywhere outside `.env` or a Kubernetes Secret
- Packages installed globally instead of into component `.venv`

---

## Development Workflow

### Phase 1

Everything runs in k3d — backend pod, OTel Collector, ClickHouse.

### Phases 2–7 (hybrid local development)

Demo services and telemetry stack run in k3d. Everything else runs locally on your machine — no Docker builds needed on every change, full debugger access, hot reload.

**In k3d (always):**

```
demo namespace      → backend pod
monitoring namespace → otel-collector + clickhouse
```

**On your machine (locally):**

```
orchestrator   → uvicorn agents/orchestrator:app --port 8080 --reload
embeddings     → uvicorn embeddings/service:app --port 8090 --reload
agents         → python -m agents.metrics (etc.)
dashboard api  → uvicorn dashboard/api/main:app --port 8002 --reload
dashboard ui   → npm run dev --prefix dashboard
```

**Expose in-cluster services to localhost via port-forward:**

```powershell
# run each in a separate terminal, keep them open
kubectl port-forward -n monitoring svc/clickhouse-svc 9000:9000 8123:8123
kubectl port-forward -n monitoring svc/otel-collector-svc 4317:4317
kubectl port-forward -n rca-system svc/postgres-rca-svc 5432:5432
```

**Local `.env` files use `localhost` instead of in-cluster DNS:**

```env
# agents/.env (local dev)
CLICKHOUSE_HOST=localhost
CLICKHOUSE_PORT=8123
RCA_DATABASE_URL=postgresql+asyncpg://rca_agent:...@localhost:5432/rca
EMBEDDINGS_URL=http://localhost:8090
```

**Alertmanager webhook** (Phase 4+) reaches your local orchestrator via `host.docker.internal` — Docker Desktop for Windows resolves this to your local machine automatically. No ngrok needed.

```yaml
# k8s/alertmanager.yaml
receivers:
  - name: rca-orchestrator
    webhook_configs:
      - url: "http://host.docker.internal:8080/webhook"
```

### Phase 8+

Containerize and deploy everything to k3d once each phase is stable.

The human controls all git operations. Claude Code does not commit, push, branch, or open PRs.
When a phase is complete, flag it clearly so the human can review and commit.

---

## Environment Variables

Each component has its own `.env.example`. Copy to `.env` before running locally.
`.env` files are gitignored — never commit them.
In-cluster, env vars are injected via Kubernetes `ConfigMap` or `Secret` references in deployment manifests.
