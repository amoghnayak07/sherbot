from log_chunker import chunk_logs
from trace_chunker import chunk_traces


def chunk(rows: list[dict], source: str) -> list[dict]:
    if source == "logs":
        return chunk_logs(rows)
    if source == "traces":
        return chunk_traces(rows)
    raise ValueError(f"Unknown source: {source}")
