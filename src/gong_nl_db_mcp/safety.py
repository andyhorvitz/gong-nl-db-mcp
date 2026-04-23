"""Read-only SQL statement validator.

This is the most security-sensitive module in the project. It is one of four
defense-in-depth layers that enforce read-only access:

  1. GCP IAM role (``roles/cloudsql.client`` + ``roles/cloudsql.instanceUser``).
  2. Postgres DB role ``readonly_analysts`` with only ``SELECT`` grants.
  3. Per-session ``SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`` and
     per-query ``BEGIN READ ONLY ... ROLLBACK``.
  4. This parser-level allow-list.

The parser is intentionally strict: only a single statement whose root node is
``SELECT``, ``WITH`` (terminating in a SELECT), or ``EXPLAIN`` of an allowed
statement is accepted. Everything else is rejected.

CHANGES TO THIS FILE MUST GO THROUGH PR REVIEW. The git history of this file is
the audit trail for the read-only guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

# Expressions we accept as the top-level of a user query.
_ALLOWED_ROOTS: tuple[type[exp.Expression], ...] = (
    exp.Select,
    exp.Union,
    exp.Intersect,
    exp.Except,
    exp.With,
)

# Expressions that, if present anywhere in the AST, indicate a write or
# side-effecting operation. If any appears, the query is rejected.
_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Alter,
    exp.Create,
    exp.Drop,
    exp.TruncateTable,
    exp.Copy,
    exp.Command,  # sqlglot's catch-all for unrecognized statements (DO, CALL, etc.)
    exp.Into,  # SELECT ... INTO new_table creates a table
    exp.Lock,  # SELECT ... FOR UPDATE / FOR SHARE
)

# Upper-cased keywords we reject even if sqlglot didn't model them.
# This is a belt-and-suspenders check on the raw SQL text (post-comment-stripping).
_FORBIDDEN_KEYWORDS: frozenset[str] = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "UPSERT",
        "REPLACE",
        "TRUNCATE",
        "DROP",
        "CREATE",
        "ALTER",
        "GRANT",
        "REVOKE",
        "COPY",
        "CALL",
        "DO",
        "VACUUM",
        "ANALYZE",
        "CLUSTER",
        "REINDEX",
        "LOCK",
        "NOTIFY",
        "LISTEN",
        "UNLISTEN",
        "SET",  # we manage session state ourselves
        "RESET",
        "BEGIN",  # we wrap every query in our own READ ONLY txn
        "COMMIT",
        "ROLLBACK",
        "SAVEPOINT",
        "RELEASE",
        "PREPARE",
        "EXECUTE",
        "DEALLOCATE",
        "DECLARE",
        "FETCH",
        "CLOSE",
        "REFRESH",
    }
)


class UnsafeQueryError(ValueError):
    """Raised when a submitted query fails the read-only allow-list."""


@dataclass(frozen=True)
class ValidatedQuery:
    """A query that has passed all safety checks."""

    sql: str
    """The normalized SQL, guaranteed to be a single read-only statement."""

    is_explain: bool
    """True if the query is an EXPLAIN (no LIMIT injection needed)."""


def validate(sql: str) -> ValidatedQuery:
    """Validate ``sql`` as a single read-only statement.

    Returns a :class:`ValidatedQuery` on success; raises
    :class:`UnsafeQueryError` on any violation.

    Rejects:
      * Empty / whitespace-only input.
      * Anything that fails to parse as Postgres SQL.
      * Multi-statement input (even if each statement is a SELECT).
      * Any statement whose root isn't SELECT / WITH / set-op / allowed EXPLAIN.
      * Any AST containing forbidden nodes (INSERT/UPDATE/DELETE/DDL/COPY/…).
      * Any raw-text keyword in ``_FORBIDDEN_KEYWORDS`` (catches sqlglot gaps).
      * ``SELECT ... INTO`` and ``SELECT ... FOR UPDATE/SHARE``.
    """
    if not sql or not sql.strip():
        raise UnsafeQueryError("empty query")

    # Parse. sqlglot.parse returns a list of top-level statements.
    try:
        statements = sqlglot.parse(sql, dialect="postgres")
    except sqlglot.errors.ParseError as e:
        raise UnsafeQueryError(f"could not parse SQL: {e}") from e

    # Drop trailing None entries sqlglot can emit for trailing semicolons.
    statements = [s for s in statements if s is not None]

    if len(statements) == 0:
        raise UnsafeQueryError("no parseable statement")
    if len(statements) > 1:
        raise UnsafeQueryError(
            f"multiple statements not allowed (found {len(statements)})"
        )

    root = statements[0]

    # Unwrap EXPLAIN; validate its inner expression with the same rules.
    is_explain = False
    if _is_explain(root):
        is_explain = True
        inner = _explain_inner(root)
        if inner is None:
            raise UnsafeQueryError("EXPLAIN must wrap a SELECT/WITH statement")
        root = inner

    if not isinstance(root, _ALLOWED_ROOTS):
        raise UnsafeQueryError(
            f"only SELECT / WITH / EXPLAIN SELECT are allowed "
            f"(got {type(root).__name__})"
        )

    # Walk the AST; reject on any forbidden node.
    for node in root.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise UnsafeQueryError(
                f"forbidden SQL construct: {type(node).__name__}"
            )

    # Belt-and-suspenders: scan the SQL (with comments AND string-literal bodies
    # stripped) for dangerous keywords that sqlglot might not have modeled as
    # distinct nodes. Stripping string bodies prevents false positives on e.g.
    # ``WHERE name = 'DROP TABLE foo'``.
    stripped = _strip_comments_and_strings(sql).upper()
    tokens = _tokenize(stripped)
    for kw in _FORBIDDEN_KEYWORDS:
        if kw in tokens:
            raise UnsafeQueryError(f"forbidden keyword in SQL: {kw}")

    # Return the re-serialized SQL so we control exactly what hits the DB.
    # For EXPLAIN, re-wrap.
    normalized = root.sql(dialect="postgres")
    if is_explain:
        normalized = f"EXPLAIN {normalized}"

    return ValidatedQuery(sql=normalized, is_explain=is_explain)


def inject_limit(sql: str, limit: int) -> str:
    """Ensure ``sql`` has a top-level LIMIT <= ``limit``.

    If the query already has a LIMIT, the smaller of (existing, ``limit``) is
    kept. Set-operation queries (UNION/INTERSECT/EXCEPT) and WITH queries get
    the LIMIT applied to the whole result. EXPLAIN queries are returned
    unchanged — adding LIMIT would change the explained plan semantics.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")

    parsed = sqlglot.parse_one(sql, dialect="postgres")
    if _is_explain(parsed):
        return sql

    existing = parsed.args.get("limit")
    if existing is not None:
        # Respect a smaller existing limit; otherwise clamp down.
        try:
            existing_n = int(existing.expression.this)
            if existing_n <= limit:
                return sql
        except (AttributeError, ValueError, TypeError):
            # Non-literal LIMIT (e.g. expression/param) — clamp defensively.
            pass

    limited = parsed.limit(limit)
    return limited.sql(dialect="postgres")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _is_explain(node: exp.Expression) -> bool:
    """Return True if ``node`` is an EXPLAIN wrapper."""
    # sqlglot models EXPLAIN as a Command with name 'EXPLAIN' on Postgres.
    if isinstance(node, exp.Command) and (node.name or "").upper() == "EXPLAIN":
        return True
    # Some sqlglot versions expose an exp.Explain class.
    if hasattr(exp, "Explain") and isinstance(node, getattr(exp, "Explain")):
        return True
    return False


