# Phase 3 Spec — Chunking + Embedding

## Dependencies

### `embeddings/requirements.txt`

```
fastapi==0.111.0
uvicorn[standard]==0.29.0
sentence-transformers==3.0.1
httpx==0.27.0
```

### `agents/requirements.txt` (additions for Phase 3)

```
fastapi==0.111.0
uvicorn[standard]==0.29.0
sqlalchemy[asyncio]==2.0.30
asyncpg==0.29.0
tenacity==8.3.0
clickhouse-connect==0.7.16   ← new
httpx==0.27.0                ← new (calls embeddings service)
```

## Environment Variables

### `embeddings/.env.example`

```
EMBEDDING_MODEL=all-MiniLM-L6-v2
EMBEDDING_DIMENSIONS=384
PORT=8090
```

### `agents/.env.example` (additions)

```
RCA_DATABASE_URL=postgresql+asyncpg://rca_agent:changeme@localhost:5433/rca
CLICKHOUSE_HOST=localhost
CLICKHOUSE_PORT=8123
CLICKHOUSE_DATABASE=otel
EMBEDDINGS_URL=http://localhost:8090
PORT=8080
ALERT_WINDOW_MINUTES=10
```

Note: ClickHouse on `localhost:8123` via `kubectl port-forward`.

## Ports

| Service            | Port   | How                                                               |
| ------------------ | ------ | ----------------------------------------------------------------- |
| Orchestrator       | `8080` | uvicorn locally                                                   |
| Embeddings service | `8090` | uvicorn locally                                                   |
| RCA PostgreSQL     | `5433` | Docker Compose                                                    |
| ClickHouse HTTP    | `8123` | `kubectl port-forward -n monitoring svc/clickhouse-svc 8123:8123` |

## New Files

```
embeddings/
├── service.py          # FastAPI embedding HTTP service
├── chunker.py          # router — dispatches by source type
├── log_chunker.py      # event-based + fixed-size fallback
├── trace_chunker.py    # trace_id grouping + span summarization
└── requirements.txt

scripts/
└── phase3-migration.sql   # adds vector extension + embeddings table
```

