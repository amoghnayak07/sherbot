# Phase 2 — Orchestrator Shell

## Goal

Build the orchestrator webhook receiver and PostgreSQL incident registry. Phase 2 is
complete when a manually POSTed webhook creates an incident record in PostgreSQL,
duplicate webhooks are rejected, and resolved webhooks set `fixed = TRUE`.

No agents, no LangGraph, no fan-out. Just the plumbing that will drive the full
pipeline in later phases.

---

## What's New in Phase 2

```
k3d cluster (sherbot)
├── demo namespace
│   └── backend pod               ← unchanged from Phase 1
└── monitoring namespace
    ├── otel-collector pod         ← unchanged
    └── clickhouse pod             ← unchanged

Your local machine
├── docker-compose
│   └── postgres-rca container    ← new (pgvector:pg16)
└── agents/orchestrator.py        ← new (runs via uvicorn)
```

---

## PostgreSQL (RCA)

- **Image:** `pgvector/pgvector:pg16`
- **Runs:** locally via Docker Compose — not in k3d
- **Port:** `5432` on `localhost`
- **No port-forward needed** — Docker container is directly accessible on your machine
- **Data persistence:** Docker volume — survives container restarts

### Why Docker Compose and not k3d

Orchestrator and all future agents run locally. Nothing in the RCA pipeline runs
inside the cluster. Keeping PostgreSQL local removes the need for `kubectl port-forward`
and keeps all local services together.

### Tables in Phase 2

```
incidents       → incident registry + deduplication
agent_runs      → per-agent run tracking (empty until Phase 4)
rca_reports     → RCA report storage (empty until Phase 4)
```

`embeddings` table and `CREATE EXTENSION vector` added in Phase 3 — not here.

---

## Orchestrator

- **File:** `agents/orchestrator.py`
- **Framework:** FastAPI
- **Port:** `8080` (local)
- **Runs:** locally via uvicorn

### Responsibilities in Phase 2

1. Receive Alertmanager webhook POST at `/webhook`
2. Pre-generate `incident_id` as UUID before any DB write
3. Check PostgreSQL for duplicate `in_flight` incident — reject if exists
4. Insert new incident record with status `in_flight`
5. Log all activity as structured JSON to stdout
6. Handle resolved webhook — set `fixed = TRUE` on matching incident
7. Always return 200 — Alertmanager retries on non-200

### What it does NOT do in Phase 2

- No LangGraph graph
- No agent fan-out
- No RCA report generation
- No embeddings

---

## Webhook Deduplication

```
1. pre-generate incident_id = uuid4()
2. query: SELECT status FROM incidents
          WHERE alert_name=$1 AND pod=$2 AND namespace=$3
          AND status='in_flight' AND fixed=FALSE
3. if row exists → log "duplicate rejected" → return 200
4. if not → INSERT incident → log "incident created" → return 200
```

---

## Resolved Webhook

Alertmanager sends `"status": "resolved"` when an alert clears.
Iterate `payload["alerts"]` and check `alert["status"]` per alert —
not the top-level `payload["status"]`.

On resolved:

```sql
UPDATE incidents SET fixed=TRUE
WHERE alert_name=$1 AND pod=$2 AND namespace=$3
AND status != 'in_flight'
```

---

## Structured Logging

Every log line is JSON to stdout. No `print()`.

```json
{
  "timestamp": "...",
  "level": "INFO",
  "component": "orchestrator",
  "message": "Incident created",
  "incident_id": "uuid",
  "alert_name": "PodOOMKilled",
  "pod": "backend-xyz"
}
```

---

## Testing in Phase 2

No Alertmanager yet. Test by manually POSTing webhooks:

```powershell
# firing webhook
curl -X POST http://localhost:8080/webhook `
  -H "Content-Type: application/json" `
  -d '{\"status\":\"firing\",\"alerts\":[{\"status\":\"firing\",\"labels\":{\"alertname\":\"PodOOMKilled\",\"pod\":\"backend-xyz\",\"namespace\":\"demo\",\"scenario\":\"oom-kill\",\"service\":\"backend\"},\"startsAt\":\"2026-01-01T10:00:00Z\"}]}'

# duplicate — should be rejected
curl -X POST http://localhost:8080/webhook `
  -H "Content-Type: application/json" `
  -d '{\"status\":\"firing\",\"alerts\":[{\"status\":\"firing\",\"labels\":{\"alertname\":\"PodOOMKilled\",\"pod\":\"backend-xyz\",\"namespace\":\"demo\"},\"startsAt\":\"2026-01-01T10:00:00Z\"}]}'

# resolved webhook
curl -X POST http://localhost:8080/webhook `
  -H "Content-Type: application/json" `
  -d '{\"status\":\"resolved\",\"alerts\":[{\"status\":\"resolved\",\"labels\":{\"alertname\":\"PodOOMKilled\",\"pod\":\"backend-xyz\",\"namespace\":\"demo\"},\"endsAt\":\"2026-01-01T10:05:00Z\"}]}'
```

---

## Phase 2 Completion Criteria

- [x] `docker-compose up -d` starts PostgreSQL successfully
- [x] All three tables exist in PostgreSQL
- [x] Orchestrator starts locally with `uvicorn agents.orchestrator:app --port 8080 --reload`
- [x] POST firing webhook → incident row inserted with `status=in_flight`
- [x] POST same webhook again → rejected, no duplicate row
- [x] POST resolved webhook → `fixed=TRUE` on matching incident
- [x] All activity logged as structured JSON to stdout
- [x] PostgreSQL writes retry on failure via Tenacity
