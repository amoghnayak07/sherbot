import json
import logging
import sys
from uuid import UUID, uuid4

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from tenacity import retry, stop_after_attempt, wait_exponential

import config


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
        except Exception as exc:
            log.error(
                "Failed to process alert",
                extra={"extra_fields": {"error": str(exc)}},
            )

    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("orchestrator:app", host="0.0.0.0", port=config.PORT, reload=True)