## `scripts/phase3-migration.sql`

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE embeddings (
    embedding_id     UUID PRIMARY KEY,
    incident_id      UUID REFERENCES incidents(incident_id),
    source           TEXT CHECK (source IN ('logs','traces')),
    content          TEXT,
    metadata         JSONB,
    embedding        vector(384),
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ON embeddings USING hnsw (embedding vector_cosine_ops);

GRANT SELECT, INSERT, UPDATE ON embeddings TO rca_agent;
```

Run via:

```powershell
psql -h localhost -p 5433 -U postgres -d rca -f scripts/phase3-migration.sql
```

## `embeddings/service.py` — Key Structure

```python
from fastapi import FastAPI
from sentence_transformers import SentenceTransformer
import os

app = FastAPI()
model = SentenceTransformer(os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2"))

@app.post("/embed")
async def embed(payload: dict) -> dict:
    # payload: { "texts": [...], "source": "logs"|"traces" }
    # returns: { "embeddings": [[...384 floats...], ...] }
    embeddings = model.encode(payload["texts"]).tolist()
    return {"embeddings": embeddings}

@app.get("/health")
async def health() -> dict:
    return {"status": "healthy"}
```

## `embeddings/chunker.py` — Key Structure

```python
from embeddings.log_chunker import chunk_logs
from embeddings.trace_chunker import chunk_traces

def chunk(rows: list[dict], source: str) -> list[dict]:
    # routes to correct chunker, returns list of:
    # { "content": str, "metadata": dict }
    if source == "logs": return chunk_logs(rows)
    if source == "traces": return chunk_traces(rows)
    raise ValueError(f"Unknown source: {source}")
```

## Chunker Output Schema

Every chunker returns a list of:

```python
{
    "content": str,
    "metadata": {
        "timestamp": str,
        "pod_name": str,
        "namespace": str,
        "trace_id": str | None,
        "span_id": str | None,
        "severity_number": int | None,
        "chunk_sequence": str,    # e.g. "1/3"
    }
}
```

## Log Chunker Logic (`embeddings/log_chunker.py`)

```python
def chunk_logs(rows: list[dict]) -> list[dict]:
    # rows come from ClickHouse otel_logs
    # column names: Timestamp, Body, TraceId, SpanId, ServiceName,
    #               SeverityNumber, LogAttributes

    # Primary: group by (TraceId, SpanId) — skip empty TraceId rows
    # Stack trace detection: keep together lines matching
    #   r'^\s+|Traceback|File |Exception|Error'
    # Supported: Python tracebacks only

    # Fallback: 512 token sliding window, 20% overlap, split at \n
```

## Trace Chunker Logic (`embeddings/trace_chunker.py`)

```python
def chunk_traces(rows: list[dict]) -> list[dict]:
    # rows come from ClickHouse otel_traces
    # column names: Timestamp, TraceId, SpanId, ParentSpanId,
    #               ServiceName, SpanName, Duration (nanoseconds), SpanAttributes

    # Group by TraceId — one chunk per complete trace
    # Per span line: "[ServiceName] SpanName duration=Xms"
    # Add db.system and db.statement if present in SpanAttributes
    # Duration: divide by 1_000_000 to convert ns → ms
    # Truncate to 512 tokens if exceeded
```

## Orchestrator `chunk_and_embed` Function

```python
async def chunk_and_embed(incident_id: UUID, ctx: dict) -> None:
    """
    Called once per incident after insert, before agent fan-out.
    1. Query ClickHouse otel_logs for ctx["pod"] + ctx["namespace"]
       in window [alert_time - window_minutes, alert_time]
    2. Query ClickHouse otel_traces for ctx["service"]
       in same window
    3. Chunk logs via log_chunker
    4. Chunk traces via trace_chunker
    5. POST all chunks to embeddings service /embed
    6. Write vectors to PostgreSQL embeddings table
       tagged with incident_id
    """
```

## ClickHouse Query Time Window

```python
from datetime import datetime, timedelta

alert_time = datetime.fromisoformat(ctx["alert_time"].replace("Z", "+00:00"))
window_start = alert_time - timedelta(minutes=ctx["window_minutes"])
window_end = alert_time

# ClickHouse query filter
# WHERE Timestamp >= '{window_start}' AND Timestamp <= '{window_end}'
```

## pgvector Retrieval (for agents in Phase 4+)

```python
async def query_embeddings(
    incident_id: str,
    source: str,                    # "logs" or "traces"
    query_text: str,                # natural language query
    pod_name: str = None,
    trace_id: str = None,
    limit: int = 5
) -> list[str]:
    # 1. embed query_text via embeddings service
    # 2. run hybrid SQL query
    # 3. return list of content strings
```

```sql
SELECT content, metadata, embedding <=> $1 AS score
FROM embeddings
WHERE incident_id = $2
AND source = $3
AND ($4 IS NULL OR metadata->>'pod_name' = $4)
AND ($5 IS NULL OR metadata->>'trace_id' = $5)
ORDER BY score
LIMIT 5;
```

## Makefile Additions

```makefile
local-run-embeddings:
	cd embeddings && .venv\Scripts\Activate.ps1 && uvicorn service:app --port 8090 --reload

port-forward-clickhouse:
	kubectl port-forward -n monitoring svc/clickhouse-svc 8123:8123
```

## Local Dev Commands

```powershell
# terminal 1 — keep running
kubectl port-forward -n monitoring svc/clickhouse-svc 8123:8123

# terminal 2 — embeddings service
cd embeddings
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn service:app --port 8090 --reload

# terminal 3 — orchestrator
cd agents
uvicorn orchestrator:app --port 8080 --reload
```

## Verification

```sql
-- connect: psql -h localhost -p 5433 -U rca_agent -d rca

-- check embeddings were written
SELECT incident_id, source, count(*), created_at
FROM embeddings
GROUP BY incident_id, source, created_at
ORDER BY created_at DESC;

-- check metadata is correct
SELECT source, metadata->>'pod_name', metadata->>'trace_id',
       metadata->>'chunk_sequence'
FROM embeddings LIMIT 10;

-- test retrieval (replace vector with actual embedding)
SELECT content, metadata->>'pod_name', embedding <=> '[0.1, 0.2, ...]'::vector AS score
FROM embeddings
WHERE source = 'logs'
ORDER BY score LIMIT 5;
```

## Gotchas

- `model.encode(texts)` returns numpy array — call `.tolist()` before returning as JSON
- `embeddings` service has ~2s cold start — start it before the orchestrator
- ClickHouse `Duration` is nanoseconds — divide by `1_000_000` for ms in trace chunker
- ClickHouse column names: `Body` not `message`, `SpanName` not `operation`, `SpanAttributes` not `attributes`
- HNSW index — do not change to IVFFlat, silently fails on small datasets
- `source` in embeddings table is `logs` or `traces` only — no `metrics`
- Phase 3 migration must run against existing Docker Compose PostgreSQL — not a fresh init
- Run migration: `psql -h localhost -p 5433 -U postgres -d rca -f scripts/phase3-migration.sql`
