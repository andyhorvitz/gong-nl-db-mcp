#!/usr/bin/env bash
# gong-nl-db-mcp installer for macOS.
#
# Usage (the one-liner colleagues run):
#   curl -LsSf https://raw.githubusercontent.com/andyhorvitz/gong-nl-db-mcp/main/scripts/install.sh | bash
#
# What this does:
#   1. Confirms we're on macOS.
#   2. Ensures `uv` is installed (installs via astral.sh if missing).
#   3. Ensures `gcloud` is installed and ADC is set up.
#   4. Writes an MCP server entry into Claude Desktop's config.
#   5. Tells the colleague to restart Claude Desktop.
#
# Re-running is safe: the script is idempotent.

set -euo pipefail

# ----- Settings -----------------------------------------------------------
# These three values must match the production Cloud SQL instance. Update
# them here when the instance moves; installers will pick up the new values
# on their next re-run.
INSTANCE_CONNECTION_NAME="${INSTANCE_CONNECTION_NAME:-REPLACE_ME:region:gong-nl-db}"
DB_NAME="${DB_NAME:-REPLACE_ME}"
IP_TYPE="${IP_TYPE:-PUBLIC}"

PACKAGE="gong-nl-db-mcp"
SERVER_NAME="gong-nl-db"
CLAUDE_CONFIG_DIR="${HOME}/Library/Application Support/Claude"
CLAUDE_CONFIG="${CLAUDE_CONFIG_DIR}/claude_desktop_config.json"

# ----- Helpers ------------------------------------------------------------

log()  { printf "\033[1;34m==>\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m!!\033[0m  %s\n" "$*" >&2; }
die()  { printf "\033[1;31m✗\033[0m  %s\n" "$*" >&2; exit 1; }

# ----- 1. Platform check --------------------------------------------------

[[ "$(uname -s)" == "Darwin" ]] || die "This installer supports macOS only."

# ----- 2. uv --------------------------------------------------------------

if ! command -v uv >/dev/null 2>&1; then
    log "Installing uv (Python package/tool runner)…"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # The uv installer puts the binary in ~/.local/bin. Add it for this session.
    export PATH="${HOME}/.local/bin:${PATH}"
    command -v uv >/dev/null 2>&1 || die "uv install appeared to succeed but 'uv' is not on PATH."
else
    log "uv already installed ($(uv --version))."
fi

# ----- 3. gcloud + ADC ----------------------------------------------------

if ! command -v gcloud >/dev/null 2>&1; then
    warn "gcloud is not installed."
    echo "Install it with:   brew install --cask google-cloud-sdk"
    echo "Or download:       https://cloud.google.com/sdk/docs/install-sdk"
    die "Install gcloud, then re-run this script."
fi

if ! gcloud auth application-default print-access-token >/dev/null 2>&1; then
    log "Logging in with Application Default Credentials…"
    log "A browser window will open. Use your @bairesdev.com Google account."
    gcloud auth application-default login
else
    log "gcloud ADC already set up."
fi

# ----- 4. Write Claude Desktop config ------------------------------------

mkdir -p "${CLAUDE_CONFIG_DIR}"

# Backup any existing config once.
if [[ -f "${CLAUDE_CONFIG}" && ! -f "${CLAUDE_CONFIG}.bak" ]]; then
    cp "${CLAUDE_CONFIG}" "${CLAUDE_CONFIG}.bak"
    log "Backed up existing config to $(basename "${CLAUDE_CONFIG}").bak"
fi

# Merge or create using Python (always available on macOS).
log "Registering MCP server '${SERVER_NAME}' in Claude Desktop config…"
python3 - <<PY
import json, os, sys
path = os.environ["CLAUDE_CONFIG"]
server_name = os.environ["SERVER_NAME"]
package = os.environ["PACKAGE"]
entry = {
    "command": "uvx",
    "args": [f"{package}@latest"],
    "env": {
        "INSTANCE_CONNECTION_NAME": os.environ["INSTANCE_CONNECTION_NAME"],
        "DB_NAME": os.environ["DB_NAME"],
        "IP_TYPE": os.environ["IP_TYPE"],
    },
}
try:
    with open(path) as f:
        cfg = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    cfg = {}
cfg.setdefault("mcpServers", {})[server_name] = entry
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
print(f"Wrote {path}")
PY

# ----- 5. Verify placeholders -------------------------------------------

if [[ "${INSTANCE_CONNECTION_NAME}" == *REPLACE_ME* || "${DB_NAME}" == "REPLACE_ME" ]]; then
    warn "This installer still has REPLACE_ME placeholders for GCP settings."
    warn "Ask the tool owner for the correct INSTANCE_CONNECTION_NAME and DB_NAME,"
    warn "then edit ${CLAUDE_CONFIG} and restart Claude Desktop."
fi

log "Done. Restart Claude Desktop to pick up the new MCP server."
log "After restart, try asking Claude: \"List the schemas in gong-nl-db\"."
