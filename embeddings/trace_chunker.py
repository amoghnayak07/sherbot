from datetime import datetime

CHUNK_TOKENS = 512


def chunk_traces(rows: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["TraceId"], []).append(row)

    chunks = []
    for trace_id, spans in groups.items():
        spans.sort(key=lambda r: r["Timestamp"])
        lines = [_span_line(span) for span in spans]

        content = "\n".join(lines)
        tokens = content.split()
        if len(tokens) > CHUNK_TOKENS:
            content = " ".join(tokens[:CHUNK_TOKENS])

        root = spans[0]
        chunks.append(
            {
                "content": content,
                "metadata": {
                    "timestamp": _iso(root["Timestamp"]),
                    "pod_name": None,
                    "namespace": None,
                    "trace_id": trace_id,
                    "span_id": root.get("SpanId"),
                    "severity_number": None,
                    "chunk_sequence": "1/1",
                },
            }
        )
    return chunks


def _span_line(span: dict) -> str:
    duration_ms = span["Duration"] / 1_000_000
    line = f"[{span['ServiceName']}] {span['SpanName']} duration={duration_ms:.2f}ms"

    attrs = span.get("SpanAttributes") or {}
    if "db.system" in attrs:
        line += f" db.system={attrs['db.system']}"
    if "db.statement" in attrs:
        line += f" db.statement={attrs['db.statement']}"

    return line


def _iso(timestamp) -> str:
    if isinstance(timestamp, datetime):
        return timestamp.isoformat()
    return str(timestamp)
