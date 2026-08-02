"""MCP server entry point.

Exposes read-only query tools against Cloud SQL Postgres ``gong-nl-db`` to
Claude Desktop via stdio.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from typing import Optional

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as _e:
    _hint = (
        "\n\ngong-nl-db-mcp could not import FastMCP from the mcp package.\n"
        "This usually means the mcp SDK was upgraded past a breaking version.\n\n"
        "Fix (run in Terminal, then restart Claude Desktop):\n"
        "  uv cache clean gong-nl-db-mcp\n"
        "  curl -LsSf https://raw.githubusercontent.com/andyhorvitz/gong-nl-db-mcp/main/scripts/install.sh | bash\n\n"
        "Or contact andy.horvitz@bairesdev.com."
    )
    raise ImportError(str(_e) + _hint) from _e

from .db import Db, DbConfig, QueryResult
from .formatting import format_result
from .safety import UnsafeQueryError, inject_limit, validate

log = logging.getLogger(__name__)

# Per-query result caps (hard ceilings — the tool-level ``limit`` arg is
# clamped to these).
SAMPLE_ROWS_CAP = 50
RUN_QUERY_CAP = 1000


def build_server() -> FastMCP:
    """Construct the FastMCP server and register all tools.

    Database connection is created lazily on first tool call so the server
    can start (and surface config errors in its banner) even if auth isn't
    fully set up yet.
    """
    mcp = FastMCP("gong-nl-db")

    _db: list[Db] = []  # lazily-initialized singleton

    def db() -> Db:
        if not _db:
            _db.append(Db(DbConfig.from_env()))
        return _db[0]

    # ------------------------------------------------------------------ #
    # Metadata / discovery tools
    # ------------------------------------------------------------------ #

    @mcp.tool(
        description=(
            "List non-system schemas in the gong-nl-db database. "
            "Call this first when exploring."
        )
    )
    def list_schemas() -> str:
        sql = (
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name NOT IN ('pg_catalog', 'information_schema') "
            "AND schema_name NOT LIKE 'pg_%' "
            "ORDER BY schema_name"
        )
        return _execute(db(), sql, max_rows=200)

    @mcp.tool(
        description=(
            "List tables and views in a schema. Use list_schemas first to find "
            "valid schema names. Defaults to 'public'."
        )
    )
    def list_tables(schema: str = "public") -> str:
        sql = (
            "SELECT table_name, table_type FROM information_schema.tables "
            f"WHERE table_schema = {_lit(schema)} "
            "ORDER BY table_name"
        )
        return _execute(db(), sql, max_rows=500)

    @mcp.tool(
        description=(
            "Describe a table's columns, types, and nullability. "
            "Use this before writing a query against an unfamiliar table."
        )
    )
    def describe_table(table: str, schema: str = "public") -> str:
        sql = (
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            f"WHERE table_schema = {_lit(schema)} "
            f"  AND table_name = {_lit(table)} "
            "ORDER BY ordinal_position"
        )
        return _execute(db(), sql, max_rows=500)

    @mcp.tool(
        description=(
            "Return up to `limit` sample rows from a table (max 50). "
            "Useful for getting a feel for the data shape before running "
            "real analytical queries."
        )
    )
    def sample_rows(table: str, schema: str = "public", limit: int = 10) -> str:
        capped = max(1, min(limit, SAMPLE_ROWS_CAP))
        # Safe: schema/table are quoted identifiers; limit is integer-clamped.
        sql = f'SELECT * FROM {_ident(schema)}.{_ident(table)} LIMIT {capped}'
        return _execute(db(), sql, max_rows=capped)

    # ------------------------------------------------------------------ #
    # Query tools
    # ------------------------------------------------------------------ #

    @mcp.tool(
        description=(
            "Run a read-only SQL query against gong-nl-db. Only SELECT, WITH "
            "(terminating in SELECT), and set-operation queries are allowed — "
            "any INSERT/UPDATE/DELETE/DDL is rejected before the query reaches "
            "the database. Results are capped at `limit` rows (max 1000)."
        )
    )
    def run_query(sql: str, limit: int = 200) -> str:
        capped = max(1, min(limit, RUN_QUERY_CAP))
        try:
            validated = validate(sql)
        except UnsafeQueryError as e:
            return f"❌ Query rejected: {e}"

        final_sql = validated.sql
        if not validated.is_explain:
            final_sql = inject_limit(final_sql, capped)

        return _execute(db(), final_sql, max_rows=capped)

    # ------------------------------------------------------------------ #
    # Domain helper tools (structured shortcuts for common questions).
    # Prefer these over hand-writing SQL — they route through the same
    # read-only execution path but hit purpose-built indexes / views.
    # ------------------------------------------------------------------ #

    @mcp.tool(
        description=(
            "Full-text search across transcript_segments. Prefer this over "
            "ILIKE when searching for phrases in calls — it uses the GIN "
            "FTS index and is ~100x faster. Returns matching segments "
            "joined to call metadata. `query` supports websearch syntax: "
            "\"pricing objection\", pricing OR discount, -competitor. "
            "`since` / `until` are ISO-8601 dates or timestamps (optional). "
            "`host_email` filters to calls owned by one user (optional)."
        )
    )
    def search_transcripts(
        query: str,
        since: Optional[str] = None,
        until: Optional[str] = None,
        host_email: Optional[str] = None,
        limit: int = 20,
    ) -> str:
        capped = max(1, min(limit, 100))
        where = [
            "to_tsvector('english', coalesce(ts.text, '')) "
            f"@@ websearch_to_tsquery('english', {_lit(query)})"
        ]
        if since:
            where.append(f"c.started >= {_lit(since)}::timestamptz")
        if until:
            where.append(f"c.started <  {_lit(until)}::timestamptz")
        if host_email:
            where.append(f"u.email = {_lit(host_email)}")
        sql = f"""
            SELECT c.id            AS call_id,
                   c.title,
                   c.started,
                   u.email         AS host_email,
                   c.company_name,
                   ts.speaker_id,
                   ts.start_time,
                   ts.end_time,
                   left(ts.text, 300) AS snippet
            FROM transcript_segments ts
            JOIN calls c ON c.id = ts.call_id
            LEFT JOIN users u ON u.id = c.primary_user_id
            WHERE {' AND '.join(where)}
            ORDER BY c.started DESC
            LIMIT {capped}
        """
        return _execute(db(), sql, max_rows=capped)

    @mcp.tool(
        description=(
            "Per-user daily call activity from the mv_user_daily materialized "
            "view — answers questions like 'how many calls did X have this "
            "week', 'avg talk ratio by person last month'. Filter by "
            "`host_email` (single user) or leave blank for team view. "
            "`since`/`until` are ISO-8601 dates (inclusive / exclusive). "
            "Returns one row per (host, date)."
        )
    )
    def user_activity(
        host_email: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 200,
    ) -> str:
        capped = max(1, min(limit, 1000))
        where: list[str] = []
        if host_email:
            where.append(f"host_email = {_lit(host_email)}")
        if since:
            where.append(f"started_date >= {_lit(since)}::date")
        if until:
            where.append(f"started_date <  {_lit(until)}::date")
        where_clause = ("WHERE " + " AND ".join(where)) if where else ""
        sql = f"""
            SELECT host_email,
                   started_date,
                   calls,
                   total_sec,
                   round(avg_talk_ratio::numeric, 3) AS avg_talk_ratio,
                   questions_asked
            FROM mv_user_daily
            {where_clause}
            ORDER BY started_date DESC, calls DESC
            LIMIT {capped}
        """
        return _execute(db(), sql, max_rows=capped)

    @mcp.tool(
        description=(
            "Semantic / meaning-based search across call transcript chunks using "
            "Vertex AI embeddings (text-embedding-005) and cosine similarity. "
            "Use this when the user asks to find calls 'about' a topic or concept "
            "— e.g. 'calls where pricing came up', 'conversations about churn "
            "risk', 'mentions of competitor X'. Unlike search_transcripts (FTS "
            "keyword matching), this finds conceptually related content even "
            "without exact word matches. "
            "`since`/`until` are ISO-8601 dates (optional). "
            "`host_email` filters to one rep's calls (optional). "
            "`limit` caps results 1–20 (default 10)."
        )
    )
    def semantic_search(
        query: str,
        since: Optional[str] = None,
        until: Optional[str] = None,
        host_email: Optional[str] = None,
        limit: int = 10,
    ) -> str:
        capped = max(1, min(limit, 20))

        # Derive GCP project from the instance connection name (project:region:instance).
        icn = os.environ.get("INSTANCE_CONNECTION_NAME", "planar-ray-494004-b8:us-central1:gong-nl-db")
        project = icn.split(":")[0]

        try:
            vec = _embed_query(query, project)
        except Exception as e:
            return f"❌ Could not embed query: {type(e).__name__}: {e}"

        # Format as a Postgres vector literal (512 floats).
        vec_literal = "[" + ",".join(f"{v:.6f}" for v in vec) + "]"

        where: list[str] = []
        if since:
            where.append(f"c.started >= {_lit(since)}::timestamptz")
        if until:
            where.append(f"c.started <  {_lit(until)}::timestamptz")
        if host_email:
            where.append(f"u.email = {_lit(host_email)}")
        where_clause = ("WHERE " + " AND ".join(where)) if where else ""

        # Uses the HNSW index (idx_tc_embedding_hnsw, vector_cosine_ops).
        # We call run_readonly directly — sqlglot does not understand the pgvector
        # <=> operator and would reject this query. The SQL is internally generated;
        # all user-supplied values are either transformed to a float vector (query
        # text) or escaped via _lit() (since/until/host_email).
        sql = f"""
            SELECT c.id            AS call_id,
                   c.title,
                   c.started,
                   u.email         AS host_email,
                   c.company_name,
                   round((1 - (tc.embedding <=> '{vec_literal}'::vector))::numeric, 4) AS similarity,
                   tc.start_time   AS chunk_start_sec,
                   left(tc.text, 400) AS snippet
            FROM transcript_chunks tc
            JOIN calls c  ON c.id = tc.call_id
            LEFT JOIN users u ON u.id = c.primary_user_id
            {where_clause}
            ORDER BY tc.embedding <=> '{vec_literal}'::vector
            LIMIT {capped}
        """
        try:
            result = db().run_readonly(sql, max_rows=capped)
        except Exception as e:
            log.exception("semantic_search query failed")
            return f"❌ Database error: {type(e).__name__}: {e}"
        return format_result(result)

    @mcp.tool(
        description=(
            "Return the Postgres query plan for a SELECT statement. "
            "Useful for debugging slow queries."
        )
    )
    def explain_query(sql: str) -> str:
        try:
            validated = validate(sql)
        except UnsafeQueryError as e:
            return f"❌ Query rejected: {e}"
        # If the caller didn't wrap in EXPLAIN themselves, do it for them.
        final_sql = (
            validated.sql if validated.is_explain else f"EXPLAIN {validated.sql}"
        )
        return _execute(db(), final_sql, max_rows=1000)

    return mcp


# ---------------------------------------------------------------------- #
# Internal helpers
# ---------------------------------------------------------------------- #


def _embed_query(text: str, project: str, region: str = "us-central1") -> list[float]:
    """Embed *text* with Vertex AI text-embedding-005 (512 dims) via REST.

    Uses gcloud ADC token — no extra Python deps beyond the standard library.
    """
    import subprocess
    from shutil import which

    gcloud = which("gcloud") or "gcloud"
    out = subprocess.run(
        [gcloud, "auth", "application-default", "print-access-token"],
        capture_output=True, text=True, timeout=15,
    )
    if out.returncode != 0:
        raise RuntimeError(
            f"Could not get ADC token: {out.stderr.strip()}. "
            "Run: gcloud auth application-default login"
        )
    token = out.stdout.strip()

    url = (
        f"https://{region}-aiplatform.googleapis.com/v1/projects/{project}"
        f"/locations/{region}/publishers/google/models/text-embedding-005:predict"
    )
    payload = json.dumps({
        "instances": [{"content": text, "task_type": "RETRIEVAL_QUERY"}],
        "parameters": {"outputDimensionality": 512},
    }).encode()

    req = urllib.request.Request(url, data=payload, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())

    return result["predictions"][0]["embeddings"]["values"]


def _execute(db_: Db, sql: str, max_rows: int) -> str:
    """Run an already-safe SQL string and format the result."""
    try:
        result: QueryResult = db_.run_readonly(sql, max_rows=max_rows)
    except Exception as e:  # pragma: no cover — surface DB errors to Claude
        log.exception("query failed")
        return f"❌ Database error: {type(e).__name__}: {e}"
    return format_result(result)


def _lit(s: str) -> str:
    """Single-quote a string literal, escaping embedded quotes. Used for
    parameters we interpolate into information_schema lookups (not user SQL)."""
    return "'" + s.replace("'", "''") + "'"


def _ident(name: str) -> str:
    """Quote a Postgres identifier. Rejects anything that isn't a safe name.

    Applied to ``schema`` / ``table`` tool arguments in :func:`sample_rows` so
    a caller can't break out of the identifier via a crafted name.
    """
    if not name or not all(ch.isalnum() or ch == "_" for ch in name):
        raise ValueError(f"invalid identifier: {name!r}")
    return '"' + name.replace('"', '""') + '"'


# ---------------------------------------------------------------------- #
# Entry point (console_script `gong-nl-db-mcp`)
# ---------------------------------------------------------------------- #


def _patch_ssl_with_certifi() -> None:
    """Anchor all SSL certificate verification to certifi's CA bundle.

    Two-layer approach:
    1. Env vars — respected by requests, httpx, and most aiohttp configurations.
    2. ssl.create_default_context patch — catches libraries that call it
       directly without checking env vars (e.g. aiohttp on Python 3.13+
       where the macOS system-keychain path changed).

    Uses setdefault / only patches when no CA is already specified so an
    admin-configured cert file always takes precedence.
    """
    import certifi
    import ssl

    bundle = certifi.where()
    os.environ.setdefault("SSL_CERT_FILE", bundle)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", bundle)

    _orig = ssl.create_default_context

    def _patched_ctx(*args, cafile=None, capath=None, cadata=None, **kwargs):
        if cafile is None and capath is None and cadata is None:
            cafile = bundle
        return _orig(*args, cafile=cafile, capath=capath, cadata=cadata, **kwargs)

    ssl.create_default_context = _patched_ctx  # type: ignore[assignment]


def main() -> None:
    if "--version" in sys.argv:
        from importlib.metadata import version
        print(version("gong-nl-db-mcp"))
        return

    # Must run before any network I/O — cloud-sql-python-connector's aiohttp
    # calls sqladmin.googleapis.com at connector init time, and the isolated
    # uvx Python environment does not reliably find the system CA bundle on
    # macOS (affects Python 3.12+ / 3.13+ depending on the build).
    _patch_ssl_with_certifi()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,  # stdout is the MCP transport — keep it clean
    )

    from importlib.metadata import version as _v, PackageNotFoundError
    try:
        _ver = _v("gong-nl-db-mcp")
    except PackageNotFoundError:
        _ver = "unknown"
    log.info("gong-nl-db-mcp %s starting (Python %s)", _ver, sys.version.split()[0])

    build_server().run()


if __name__ == "__main__":
    main()
