# Phase 2 Spec — Orchestrator Shell

## Dependencies

### `agents/requirements.txt` (Phase 2 only)

```
fastapi==0.111.0
uvicorn[standard]==0.29.0
sqlalchemy[asyncio]==2.0.30
asyncpg==0.29.0
tenacity==8.3.0
```

## Environment Variables

### `agents/.env.example`

```
RCA_DATABASE_URL=postgresql+asyncpg://rca_agent:changeme@localhost:5432/rca
PORT=8080
```

## Ports

| Service        | Port   | How                         |
| -------------- | ------ | --------------------------- |
| Orchestrator   | `8080` | uvicorn locally             |
| RCA PostgreSQL | `5432` | Docker Compose on localhost |

## New Files

```
docker-compose.yml          # repo root — PostgreSQL local service
agents/
├── orchestrator.py         # FastAPI webhook receiver
└── requirements.txt        # Phase 2 deps only
```

No new k8s manifests in Phase 2 — PostgreSQL runs via Docker Compose.

## `docker-compose.yml`

```yaml
services:
  postgres-rca:
    image: pgvector/pgvector:pg16
    container_name: sherbot-postgres
    ports:
      - "5432:5432"
    environment:
      POSTGRES_DB: rca
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: changeme
    volumes:
      - postgres-rca-data:/var/lib/postgresql/data
      - ./scripts/postgres-init.sql:/docker-entrypoint-initdb.d/init.sql

volumes:
  postgres-rca-data:
```

## `scripts/postgres-init.sql`

```sql
-- No vector extension yet — added in Phase 3

CREATE TABLE incidents (
    incident_id      UUID PRIMARY KEY,
    alert_name       TEXT NOT NULL,
    pod              TEXT NOT NULL,
    namespace        TEXT NOT NULL,
    scenario         TEXT,
    status           TEXT CHECK (status IN ('in_flight','completed','failed')),
    fixed            BOOLEAN DEFAULT FALSE,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE agent_runs (
    run_id           UUID PRIMARY KEY,
    incident_id      UUID REFERENCES incidents(incident_id),
    agent_name       TEXT NOT NULL,
    status           TEXT CHECK (status IN ('pending','running','completed','failed')),
    confidence       FLOAT CHECK (confidence BETWEEN 0.0 AND 1.0),
    data_found       BOOLEAN,
    completed_at     TIMESTAMPTZ
);

CREATE TABLE rca_reports (
    report_id            UUID PRIMARY KEY,
    incident_id          UUID REFERENCES incidents(incident_id),
    root_cause           TEXT,
    blast_radius         JSONB,
    contributing_factors JSONB,
    remediation_steps    JSONB,
    overall_confidence   FLOAT,
    status               TEXT DEFAULT 'completed',
    raw_output           TEXT,
    created_at           TIMESTAMPTZ DEFAULT NOW()
);

CREATE USER rca_agent WITH PASSWORD 'changeme';
GRANT SELECT, INSERT, UPDATE ON incidents, agent_runs, rca_reports TO rca_agent;
```

Note: `embeddings` table, `CREATE EXTENSION vector`, and GRANT on embeddings added in Phase 3.

## `agents/orchestrator.py` — Key Structure

```python
import asyncio, logging, json, os
from uuid import uuid4
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text
from tenacity import retry, stop_after_attempt, wait_exponential

# JSON formatter — all logs to stdout
class JSONFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "component": "orchestrator",
            "message": record.getMessage(),
            **getattr(record, "extra", {})
        })

engine = create_async_engine(os.getenv("RCA_DATABASE_URL"))
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
app = FastAPI()

@app.post("/webhook")
async def handle_webhook(payload: dict) -> dict:
    for alert in payload["alerts"]:            # iterate alerts, not top-level payload
        if alert["status"] == "resolved":
            await mark_incident_resolved(alert)
            continue
        incident_id = uuid4()                  # pre-generate before DB insert
        if await check_in_flight(alert):
            log.info("Duplicate rejected", extra={"alert_name": alert["labels"]["alertname"]})
            continue
        await insert_incident(incident_id, alert)
    return {"status": "ok"}

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
async def write_to_postgres(query: str, params: dict) -> None: ...

async def check_in_flight(alert: dict) -> bool: ...
async def insert_incident(incident_id, alert: dict) -> None: ...
async def mark_incident_resolved(alert: dict) -> None: ...
```

## Makefile Additions

```makefile
local-up:
	docker-compose up -d

local-down:
	docker-compose down

local-run-orchestrator:
	cd agents && .venv\Scripts\Activate.ps1 && uvicorn orchestrator:app --port 8080 --reload
```

## Local Dev Commands

```powershell
# start PostgreSQL
docker-compose up -d

# set up venv and install deps
cd agents
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# run orchestrator
uvicorn orchestrator:app --port 8080 --reload
```

## Verification

```sql
-- connect: psql -h localhost -U rca_agent -d rca

-- check incident created
SELECT incident_id, alert_name, pod, status, fixed, created_at
FROM incidents ORDER BY created_at DESC LIMIT 5;

-- check no duplicates
SELECT alert_name, pod, namespace, count(*)
FROM incidents
GROUP BY alert_name, pod, namespace;

-- check resolved set fixed=TRUE
SELECT incident_id, alert_name, fixed FROM incidents;
```

## Gotchas

- `rca-system` namespace does not exist in Phases 1-7 — PostgreSQL runs via Docker Compose
- No k8s manifests for PostgreSQL — Docker Compose only
- `agents/` only has `orchestrator.py` in Phase 2 — other agents added in Phases 4-6
- `embeddings` table and `vector` extension added in Phase 3 — do not add here
- Always pre-generate `incident_id = uuid4()` before PostgreSQL insert
- Iterate `payload["alerts"]` — check `alert["status"]` per alert, not top-level payload
- Schema changes: `docker-compose down -v && docker-compose up -d` resets the volume
