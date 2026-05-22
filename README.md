# gong-nl-db-mcp

Read-only Claude Desktop access to the BairesDev `gong-nl-db` Cloud SQL
Postgres instance.

This is an **MCP server** that colleagues install on their Mac. Once set up,
they can ask Claude Desktop questions like "what tables are in gong-nl-db?" or
"show me last week's top 10 accounts by call volume" and Claude will query the
database directly — always read-only, always audited to their personal
@bairesdev.com identity.

---

## For colleagues (one-time setup, ~3 minutes)

You need:
- macOS
- Claude Desktop ([download](https://claude.ai/download))
- A Google Cloud SDK install — if you don't have `gcloud`:
  ```sh
  brew install --cask google-cloud-sdk
  ```

Then run:

```sh
curl -LsSf https://raw.githubusercontent.com/andyhorvitz/gong-nl-db-mcp/main/scripts/install.sh | bash
```

The installer will:
1. Install `uv` (tiny Python runner) if you don't have it.
2. Prompt you to sign in to Google with `gcloud auth application-default login`.
   Use your `@bairesdev.com` account.
3. Register the `gong-nl-db` MCP server in Claude Desktop's config.

**Restart Claude Desktop** and try asking it: *"List the schemas in gong-nl-db."*

If you get a permissions error, ping Andy — he needs to grant your Google
account access to the Cloud SQL instance (see the owner setup section below).

### Troubleshooting

**`CERTIFICATE_VERIFY_FAILED` / SSL errors in Claude Desktop's logs**

This is the most common failure. The installer pins the server to Python 3.12
(`--python 3.12` in the Claude Desktop config), which avoids the issue entirely
on a fresh install. If you hit it anyway (e.g. you installed before this fix):

```sh
# 1. Clear the cached old package
uv cache clean gong-nl-db-mcp

# 2. Re-run the installer to update your Claude Desktop config
curl -LsSf https://raw.githubusercontent.com/andyhorvitz/gong-nl-db-mcp/main/scripts/install.sh | bash

# 3. Fully quit and reopen Claude Desktop (⌘Q, not just close the window)
```

**"Could not determine IAM DB username"**

You either aren't logged in or logged in with the wrong account. Run:

```sh
gcloud auth application-default login
# Use your @bairesdev.com account when the browser opens.
```

Then restart Claude Desktop.

**`Failed to spawn process: No such file or directory`**

Claude Desktop launches with a stripped PATH that excludes `~/.local/bin`
(where `uv` installs its tools by default). Fix: symlink `uvx` into a
directory Claude Desktop can see, then re-run the installer:

```sh
sudo ln -sf "$(which uvx)" /usr/local/bin/uvx
curl -LsSf https://raw.githubusercontent.com/andyhorvitz/gong-nl-db-mcp/main/scripts/install.sh | bash
```

The installer now writes the **absolute path** to `uvx` into the config
automatically, so a fresh install won't hit this.

**MCP server not appearing in Claude Desktop**

Check `~/Library/Logs/Claude/` for stderr from the server. Also verify the
entry exists in `~/Library/Application Support/Claude/claude_desktop_config.json`
under `mcpServers.gong-nl-db`.

### What you can do

Claude will have these tools available under the `gong-nl-db` MCP server:

| Tool | What it does |
| --- | --- |
| `list_schemas` | Show non-system schemas |
| `list_tables(schema)` | Show tables/views in a schema |
| `describe_table(table, schema)` | Show columns, types, nullability |
| `sample_rows(table, schema, limit)` | Return up to 50 sample rows |
| `run_query(sql, limit)` | Run a read-only SELECT / WITH / set-op (max 1000 rows) |
| `explain_query(sql)` | Return the query plan |

### What you *can't* do

Every query is checked against a read-only allow-list **before** it reaches
the database. Attempting `INSERT`, `UPDATE`, `DELETE`, `DROP`, `TRUNCATE`,
`COPY`, `CALL`, `VACUUM`, `SET`, etc. will be rejected. Even if that layer
somehow let a write through, the Postgres role you connect as only has
`SELECT` grants and the transaction is explicitly `READ ONLY`. Four layers
of defense — you are not going to accidentally drop prod.

---

## For the owner (Andy): initial Cloud SQL setup

This is a one-time-per-instance setup. After this, each new colleague just
needs the per-user steps below.

### 1. Enable IAM database authentication on the instance

```sh
gcloud sql instances patch gong-nl-db \
  --database-flags=cloudsql.iam_authentication=on,cloudsql.enable_pgaudit=on,pgaudit.log=read
```

### 2. Create the read-only Postgres role

Connect as a superuser (e.g. via `cloud-sql-proxy` + `psql`):

```sql
CREATE ROLE readonly_analysts;
GRANT CONNECT ON DATABASE <db> TO readonly_analysts;
GRANT USAGE ON SCHEMA public TO readonly_analysts;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO readonly_analysts;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO readonly_analysts;
ALTER DATABASE <db> SET default_transaction_read_only = on;
```

Repeat the `GRANT USAGE` / `GRANT SELECT` / `ALTER DEFAULT PRIVILEGES` block
for each additional schema you want to expose.

### 3. For each colleague (e.g. `alice@bairesdev.com`)

```sh
# GCP IAM — lets them authenticate to the instance
gcloud projects add-iam-policy-binding <PROJECT_ID> \
  --member=user:alice@bairesdev.com --role=roles/cloudsql.client
gcloud projects add-iam-policy-binding <PROJECT_ID> \
  --member=user:alice@bairesdev.com --role=roles/cloudsql.instanceUser

# Cloud SQL — registers them as an IAM DB user on the instance
gcloud sql users create alice@bairesdev.com \
  --instance=gong-nl-db --type=cloud_iam_user
```

Then, in Postgres:

```sql
GRANT readonly_analysts TO "alice@bairesdev.com";
```

### 4. Configure the installer

Edit `scripts/install.sh` and replace the `REPLACE_ME` placeholders with:
- `INSTANCE_CONNECTION_NAME` — `<project>:<region>:gong-nl-db`
- `DB_NAME` — the Postgres database name

Commit, push to `main`. Next colleague who re-runs the one-liner picks up the
new config.

---

## Development

```sh
uv venv --python 3.12
uv pip install -e ".[dev]"
.venv/bin/pytest                       # run the safety test suite
```

Test the MCP server locally against a running Cloud SQL Auth Proxy or the
live instance:

```sh
INSTANCE_CONNECTION_NAME=... DB_NAME=... \
  .venv/bin/gong-nl-db-mcp    # speaks MCP over stdio
```

### Releasing

Tag-driven: `git tag v0.2.0 && git push --tags` triggers
`.github/workflows/release.yml`, which publishes to PyPI. Colleagues' `uvx
gong-nl-db-mcp@latest` picks it up automatically.

### The safety guarantee

`src/gong_nl_db_mcp/safety.py` is the statement-level allow-list. **Any
change to that file must go through PR review.** The file's git history is
the audit trail for the read-only guarantee. See `tests/test_safety.py` for
the allow/deny corpus.
