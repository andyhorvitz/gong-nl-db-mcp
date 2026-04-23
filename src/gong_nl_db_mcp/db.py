"""Cloud SQL Postgres connection layer with IAM auth and read-only enforcement.

Connects to Cloud SQL via the Cloud SQL Python Connector using the colleague's
Application Default Credentials. No passwords, no service-account keys.

Every query is executed inside a ``BEGIN READ ONLY ... ROLLBACK`` block with a
``statement_timeout`` set via ``SET LOCAL``, providing the per-session and
per-query layers of the read-only defense-in-depth stack.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from google.auth import default as google_auth_default
from google.cloud.sql.connector import Connector, IPTypes

log = logging.getLogger(__name__)


# Defaults; all overridable via env.
DEFAULT_STATEMENT_TIMEOUT_MS = 30_000
DEFAULT_IDLE_TXN_TIMEOUT_MS = 30_000


@dataclass(frozen=True)
class DbConfig:
    """Configuration for the Cloud SQL connection.

    All fields are sourced from environment variables by :meth:`from_env`.
    """

    instance_connection_name: str
    """``<project>:<region>:<instance>`` — e.g. ``my-proj:us-central1:gong-nl-db``."""

    db_name: str
    """Postgres database name on the instance."""

    ip_type: IPTypes = IPTypes.PUBLIC
    """PUBLIC or PRIVATE. PRIVATE requires the colleague to be on a VPC with
    access to the Cloud SQL instance's private IP."""

    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS
    idle_txn_timeout_ms: int = DEFAULT_IDLE_TXN_TIMEOUT_MS

    @classmethod
    def from_env(cls) -> DbConfig:
        icn = os.environ.get("INSTANCE_CONNECTION_NAME", "").strip()
        db = os.environ.get("DB_NAME", "").strip()
        if not icn:
            raise RuntimeError(
                "INSTANCE_CONNECTION_NAME is required "
                "(format: project:region:instance)"
            )
        if not db:
            raise RuntimeError("DB_NAME is required")

        ip_type = IPTypes.PUBLIC
        if os.environ.get("IP_TYPE", "").upper() == "PRIVATE":
            ip_type = IPTypes.PRIVATE

        return cls(
            instance_connection_name=icn,
            db_name=db,
            ip_type=ip_type,
            statement_timeout_ms=int(
                os.environ.get("STATEMENT_TIMEOUT_MS", DEFAULT_STATEMENT_TIMEOUT_MS)
            ),
            idle_txn_timeout_ms=int(
                os.environ.get("IDLE_TXN_TIMEOUT_MS", DEFAULT_IDLE_TXN_TIMEOUT_MS)
            ),
        )


@dataclass
class QueryResult:
    """Rows returned from a read-only query."""

    columns: list[str]
    rows: list[tuple[Any, ...]]
    row_count: int
    truncated: bool
    """True if the database returned more rows than we returned to the caller."""


class Db:
    """Thin wrapper over the Cloud SQL Python Connector.

    Holds a single :class:`Connector` for the lifetime of the MCP server and
    opens a fresh connection for each query (connector handles pooling
    internally and refreshes IAM tokens automatically).
    """

    def __init__(self, cfg: DbConfig) -> None:
        self.cfg = cfg
        self._iam_user = _resolve_iam_user()
        log.info(
            "initializing Cloud SQL connector for %s as %s",
            cfg.instance_connection_name,
            self._iam_user,
        )
        self._connector = Connector(refresh_strategy="lazy")

    def close(self) -> None:
        self._connector.close()

    # ------------------------------------------------------------------ #
    # Public query API
    # ------------------------------------------------------------------ #

    def run_readonly(self, sql: str, max_rows: int) -> QueryResult:
        """Execute ``sql`` inside a ``READ ONLY`` transaction and return rows.

        The caller is responsible for ensuring ``sql`` has already passed
        :func:`gong_nl_db_mcp.safety.validate`. This method does NOT re-validate;
        it layers the DB-side read-only protections on top.
        """
        conn = self._connect()
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                # Per-session read-only characteristics (belt + suspenders on
                # top of the DB-level default_transaction_read_only).
                cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
                # Start the actual read-only transaction for the query.
                cur.execute("BEGIN READ ONLY")
                try:
                    cur.execute(
                        f"SET LOCAL statement_timeout = {self.cfg.statement_timeout_ms}"
                    )
                    cur.execute(
                        "SET LOCAL idle_in_transaction_session_timeout = "
                        f"{self.cfg.idle_txn_timeout_ms}"
                    )
                    cur.execute(sql)
                    columns = (
                        [d[0] for d in cur.description] if cur.description else []
                    )
                    rows: list[tuple[Any, ...]] = []
                    truncated = False
                    if columns:
                        # Fetch up to max_rows+1 so we can detect truncation.
                        fetched = cur.fetchmany(max_rows + 1)
                        if len(fetched) > max_rows:
                            truncated = True
                            fetched = fetched[:max_rows]
                        rows = [tuple(r) for r in fetched]
                    return QueryResult(
                        columns=columns,
                        rows=rows,
                        row_count=len(rows),
                        truncated=truncated,
                    )
                finally:
                    # Always ROLLBACK — we never want to accidentally commit,
                    # even though the transaction is READ ONLY.
                    cur.execute("ROLLBACK")
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #

    def _connect(self) -> Any:
        """Open a new pg8000 connection via the Cloud SQL Connector."""
        return self._connector.connect(
            self.cfg.instance_connection_name,
            "pg8000",
            user=self._iam_user,
            db=self.cfg.db_name,
            enable_iam_auth=True,
            ip_type=self.cfg.ip_type,
        )


def _resolve_iam_user() -> str:
    """Return the IAM DB username for the current ADC principal.

    For user credentials this is the email address (e.g.
    ``alice@bairesdev.com``). For service accounts, Cloud SQL expects the
    principal's email MINUS the ``.gserviceaccount.com`` suffix.
    """
    creds, _project = google_auth_default(
        scopes=["https://www.googleapis.com/auth/sqlservice.admin"]
    )

    # User credentials expose the account via .id_token or the quota_project
    # plus a .service_account_email for SA creds. Most reliable: the credentials
    # subject on refresh. But we can avoid a refresh by inspecting attributes.
    email = getattr(creds, "service_account_email", None) or getattr(
        creds, "_service_account_email", None
    )
    if email:
        # Service account: strip .gserviceaccount.com per Cloud SQL IAM docs.
        if email.endswith(".gserviceaccount.com"):
            email = email[: -len(".gserviceaccount.com")]
        return email

    # User ADC: the gcloud CLI writes the active account to
    # application_default_credentials.json; google-auth exposes it via
    # .quota_project_id? No — use the IAM Credentials API via an ID-token call
    # or just honor an explicit override. We ship an override env var for the
    # rare case where introspection fails.
    override = os.environ.get("IAM_DB_USER", "").strip()
    if override:
        return override

    # Fall back to refreshing the creds and reading .signer_email / .account.
    account = getattr(creds, "account", None) or getattr(creds, "signer_email", None)
    if account:
        return account

    raise RuntimeError(
        "Could not determine IAM DB username from ADC. "
        "Set IAM_DB_USER to your Google account email (e.g. alice@bairesdev.com)."
    )
