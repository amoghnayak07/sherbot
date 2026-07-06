import os

RCA_DATABASE_URL = os.getenv(
    "RCA_DATABASE_URL",
    "postgresql+asyncpg://rca_agent:changeme@localhost:5433/rca",
)
CLICKHOUSE_HOST = os.getenv("CLICKHOUSE_HOST", "localhost")
CLICKHOUSE_PORT = int(os.getenv("CLICKHOUSE_PORT", "8123"))
CLICKHOUSE_DATABASE = os.getenv("CLICKHOUSE_DATABASE", "otel")
EMBEDDINGS_URL = os.getenv("EMBEDDINGS_URL", "http://localhost:8090")
PORT = int(os.getenv("PORT", "8080"))
ALERT_WINDOW_MINUTES = int(os.getenv("ALERT_WINDOW_MINUTES", "10"))
