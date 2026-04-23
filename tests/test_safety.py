"""Allow/deny corpus for the read-only SQL validator.

These tests are the audit trail for the read-only guarantee. If any of these
tests start failing, treat it as a P0 — the safety property has regressed.
"""

from __future__ import annotations

import pytest

from gong_nl_db_mcp.safety import (
    UnsafeQueryError,
    inject_limit,
    validate,
)

# --------------------------------------------------------------------------- #
# Queries that MUST pass
# --------------------------------------------------------------------------- #

ALLOWED: list[str] = [
    "SELECT 1",
    "SELECT * FROM accounts",
    "SELECT id, name FROM accounts WHERE active = true",
    "SELECT count(*) FROM calls",
    "SELECT * FROM accounts LIMIT 10",
    "SELECT * FROM accounts ORDER BY created_at DESC LIMIT 100",
    # Joins
    "SELECT a.id, c.subject FROM accounts a JOIN calls c ON c.account_id = a.id",
    # Aggregations
    "SELECT account_id, count(*) FROM calls GROUP BY account_id HAVING count(*) > 5",
    # CTE terminating in SELECT
    "WITH recent AS (SELECT * FROM calls WHERE started_at > now() - interval '7 days') SELECT * FROM recent",
    # Nested CTE
    "WITH a AS (SELECT 1 AS x), b AS (SELECT x + 1 AS y FROM a) SELECT * FROM b",
    # Set operations
    "SELECT id FROM accounts UNION SELECT id FROM leads",
    "SELECT id FROM accounts INTERSECT SELECT id FROM partners",
    "SELECT id FROM accounts EXCEPT SELECT id FROM churned",
    # Subqueries
    "SELECT * FROM accounts WHERE id IN (SELECT account_id FROM calls)",
    # Window functions
    "SELECT id, row_number() OVER (PARTITION BY account_id ORDER BY started_at) FROM calls",
    # EXPLAIN
    "EXPLAIN SELECT * FROM accounts",
    "EXPLAIN SELECT count(*) FROM calls WHERE started_at > now() - interval '1 day'",
    # String literals that contain dangerous-looking keywords
    "SELECT * FROM accounts WHERE name = 'DROP TABLE foo'",
    "SELECT * FROM accounts WHERE note = '-- this is not a comment in a string'",
    # Line comments and block comments (stripped before keyword scan)
    "-- show everything\nSELECT * FROM accounts",
    "/* find active */ SELECT * FROM accounts WHERE active = true",
    # Trailing semicolon
    "SELECT 1;",
]


# --------------------------------------------------------------------------- #
# Queries that MUST be rejected
# --------------------------------------------------------------------------- #

REJECTED: list[tuple[str, str]] = [
    # Empty
    ("", "empty"),
    ("   \n  ", "empty"),
    # DML
    ("INSERT INTO accounts (id) VALUES (1)", "INSERT"),
    ("UPDATE accounts SET name = 'x' WHERE id = 1", "UPDATE"),
    ("DELETE FROM accounts", "DELETE"),
    ("DELETE FROM accounts WHERE id = 1", "DELETE"),
    ("MERGE INTO accounts USING src ON src.id = accounts.id WHEN MATCHED THEN UPDATE SET name = src.name", "MERGE"),
    # DDL
    ("DROP TABLE accounts", "DROP"),
    ("CREATE TABLE evil (id int)", "CREATE"),
    ("ALTER TABLE accounts ADD COLUMN evil text", "ALTER"),
    ("TRUNCATE accounts", "TRUNCATE"),
    # DCL
    ("GRANT SELECT ON accounts TO public", "GRANT"),
    ("REVOKE SELECT ON accounts FROM public", "REVOKE"),
    # COPY / bulk
    ("COPY accounts TO '/tmp/out.csv'", "COPY"),
    ("COPY accounts FROM '/tmp/in.csv'", "COPY"),
    # Session / txn control
    ("SET search_path TO public", "SET"),
    ("BEGIN; SELECT 1; COMMIT;", "multiple"),
    ("RESET ALL", "RESET"),
    # CTE that hides a DML
    ("WITH d AS (DELETE FROM accounts RETURNING id) SELECT * FROM d", "DELETE"),
    ("WITH i AS (INSERT INTO accounts (id) VALUES (1) RETURNING id) SELECT * FROM i", "INSERT"),
    ("WITH u AS (UPDATE accounts SET name='x' RETURNING id) SELECT * FROM u", "UPDATE"),
    # SELECT INTO creates a table
    ("SELECT * INTO new_table FROM accounts", "INTO"),
    # Locking reads
    ("SELECT * FROM accounts FOR UPDATE", "FOR UPDATE"),
    ("SELECT * FROM accounts FOR SHARE", "FOR SHARE"),
    # Multi-statement injection attempts
    ("SELECT 1; DROP TABLE accounts;", "multiple"),
    ("SELECT 1; SELECT 2;", "multiple"),
    # Procedural
    ("CALL some_procedure()", "CALL"),
    ("DO $$ BEGIN DELETE FROM accounts; END $$", "DO"),
    # Maintenance
    ("VACUUM accounts", "VACUUM"),
    ("ANALYZE accounts", "ANALYZE"),
    # LISTEN/NOTIFY
    ("LISTEN channel", "LISTEN"),
    ("NOTIFY channel, 'hi'", "NOTIFY"),
    # EXPLAIN wrapping a write
    ("EXPLAIN DELETE FROM accounts", "DELETE"),
    ("EXPLAIN UPDATE accounts SET name='x'", "UPDATE"),
    # Garbage
    ("this is not sql", "parse"),
]


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("sql", ALLOWED)
def test_allowed(sql: str) -> None:
    result = validate(sql)
    assert result.sql, "validator must return non-empty normalized SQL"


@pytest.mark.parametrize("sql,_reason", REJECTED)
def test_rejected(sql: str, _reason: str) -> None:
    with pytest.raises(UnsafeQueryError):
        validate(sql)


# --------------------------------------------------------------------------- #
# LIMIT injection
# --------------------------------------------------------------------------- #


def test_inject_limit_adds_limit_when_missing() -> None:
    out = inject_limit("SELECT * FROM accounts", 1000)
    assert "LIMIT 1000" in out.upper()


def test_inject_limit_respects_smaller_existing_limit() -> None:
    out = inject_limit("SELECT * FROM accounts LIMIT 10", 1000)
    # Existing LIMIT 10 is smaller than cap; should stay 10.
    assert "LIMIT 10" in out.upper()
    assert "LIMIT 1000" not in out.upper()


def test_inject_limit_clamps_larger_existing_limit() -> None:
    out = inject_limit("SELECT * FROM accounts LIMIT 999999", 1000)
    assert "LIMIT 1000" in out.upper()
    assert "999999" not in out


def test_inject_limit_leaves_explain_untouched() -> None:
    sql = "EXPLAIN SELECT * FROM accounts"
    out = inject_limit(sql, 1000)
    assert out == sql


def test_inject_limit_rejects_non_positive() -> None:
    with pytest.raises(ValueError):
        inject_limit("SELECT 1", 0)
    with pytest.raises(ValueError):
        inject_limit("SELECT 1", -5)
