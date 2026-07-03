import os

RCA_DATABASE_URL = os.getenv(
    "RCA_DATABASE_URL",
    "postgresql+asyncpg://rca_agent:changeme@localhost:5433/rca",
)
PORT = int(os.getenv("PORT", "8080"))
