"""Apply numbered SQL migrations to the gong-nl-db Cloud SQL instance.

Migrations are files in ``migrations/NNN_name.sql`` where ``NNN`` is a
zero-padded integer. Applied versions are recorded in
``public.schema_migrations``. Each file runs in its own transaction
UNLESS its header contains ``-- migrate: no-transaction`` (required for
``CREATE INDEX CONCURRENTLY``).

Connection:
  * IAM ADC by default (same path as the MCP server).
  * If ``PGPASSWORD_FILE`` env var is set, falls back to built-in user
    auth with that password. Used for migrations on throwaway clones.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from google.cloud.sql.connector import Connector, IPTypes

log = logging.getLogger("migrate")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
VERSION_RE = re.compile(r"^(\d{3,})_.+\.sql$")
NO_TXN_MARKER = "-- migrate: no-transaction"


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path
    sql: str
    sha256: str
    no_transaction: bool


def discover() -> list[Migration]:
    items: list[Migration] = []
    for p in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = VERSION_RE.match(p.name)
        if not m:
            continue
        sql = p.read_text()
        items.append(
            Migration(
                version=m.group(1),
                name=p.stem,
                path=p,
                sql=sql,
                sha256=hashlib.sha256(sql.encode()).hexdigest(),
                no_transaction=any(
                    NO_TXN_MARKER in line for line in sql.splitlines()[:10]
                ),
            )
        )
    return items


def connect():
    icn = os.environ.get(
        "INSTANCE_CONNECTION_NAME",
        "planar-ray-494004-b8:us-central1:gong-nl-db-clone",
    )
    db = os.environ.get("DB_NAME", "gong")
    ip_type = IPTypes.PRIVATE if os.environ.get("IP_TYPE", "").upper() == "PRIVATE" else IPTypes.PUBLIC

    pw_file = os.environ.get("PGPASSWORD_FILE")
    connector = Connector(refresh_strategy="lazy")
    if pw_file:
        pw = Path(pw_file).read_text().strip()
        user = os.environ.get("PGUSER", "postgres")
        conn = connector.connect(
            icn, "pg8000", user=user, password=pw, db=db, ip_type=ip_type
        )
        log.info("connected to %s/%s as %s (password auth)", icn, db, user)
    else:
        from gong_nl_db_mcp.db import _resolve_iam_user  # type: ignore

        user = _resolve_iam_user()
        conn = connector.connect(
            icn, "pg8000", user=user, db=db, enable_iam_auth=True, ip_type=ip_type
        )
        log.info("connected to %s/%s as %s (IAM)", icn, db, user)
    # The clone (and prod) has default_transaction_read_only = on at the DB
    # level for safety. Flip this session to read-write so DDL + inserts
    # into schema_migrations can run.
    cur = conn.cursor()
    cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE")
    cur.execute("SET default_transaction_read_only = off")
    conn.commit()
    cur.close()
    return connector, conn


def ensure_table(conn) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS public.schema_migrations (
          version     text PRIMARY KEY,
          name        text NOT NULL,
          sha256      text NOT NULL,
          applied_at  timestamptz NOT NULL DEFAULT now(),
          duration_ms integer NOT NULL
        )
        """
    )
    conn.commit()
    cur.close()


def applied_versions(conn) -> dict[str, str]:
    cur = conn.cursor()
    cur.execute("SELECT version, sha256 FROM public.schema_migrations")
    out = {v: h for v, h in cur.fetchall()}
    cur.close()
    return out


def apply_one(conn, m: Migration) -> None:
    t0 = time.time()
    if m.no_transaction:
        # Run each statement separately outside a txn. Caller must have
        # autocommit on.
        prev = conn.autocommit
        conn.autocommit = True
        cur = conn.cursor()
        try:
            # pg8000 doesn't split multi-statement strings reliably across
            # CONCURRENTLY boundaries — split on semicolons at line starts.
            for stmt in _split_statements(m.sql):
                if stmt.strip():
                    log.info("  exec: %s...", stmt.strip().splitlines()[0][:80])
                    cur.execute(stmt)
        finally:
            cur.close()
            conn.autocommit = prev
        ms = int((time.time() - t0) * 1000)
        # Record the version in a separate committed txn
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO public.schema_migrations(version,name,sha256,duration_ms) VALUES (%s,%s,%s,%s)",
            (m.version, m.name, m.sha256, ms),
        )
        conn.commit()
        cur.close()
    else:
        cur = conn.cursor()
        try:
            cur.execute("BEGIN")
            cur.execute(m.sql)
            cur.execute(
                "INSERT INTO public.schema_migrations(version,name,sha256,duration_ms) VALUES (%s,%s,%s,%s)",
                (m.version, m.name, m.sha256, int((time.time() - t0) * 1000)),
            )
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        finally:
            cur.close()


def _split_statements(sql: str) -> list[str]:
    """Very small splitter for migrations with no-transaction marker.

    Splits on ``;`` that end a line (ignoring those inside single-line
    ``--`` comments). Good enough for our DDL files; not a full parser.
    """
    out: list[str] = []
    buf: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        buf.append(line)
        if stripped.endswith(";"):
            out.append("\n".join(buf))
            buf = []
    if buf and "\n".join(buf).strip():
        out.append("\n".join(buf))
    return out


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    ap = argparse.ArgumentParser()
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Show plan; don't apply.")
    group.add_argument("--apply", action="store_true", help="Apply pending migrations.")
    group.add_argument("--status", action="store_true", help="Show applied versions.")
    args = ap.parse_args()

    migrations = discover()
    if not migrations:
        log.error("no migrations found under %s", MIGRATIONS_DIR)
        return 1

    connector, conn = connect()
    try:
        ensure_table(conn)
        applied = applied_versions(conn)

        pending: list[Migration] = []
        for m in migrations:
            prev = applied.get(m.version)
            if prev is None:
                pending.append(m)
            elif prev != m.sha256:
                log.error(
                    "migration %s already applied but file has changed "
                    "(stored sha=%s, file sha=%s). Aborting.",
                    m.version, prev[:12], m.sha256[:12],
                )
                return 2

        if args.status:
            print(f"Applied: {len(applied)}  Pending: {len(pending)}")
            for m in migrations:
                tag = "APPLIED" if m.version in applied else "pending"
                print(f"  {tag:8} {m.version}  {m.name}  (txn={not m.no_transaction})")
            return 0

        if args.dry_run:
            print(f"{len(pending)} pending migration(s):")
            for m in pending:
                print(f"  {m.version}  {m.name}  ({len(m.sql)} bytes, txn={not m.no_transaction})")
            return 0

        # apply
        for m in pending:
            log.info("applying %s %s ...", m.version, m.name)
            apply_one(conn, m)
            log.info("  OK")
        log.info("done: %d applied", len(pending))
        return 0
    finally:
        conn.close()
        connector.close()


if __name__ == "__main__":
    sys.exit(main())
