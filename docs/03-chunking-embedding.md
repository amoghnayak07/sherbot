# Phase 3 — Chunking + Embedding

## Goal

Wire chunking and embedding into the orchestrator. When a webhook arrives, the
orchestrator queries ClickHouse for logs and traces in the alert time window,
chunks them, embeds them via the embeddings service, and writes vectors to
PostgreSQL pgvector. Phase 3 is complete when a webhook produces embedded chunks
retrievable via cosine similarity from PostgreSQL.

No agents yet — just proving the pre-processing pipeline works end-to-end.

---

## What's New in Phase 3

```
k3d cluster (sherbot)
├── demo namespace
│   └── backend pod               ← unchanged
└── monitoring namespace
    ├── otel-collector pod         ← unchanged
    └── clickhouse pod             ← unchanged

Your local machine
├── docker-compose
│   └── postgres-rca (port 5433)  ← unchanged
├── agents/orchestrator.py        ← updated — triggers chunk+embed after incident insert
├── embeddings/service.py         ← new — HTTP embedding service
├── embeddings/chunker.py         ← new — router
├── embeddings/log_chunker.py     ← new
└── embeddings/trace_chunker.py   ← new
```

---

## Updated Orchestrator Flow

```
webhook arrives
      ↓
deduplication check
      ↓
insert incident (status=in_flight)
      ↓
chunk_and_embed(incident_id, ctx)   ← new in Phase 3
      ↓
log "pre-processing complete, ready for agents"
      ↓
return 200
```

`chunk_and_embed` is called once per incident before any agent runs.
It queries ClickHouse, chunks logs and traces, embeds via embeddings service,
and writes all vectors to the `embeddings` table tagged with `incident_id`.

---

## Embeddings Service

- **File:** `embeddings/service.py`
- **Model:** `all-MiniLM-L6-v2` — local, ~80MB, 384 dimensions, no external API call
- **Port:** `8090` (local)
- **Runs:** locally via uvicorn
- **Role:** single model load, all chunkers call it via HTTP

```
POST /embed
Input:  { "texts": ["chunk 1", "chunk 2"], "source": "logs"|"traces" }
Output: { "embeddings": [[...384 floats...], [...384 floats...]] }

GET /health
Output: { "status": "healthy" }
```

---

## PostgreSQL — New in Phase 3

`CREATE EXTENSION vector` and `embeddings` table added to the schema.
Run via a migration script against the existing Docker Compose PostgreSQL.

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

Note: `source` is `logs` or `traces` only — no `metrics`. Metrics agent queries
ClickHouse directly without embedding.

---

## Chunking Strategy

### What gets chunked

- **Logs** — from `otel_logs` table, scoped to alert pod + namespace + time window
- **Traces** — from `otel_traces` table, scoped to alert service + time window
- **Metrics** — NOT chunked or embedded. Metrics agent queries ClickHouse directly.

### Log Chunking

**Primary — event-based (logs with trace context):**

- Group rows by `(TraceId, SpanId)`
- All log lines for one request travel as one chunk
- Stack trace boundary detection — Python tracebacks kept together

**Fallback — fixed-size token window (no TraceId):**

- 512 tokens per chunk, 20% overlap (102 tokens)
- Split at log line boundary

### Trace Chunking

- Group all spans by `TraceId` — one chunk per complete trace
- Per span line: `[ServiceName] SpanName duration=Xms db.system=Y db.statement=Z`
- Truncate to 512 tokens if exceeded

### Chunk Metadata (stored in embeddings.metadata JSONB)

```json
{
  "timestamp": "2026-07-01T14:45:00Z",
  "pod_name": "backend-xyz",
  "namespace": "demo",
  "trace_id": "7b3b9b4859",
  "span_id": "abc123",
  "severity_number": 17,
  "chunk_sequence": "1/3"
}
```

---

## Hybrid Retrieval (used by agents in Phase 4+)

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

- `pod_name` filter for log chunks
- `trace_id` filter for trace chunks
- Both nullable — retrieval never blocked if metadata missing

---

## ClickHouse Column Names (real — from Phase 1 discoveries)

Chunkers must use these exact column names:

**`otel_logs`:** `Timestamp`, `Body`, `TraceId`, `SpanId`, `ServiceName`,
`SeverityNumber`, `LogAttributes`

**`otel_traces`:** `Timestamp`, `TraceId`, `SpanId`, `ParentSpanId`,
`ServiceName`, `SpanName`, `Duration` (nanoseconds), `SpanAttributes`

---

## Phase 3 Completion Criteria

- [ ] `embeddings/service.py` starts locally on port `8090`
- [ ] `POST /embed` returns 384-dimension vectors
- [ ] `embeddings` table + HNSW index exist in PostgreSQL
- [ ] `CREATE EXTENSION vector` applied to existing database
- [ ] After firing webhook + `GET /slow` on backend, orchestrator queries ClickHouse
- [ ] Log chunks written to `embeddings` table with `source=logs`
- [ ] Trace chunks written to `embeddings` table with `source=traces`
- [ ] Hybrid retrieval query returns top-5 semantically relevant chunks
- [ ] All chunks tagged with correct `incident_id`
