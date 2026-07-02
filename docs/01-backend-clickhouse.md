# Phase 1 — Backend Service + OTel Collector + ClickHouse

## Goal

Get one FastAPI service running in k3d, emitting telemetry through the OTel Collector
into ClickHouse. Phase 1 is complete when hitting a backend endpoint produces a
visible trace in `otel.traces` and a log line in `otel.logs` in ClickHouse.

No agents, no alerts, no PostgreSQL, no frontend. Just one pod producing telemetry
that lands in ClickHouse.

---

## What Runs in Phase 1

```
k3d cluster (sherbot)
├── demo namespace
│   └── backend pod (FastAPI)       ← your service
└── monitoring namespace
    ├── otel-collector pod           ← DaemonSet, receives telemetry
    └── clickhouse pod               ← StatefulSet, stores telemetry
```

---

## Backend Service

- **Language:** Python 3.11 + FastAPI + uvicorn
- **Port:** `8001`
- **Namespace:** `demo`
- **OTel instrumented:** traces + structured JSON logs
- **Calls:** nothing — standalone in Phase 1 (no frontend, no database)

### Endpoints

| Endpoint      | Purpose                                                                    |
| ------------- | -------------------------------------------------------------------------- |
| `GET /health` | Liveness check — returns `{"status":"healthy","service":"backend"}`        |
| `GET /data`   | Normal traffic — returns `{"data":"ok","service":"backend"}`               |
| `GET /slow`   | Simulates slow DB call — sleeps 4s, emits span with `db.system=postgresql` |

`/oom` and `/crash` endpoints are **not implemented in Phase 1** — added in Phase 8+.

### OTel Instrumentation

Manual SDK setup — not auto-instrumentation.

**What gets instrumented:**

- All HTTP routes via `FastAPIInstrumentor`
- The artificial DB call in `/slow` via a manual span with `db.system`, `db.statement` attributes

**What gets emitted:**

- Traces → OTel Collector gRPC `4317` → ClickHouse `otel.traces`
- Logs → stdout → OTel Collector filelogreceiver → ClickHouse `otel.logs`

---

## OTel Collector

- **Image:** `otel/opentelemetry-collector-contrib` — required for ClickHouse exporter
- **Deployment:** DaemonSet in `monitoring`
- **Receivers active in Phase 1:**
  - `filelogreceiver` — tails `/var/log/pods/**/*.log`, strips CRI prefix, parses JSON
  - `otlp` — gRPC `0.0.0.0:4317`, receives traces from backend
- **Receivers deferred to Phase 8+:** `kubeletstats`, `hostmetrics`, `k8sobjects`
- **Processor:** `k8sattributes` — enriches signals with `k8s.pod.name`, `k8s.namespace.name`
- **Exporter:** `clickhouse` → native protocol `clickhouse-svc.monitoring:9000`
- **Crash resilience:** `sending_queue` with `file_storage` extension

---

## ClickHouse

- **Deployment:** StatefulSet in `monitoring` + PVC
- **Schema:** auto-created by the `clickhouse` exporter on first connection — no init SQL. The
  exporter's `INSERT` statements are hardcoded against its own fixed column names, so we do not
  define our own table schema; fighting that would mean re-deriving the exporter's exact column
  set ourselves for no benefit.
- **TTL:** set via `ttl: 24h` on the exporter config — applies to all exporter-managed tables
- **Tables active in Phase 1:**
  - `otel.otel_logs` — backend stdout logs (`Timestamp`, `TraceId`, `SpanId`, `ServiceName`, `Body`,
    `LogAttributes`, plus materialized `__otel_materialized_k8s.*` columns from `ResourceAttributes`)
  - `otel.otel_traces` — backend HTTP + manual DB spans (`Timestamp`, `TraceId`, `SpanId`,
    `ServiceName`, `SpanName`, `SpanAttributes`, `Duration` in nanoseconds, `StatusCode`, etc.)
  - `otel.otel_metrics` — not created until Phase 4+, when a metrics pipeline is added

---

## Data Flow

```
backend pod
├── stdout logs (JSON)
│     ↓ filelogreceiver (tails /var/log/pods)
│     ↓ k8sattributes processor
│     ↓ clickhouse exporter
│     → otel.otel_logs
│
└── OTLP traces (gRPC :4317)
      ↓ otlp receiver
      ↓ k8sattributes processor
      ↓ clickhouse exporter
      → otel.otel_traces
```

---

## How to Verify

```powershell
# 1. Check pods are running
kubectl get pods -A

# 2. Hit the backend
kubectl port-forward -n demo svc/backend-svc 8001:8001
curl http://localhost:8001/health
curl http://localhost:8001/slow

# 3. Open ClickHouse shell
kubectl exec -n monitoring -it statefulset/clickhouse -- clickhouse-client --database otel

# 4. Run verification queries (see spec doc)
```

---

## Namespace and RBAC Scope

**Phase 1 namespaces:**

- `demo` — backend pod
- `monitoring` — OTel Collector + ClickHouse

**RBAC in Phase 1:**

- One ClusterRole: `otel-collector-kubelet` — for `k8sattributes` processor
- One ServiceAccount: `otel-collector-sa` in `monitoring`
- Full agent RBAC deferred to Phase 2+

---

## Phase 1 Completion Criteria

- [x] `kubectl get pods -A` shows backend, otel-collector, clickhouse all `Running`
- [x] `GET /health` returns 200
- [x] `GET /data` returns 200
- [x] `GET /slow` returns 200 after ~4 seconds
- [x] ClickHouse `otel.otel_logs` has rows with `ServiceName = 'backend'`
- [x] ClickHouse `otel.otel_traces` has rows from `backend` service
- [x] After `GET /slow`, `otel.otel_traces` has a row with `SpanAttributes['db.system'] = 'postgresql'`
- [x] OTel Collector logs show no export errors (`kubectl logs -n monitoring -l app=otel-collector`)