def _explain_inner(node: exp.Expression) -> exp.Expression | None:
    """Extract the inner statement from an EXPLAIN node."""
    if hasattr(exp, "Explain") and isinstance(node, getattr(exp, "Explain")):
        inner = node.this
        if isinstance(inner, exp.Expression):
            return inner
    if isinstance(node, exp.Command):
        # sqlglot parks the rest of the EXPLAIN statement in ``.expression`` as
        # a string Literal (``Literal(is_string=True, this='SELECT ...')``).
        # Extract the string and re-parse it.
        expr = node.expression
        inner_sql: str | None = None
        if isinstance(expr, exp.Literal):
            inner_sql = expr.this
        elif isinstance(expr, str):
            inner_sql = expr
        if inner_sql and inner_sql.strip():
            try:
                return sqlglot.parse_one(inner_sql, dialect="postgres")
            except sqlglot.errors.ParseError:
                return None
    return None


def _strip_comments_and_strings(sql: str) -> str:
    """Remove ``--``/``/* */`` comments and replace string/ident-quoted bodies
    with empty delimiters.

    Used only for the belt-and-suspenders keyword scan. We can't just elide
    content between the outer quotes: we must handle the standard SQL
    doubled-quote escape (``''`` inside ``'...'``) and Postgres dollar-quoted
    strings (``$tag$...$tag$``).
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        # Line comment
        if c == "-" and nxt == "-":
            while i < n and sql[i] != "\n":
                i += 1
            continue

        # Block comment (not nested — Postgres does nest but we don't need to
        # preserve content; we just skip until */).
        if c == "/" and nxt == "*":
            i += 2
            while i < n - 1 and not (sql[i] == "*" and sql[i + 1] == "/"):
                i += 1
            i += 2
            continue

        # Dollar-quoted string: $tag$ ... $tag$
        if c == "$":
            # Find matching closing $tag$.
            j = sql.find("$", i + 1)
            if j != -1:
                tag = sql[i : j + 1]  # e.g. "$$" or "$foo$"
                end = sql.find(tag, j + 1)
                if end != -1:
                    out.append(tag)
                    out.append(tag)
                    i = end + len(tag)
                    continue
            # Fall through on malformed dollar quote.

        # Single-quoted string.
        if c == "'":
            out.append("'")
            i += 1
            while i < n:
                if sql[i] == "'" and i + 1 < n and sql[i + 1] == "'":
                    # Escaped quote — skip both.
                    i += 2
                    continue
                if sql[i] == "'":
                    out.append("'")
                    i += 1
                    break
                i += 1
            continue

        # Double-quoted identifier.
        if c == '"':
            out.append('"')
            i += 1
            while i < n:
                if sql[i] == '"' and i + 1 < n and sql[i + 1] == '"':
                    i += 2
                    continue
                if sql[i] == '"':
                    out.append('"')
                    i += 1
                    break
                i += 1
            continue

        out.append(c)
        i += 1
    return "".join(out)


def _tokenize(sql_upper: str) -> set[str]:
    """Return the set of whitespace/punctuation-separated word tokens."""
    buf: list[str] = []
    tokens: set[str] = set()
    for ch in sql_upper:
        if ch.isalnum() or ch == "_":
            buf.append(ch)
        else:
            if buf:
                tokens.add("".join(buf))
                buf.clear()
    if buf:
        tokens.add("".join(buf))
    return tokens
