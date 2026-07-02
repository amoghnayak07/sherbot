import asyncio
import json
import logging
import os
import sys
import time

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

OTEL_SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "backend")
OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv(
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "http://otel-collector-svc.monitoring.svc.cluster.local:4317",
)
PORT = int(os.getenv("PORT", "8001"))

resource = Resource.create(
    {"service.name": OTEL_SERVICE_NAME, "service.namespace": "demo"}
)
provider = TracerProvider(resource=resource)
provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=OTEL_EXPORTER_OTLP_ENDPOINT, insecure=True))
)
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(OTEL_SERVICE_NAME)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "service": OTEL_SERVICE_NAME,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in logging.LogRecord.__dict__ and key not in payload:
                payload[key] = value
        return json.dumps(payload)


handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
log = logging.getLogger(OTEL_SERVICE_NAME)
log.setLevel(logging.INFO)
log.addHandler(handler)
log.propagate = False

app = FastAPI()
FastAPIInstrumentor.instrument_app(app)


@app.get("/health")
async def health() -> dict:
    return {"status": "healthy", "service": "backend"}


@app.get("/data")
async def data() -> dict:
    return {"data": "ok", "service": "backend"}


@app.get("/slow")
async def trigger_slow() -> dict:
    with tracer.start_as_current_span("db-query") as span:
        span.set_attribute("db.system", "postgresql")
        span.set_attribute("db.statement", "SELECT pg_sleep(4)")
        await asyncio.sleep(4)
        span_context = span.get_span_context()
        log.warning(
            "Slow query detected",
            extra={
                "event": "slow_query",
                "duration_ms": 4000,
                "trace_id": format(span_context.trace_id, "032x"),
                "span_id": format(span_context.span_id, "016x"),
            },
        )
    return {"status": "ok", "latency_ms": 4000}
