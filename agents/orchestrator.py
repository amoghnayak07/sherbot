import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import clickhouse_connect
import httpx
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from tenacity import retry, stop_after_attempt, wait_exponential

import config

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "embeddings"))
from chunker import chunk  # noqa: E402


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "component": "orchestrator",
            "message": record.getMessage(),
        }
        payload.update(getattr(record, "extra_fields", {}))
        return json.dumps(payload)


handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JSONFormatter())
log = logging.getLogger("orchestrator")
log.setLevel(logging.INFO)
log.addHandler(handler)
log.propagate = False

engine = create_async_engine(config.RCA_DATABASE_URL)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

app = FastAPI()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
async def run_query(query: str, params: dict) -> list:
    async with AsyncSessionLocal() as session:
        result = await session.execute(text(query), params)
        await session.commit()
        if result.returns_rows:
            return result.mappings().all()
        return []


async def check_in_flight(alert: dict) -> bool:
    labels = alert["labels"]
    rows = await run_query(
        """
        SELECT status FROM incidents
        WHERE alert_name = :alert_name AND pod = :pod AND namespace = :namespace
        AND status = 'in_flight' AND fixed = FALSE
        """,
        {
            "alert_name": labels["alertname"],
            "pod": labels["pod"],
            "namespace": labels["namespace"],
        },
    )
    return len(rows) > 0


async def insert_incident(incident_id: UUID, alert: dict) -> None:
    labels = alert["labels"]
    await run_query(
        """
        INSERT INTO incidents (incident_id, alert_name, pod, namespace, scenario, status)
        VALUES (:incident_id, :alert_name, :pod, :namespace, :scenario, 'in_flight')
        """,
        {
            "incident_id": str(incident_id),
            "alert_name": labels["alertname"],
            "pod": labels["pod"],
            "namespace": labels["namespace"],
            "scenario": labels.get("scenario"),
        },
    )
    log.info(
        "Incident created",
        extra={
            "extra_fields": {
                "incident_id": str(incident_id),
                "alert_name": labels["alertname"],
                "pod": labels["pod"],
            }
        },
    )


async def mark_incident_resolved(alert: dict) -> None:
    labels = alert["labels"]
    await run_query(
        """
        UPDATE incidents SET fixed = TRUE
        WHERE alert_name = :alert_name AND pod = :pod AND namespace = :namespace
        AND status != 'in_flight'
        """,
        {
            "alert_name": labels["alertname"],
            "pod": labels["pod"],
            "namespace": labels["namespace"],
        },
    )
    log.info(
        "Incident resolved",
        extra={
            "extra_fields": {
                "alert_name": labels["alertname"],
                "pod": labels["pod"],
            }
        },
    )


async def query_clickhouse_logs(pod: str, namespace: str, window_start: datetime, window_end: datetime) -> list[dict]:
    client = await clickhouse_connect.get_async_client(
        host=config.CLICKHOUSE_HOST, port=config.CLICKHOUSE_PORT, database=config.CLICKHOUSE_DATABASE
    )
    result = await client.query(
        """
        SELECT Timestamp, Body, TraceId, SpanId, ServiceName, SeverityNumber, LogAttributes, ResourceAttributes
        FROM otel_logs
        WHERE ResourceAttributes['k8s.pod.name'] = {pod:String}
        AND ResourceAttributes['k8s.namespace.name'] = {namespace:String}
        AND Timestamp >= {window_start:DateTime64} AND Timestamp <= {window_end:DateTime64}
        """,
        parameters={"pod": pod, "namespace": namespace, "window_start": window_start, "window_end": window_end},
    )
    return [dict(zip(result.column_names, row)) for row in result.result_rows]


async def query_clickhouse_traces(service: str, window_start: datetime, window_end: datetime) -> list[dict]:
    client = await clickhouse_connect.get_async_client(
        host=config.CLICKHOUSE_HOST, port=config.CLICKHOUSE_PORT, database=config.CLICKHOUSE_DATABASE
    )
    result = await client.query(
        """
        SELECT Timestamp, TraceId, SpanId, ParentSpanId, ServiceName, SpanName, Duration, SpanAttributes
        FROM otel_traces
        WHERE ServiceName = {service:String}
        AND Timestamp >= {window_start:DateTime64} AND Timestamp <= {window_end:DateTime64}
        """,
        parameters={"service": service, "window_start": window_start, "window_end": window_end},
    )
    return [dict(zip(result.column_names, row)) for row in result.result_rows]


async def embed_chunks(chunks: list[dict], source: str) -> list[list[float]]:
    if not chunks:
        return []
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{config.EMBEDDINGS_URL}/embed",
            json={"texts": [c["content"] for c in chunks], "source": source},
        )
        response.raise_for_status()
        return response.json()["embeddings"]


async def insert_embeddings(incident_id: UUID, source: str, chunks: list[dict], vectors: list[list[float]]) -> None:
    for chunk_data, vector in zip(chunks, vectors):
        await run_query(
            """
            INSERT INTO embeddings (embedding_id, incident_id, source, content, metadata, embedding)
            VALUES (:embedding_id, :incident_id, :source, :content, CAST(:metadata AS JSONB), CAST(:embedding AS vector))
            """,
            {
                "embedding_id": str(uuid4()),
                "incident_id": str(incident_id),
                "source": source,
                "content": chunk_data["content"],
                "metadata": json.dumps(chunk_data["metadata"]),
                "embedding": "[" + ",".join(str(v) for v in vector) + "]",
            },
        )


async def chunk_and_embed(incident_id: UUID, alert: dict) -> None:
    labels = alert["labels"]
    pod = labels["pod"]
    namespace = labels["namespace"]
    service = labels.get("service", pod)

    alert_time = datetime.fromisoformat(alert["startsAt"].replace("Z", "+00:00"))
    window_start = alert_time - timedelta(minutes=config.ALERT_WINDOW_MINUTES)

    log_rows = await query_clickhouse_logs(pod, namespace, window_start, alert_time)
    trace_rows = await query_clickhouse_traces(service, window_start, alert_time)

    log_chunks = chunk(log_rows, "logs")
    trace_chunks = chunk(trace_rows, "traces")

    log_vectors = await embed_chunks(log_chunks, "logs")
    trace_vectors = await embed_chunks(trace_chunks, "traces")

    await insert_embeddings(incident_id, "logs", log_chunks, log_vectors)
    await insert_embeddings(incident_id, "traces", trace_chunks, trace_vectors)

    log.info(
        "Pre-processing complete, ready for agents",
        extra={
            "extra_fields": {
                "incident_id": str(incident_id),
                "log_chunks": len(log_chunks),
                "trace_chunks": len(trace_chunks),
            }
        },
    )


@app.post("/webhook")
async def handle_webhook(payload: dict) -> dict:
    for alert in payload["alerts"]:
        try:
            if alert["status"] == "resolved":
                await mark_incident_resolved(alert)
                continue

            incident_id = uuid4()
            if await check_in_flight(alert):
                log.info(
                    "Duplicate rejected",
                    extra={"extra_fields": {"alert_name": alert["labels"]["alertname"]}},
                )
                continue

            await insert_incident(incident_id, alert)
            await chunk_and_embed(incident_id, alert)
        except Exception as exc:
            log.error(
                "Failed to process alert",
                extra={"extra_fields": {"error": str(exc)}},
            )

    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("orchestrator:app", host="0.0.0.0", port=config.PORT, reload=True)
