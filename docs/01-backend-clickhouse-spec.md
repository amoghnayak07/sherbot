# Phase 1 Spec — Backend Service + OTel Collector + ClickHouse

## Dependencies

### `services/backend/requirements.txt`

```
fastapi==0.111.0
uvicorn[standard]==0.29.0
opentelemetry-sdk==1.24.0
opentelemetry-instrumentation-fastapi==0.45b0
opentelemetry-exporter-otlp-proto-grpc==1.24.0
```

## Environment Variables

### `services/backend/.env.example`

```
OTEL_SERVICE_NAME=backend
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector-svc.monitoring.svc.cluster.local:4317
PORT=8001
```

## Ports

| Service             | Port   | DNS (in-cluster)                                       |
| ------------------- | ------ | ------------------------------------------------------ |
| Backend             | `8001` | `backend-svc.demo.svc.cluster.local:8001`              |
| OTel Collector gRPC | `4317` | `otel-collector-svc.monitoring.svc.cluster.local:4317` |
| ClickHouse native   | `9000` | `clickhouse-svc.monitoring.svc.cluster.local:9000`     |
| ClickHouse HTTP     | `8123` | same host                                              |

## Manifest Checklist

```
k8s/
├── namespace.yaml                # demo + monitoring only
├── rbac.yaml                     # otel-collector-kubelet ClusterRole + otel-collector-sa
├── clickhouse.yaml               # StatefulSet + PVC (5Gi) + Service (no init SQL — schema is
│                                  # auto-created by the clickhouse exporter on first connection)
├── otel-collector-config.yaml    # ConfigMap — collector pipeline
└── otel-collector.yaml           # DaemonSet + Service

services/backend/k8s/
├── deployment.yaml
├── service.yaml
└── configmap.yaml
```

## `services/backend/main.py` — Key Structure

```python
# OTel setup — add before app = FastAPI()
# 1. Create Resource with service.name=backend, service.namespace=demo
# 2. Create TracerProvider with BatchSpanProcessor → OTLPSpanExporter
# 3. trace.set_tracer_provider(provider)
# 4. Set up JSON logging formatter to stdout
# 5. app = FastAPI()
# 6. FastAPIInstrumentor.instrument_app(app)  ← after app creation

@app.get("/health")
async def health() -> dict: ...
# returns {"status": "healthy", "service": "backend"}

@app.get("/data")
async def data() -> dict: ...
# returns {"data": "ok", "service": "backend"}

@app.get("/slow")
async def trigger_slow() -> dict: ...
# opens manual span "db-query" with:
#   db.system = "postgresql"
#   db.statement = "SELECT pg_sleep(4)"
# asyncio.sleep(4) inside span — no real DB connection needed
# logs warning: {"event":"slow_query","duration_ms":4000}
# returns {"status": "ok", "latency_ms": 4000}
```

## OTel Collector Config — Phase 1 Only

**Receivers:** `filelog` + `otlp` only. No kubeletstats, hostmetrics, k8sobjects.

**filelog operator chain:**

1. `regex_parser` — strips CRI prefix: `^(?P<time>[^ ]+) (?P<stream>stdout|stderr) (?P<logtag>[^ ]+) (?P<log>.*)$`
2. `json_parser` — parses `attributes.log` as JSON

**k8sattributes processor** extracts:

- `k8s.pod.name`
- `k8s.namespace.name`
- `k8s.deployment.name`
- `k8s.node.name`

**clickhouse exporter:**

- endpoint: `tcp://clickhouse-svc.monitoring:9000`
- database: `otel`
- `ttl: 24h` — applies to the exporter's auto-created tables (`otel_logs`, `otel_traces`, `otel_metrics`)
- `sending_queue` with `file_storage` — no `batch` processor before it
- schema is **not** hand-defined — the exporter creates `otel_logs`/`otel_traces`/`otel_metrics`
  itself on first connection, with a fixed column set (`Timestamp`, `TraceId`, `SpanId`,
  `ServiceName`, `Body`/`SpanName`, `LogAttributes`/`SpanAttributes`, `Duration` in nanoseconds for
  traces, plus `__otel_materialized_k8s.*` columns materialized from `ResourceAttributes`)

**Pipelines:**

- `logs`: filelog → k8sattributes → clickhouse
- `traces`: otlp → k8sattributes → clickhouse

## OTel Collector DaemonSet — Required Fields

