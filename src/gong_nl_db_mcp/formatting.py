"""Format query results into text suitable for MCP tool return values.

Claude Desktop renders MCP tool results as text. Markdown tables are readable
for small results; CSV is compact for large ones. This module picks the format
and enforces a byte cap so a huge SELECT doesn't blow up the context window.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from .db import QueryResult

# Soft cap on response size. Well under Claude Desktop's context budget but
# large enough for typical analytical queries.
DEFAULT_MAX_BYTES = 5 * 1024 * 1024


def format_result(result: QueryResult, max_bytes: int = DEFAULT_MAX_BYTES) -> str:
    """Render ``result`` as markdown; fall back to CSV if markdown is too big.

    Always includes a row-count line and a truncation notice when applicable.
    """
    if not result.columns:
        return "_(query returned no columns — usually an EXPLAIN or empty result)_"
    if result.row_count == 0:
        return f"| {' | '.join(result.columns)} |\n_(0 rows)_"

    md = _as_markdown(result)
    if len(md.encode("utf-8")) <= max_bytes:
        return _with_footer(md, result)

    # Too big for markdown — fall back to CSV and truncate by bytes.
    csv_text = _as_csv(result)
    encoded = csv_text.encode("utf-8")
    if len(encoded) > max_bytes:
        truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
        # Drop a trailing partial line.
        truncated = truncated.rsplit("\n", 1)[0]
        csv_text = (
            truncated
            + f"\n\n_(output truncated at {max_bytes:,} bytes; "
            f"refine your query or add a LIMIT)_"
        )
    return _with_footer(csv_text, result)


def _as_markdown(result: QueryResult) -> str:
    header = "| " + " | ".join(result.columns) + " |"
    sep = "| " + " | ".join("---" for _ in result.columns) + " |"
    lines = [header, sep]
    for row in result.rows:
        lines.append("| " + " | ".join(_cell(v) for v in row) + " |")
    return "\n".join(lines)


def _as_csv(result: QueryResult) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(result.columns)
    for row in result.rows:
        w.writerow(["" if v is None else str(v) for v in row])
    return buf.getvalue()


def _cell(v: Any) -> str:
    if v is None:
        return "_null_"
    s = str(v)
    # Escape pipes so they don't break the markdown table.
    s = s.replace("|", "\\|").replace("\n", " ")
    if len(s) > 200:
        s = s[:197] + "…"
    return s


def _with_footer(body: str, result: QueryResult) -> str:
    footer = f"\n\n_{result.row_count} row(s)"
    if result.truncated:
        footer += " (truncated — more rows matched; add a tighter LIMIT or WHERE)"
    footer += "._"
    return body + footer
