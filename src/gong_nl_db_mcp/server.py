"""MCP server entry point.

Exposes read-only query tools against Cloud SQL Postgres ``gong-nl-db`` to
Claude Desktop via stdio.
"""

from __future__ import annotations

import logging
import os
import sys

from mcp.server.fastmcp import FastMCP

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


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,  # stdout is the MCP transport — keep it clean
    )
    build_server().run()


if __name__ == "__main__":
    main()