```yaml
hostNetwork: true
dnsPolicy: ClusterFirstWithHostNet
env:
  - name: K8S_NODE_NAME
    valueFrom:
      fieldRef:
        fieldPath: spec.nodeName
volumes:
  - varlogpods  → hostPath: /var/log/pods
  - hostfs      → hostPath: /
  - queue       → hostPath: /var/lib/otelcol/queue (DirectoryOrCreate)
```

## RBAC — Phase 1 Only

One ClusterRole `otel-collector-kubelet` for `k8sattributes` processor:

```yaml
resources: [nodes/stats, nodes/metrics, nodes/proxy, pods, namespaces, nodes]
verbs: [get, list, watch]
apiGroups apps: [replicasets, deployments]
verbs: [get, list, watch]
```

## Backend Kubernetes Manifest Fields

**deployment.yaml key fields:**

- `imagePullPolicy: Never` — local k3d image
- `containerPort: 8001`
- Resource limits: `memory: 256Mi, cpu: 200m`
- Env from ConfigMap: `OTEL_SERVICE_NAME`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `PORT`

**service.yaml:** ClusterIP, port `8001`

## Dockerfile

```
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
```

## Makefile

```makefile
setup:
	k3d cluster create sherbot
	kubectl apply -f k8s/namespace.yaml
	kubectl apply -f k8s/rbac.yaml
	kubectl apply -f k8s/clickhouse.yaml
	kubectl apply -f k8s/otel-collector-config.yaml
	kubectl apply -f k8s/otel-collector.yaml

backend:
	docker build -t rca-backend:latest ./services/backend
	k3d image import rca-backend:latest -c sherbot
	kubectl apply -f services/backend/k8s/

teardown:
	k3d cluster delete sherbot
```

## Verification Queries (run in ClickHouse shell)

```sql
-- logs flowing
SELECT ServiceName, Body, Timestamp
FROM otel.otel_logs
WHERE `__otel_materialized_k8s.namespace.name` = 'demo'
ORDER BY Timestamp DESC LIMIT 10;

-- traces flowing
SELECT ServiceName, SpanName, Duration, Timestamp
FROM otel.otel_traces
ORDER BY Timestamp DESC LIMIT 10;

-- slow endpoint DB span
SELECT TraceId, SpanName, Duration,
       SpanAttributes['db.system'], SpanAttributes['db.statement']
FROM otel.otel_traces
WHERE SpanAttributes['db.system'] != ''
LIMIT 5;
```

## Gotchas

- `FastAPIInstrumentor.instrument_app(app)` must be called after `app = FastAPI()`
- Do NOT add `batch` processor before ClickHouse exporter — it handles its own batching
- OTel Collector must be `contrib` image — core image has no ClickHouse exporter
- `k8sattributes` needs its own RBAC — included in `otel-collector-kubelet` ClusterRole
- **ClickHouse exporter owns the schema.** Its `INSERT` statements are hardcoded against its own
  fixed columns (`Timestamp`, `TraceId`, `SpanId`, `ServiceName`, `Body`/`SpanName`, `LogAttributes`/
  `SpanAttributes`, `Duration` in nanoseconds, etc.) — do not hand-write `CREATE TABLE` schema for
  `otel_logs`/`otel_traces`/`otel_metrics` and expect the exporter to use it. Use `ttl` in the
  exporter config for retention instead of a manual `TTL` clause.
- **ClickHouse's own default-user setup on `clickhouse/clickhouse-server` locks the `default` user
  to `127.0.0.1`/`::1`** unless `CLICKHOUSE_SKIP_USER_SETUP=1` is set — without it, other pods
  (like the OTel Collector) get an opaque "Authentication failed" error, not a network error.
  `CLICKHOUSE_ALLOW_EMPTY_PASSWORD` is not recognized by this image version — don't rely on it.
- **The OTel Collector's `file_storage` extension needs a writable directory.** A `hostPath` with
  `DirectoryOrCreate` is created owned by `root`, but `otelcol-contrib` runs as a non-root user by
  default — set `securityContext.runAsUser: 0` on the collector container, or the exporter fails
  to start with a permission-denied error on its queue file.
- `hostNetwork: true` required on collector DaemonSet for kubelet API access
- Windows venv activation: `.venv\Scripts\Activate.ps1` not `source .venv/bin/activate`
- ClickHouse shell command: `kubectl exec -n monitoring -it statefulset/clickhouse -- clickhouse-client --database otel`
