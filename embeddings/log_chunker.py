import re
from datetime import datetime

STACK_LINE_RE = re.compile(r"^\s|Traceback|File |Exception|Error")
CHUNK_TOKENS = 512
OVERLAP_RATIO = 0.2


def chunk_logs(rows: list[dict]) -> list[dict]:
    traced_rows = [row for row in rows if row.get("TraceId")]
    untraced_rows = [row for row in rows if not row.get("TraceId")]

    chunks = _chunk_by_trace(traced_rows)
    chunks.extend(_chunk_fixed_window(untraced_rows))
    return chunks


def _chunk_by_trace(rows: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        key = (row["TraceId"], row.get("SpanId"))
        groups.setdefault(key, []).append(row)

    chunks = []
    for (trace_id, span_id), group_rows in groups.items():
        group_rows.sort(key=lambda r: r["Timestamp"])
        content = "\n".join(row["Body"] for row in group_rows)
        first = group_rows[0]
        chunks.append(
            {
                "content": content,
                "metadata": {
                    "timestamp": _iso(first["Timestamp"]),
                    "pod_name": _pod_name(first),
                    "namespace": _namespace(first),
                    "trace_id": trace_id,
                    "span_id": span_id,
                    "severity_number": first.get("SeverityNumber"),
                    "chunk_sequence": "1/1",
                },
            }
        )
    return chunks


def _chunk_fixed_window(rows: list[dict]) -> list[dict]:
    rows = sorted(rows, key=lambda r: r["Timestamp"])
    if not rows:
        return []

    windows = []
    i = 0
    n = len(rows)
    while i < n:
        window_end = i
        token_count = 0
        in_stack_trace = False
        while window_end < n:
            line = rows[window_end]["Body"]
            is_stack_line = bool(STACK_LINE_RE.match(line))
            line_tokens = len(line.split())
            if (
                token_count + line_tokens > CHUNK_TOKENS
                and window_end > i
                and not in_stack_trace
            ):
                break
            token_count += line_tokens
            in_stack_trace = is_stack_line
            window_end += 1
        windows.append((i, window_end))
        if window_end >= n:
            break
        i += max(1, int((window_end - i) * (1 - OVERLAP_RATIO)))

    total = len(windows)
    chunks = []
    for seq, (start, end) in enumerate(windows, start=1):
        window_rows = rows[start:end]
        first = window_rows[0]
        chunks.append(
            {
                "content": "\n".join(row["Body"] for row in window_rows),
                "metadata": {
                    "timestamp": _iso(first["Timestamp"]),
                    "pod_name": _pod_name(first),
                    "namespace": _namespace(first),
                    "trace_id": None,
                    "span_id": None,
                    "severity_number": first.get("SeverityNumber"),
                    "chunk_sequence": f"{seq}/{total}",
                },
            }
        )
    return chunks


def _iso(timestamp) -> str:
    if isinstance(timestamp, datetime):
        return timestamp.isoformat()
    return str(timestamp)


def _pod_name(row: dict) -> str | None:
    attrs = row.get("ResourceAttributes") or {}
    return attrs.get("k8s.pod.name")


def _namespace(row: dict) -> str | None:
    attrs = row.get("ResourceAttributes") or {}
    return attrs.get("k8s.namespace.name")
