#!/usr/bin/env bash
# gong-nl-db-mcp installer for macOS.
#
# Usage (the one-liner colleagues run):
#   curl -LsSf https://raw.githubusercontent.com/andyhorvitz/gong-nl-db-mcp/main/scripts/install.sh | bash
#
# What this does:
#   1. Confirms we're on macOS.
#   2. Ensures `uv` is installed (installs via astral.sh if missing).
#   3. Ensures `gcloud` is installed (auto-installs if missing) and ADC is set up.
#   4. Installs gong-nl-db-mcp as a persistent uv tool — a locked venv that
#      does NOT re-resolve dependencies on every Claude Desktop restart.
#   5. Writes an MCP server entry into Claude Desktop's config using the
#      absolute path to the installed binary (no uvx, no @latest).
#   6. Runs a smoke test to confirm the binary starts cleanly.
#   7. Tells the colleague to restart Claude Desktop.
#
# Re-running is safe: the script is idempotent and upgrades the tool in place.

set -euo pipefail

# ----- Settings -----------------------------------------------------------
INSTANCE_CONNECTION_NAME="${INSTANCE_CONNECTION_NAME:-planar-ray-494004-b8:us-central1:gong-nl-db}"
DB_NAME="${DB_NAME:-gong}"
IP_TYPE="${IP_TYPE:-PUBLIC}"

PACKAGE="gong-nl-db-mcp"
# Pin to a known-good release. Re-run this installer (with an updated version
# here) to upgrade. Users are never auto-upgraded on Claude Desktop restart.
PACKAGE_VERSION="0.1.8"
PYTHON_VERSION="3.13"
SERVER_NAME="gong-nl-db"
CLAUDE_CONFIG_DIR="${HOME}/Library/Application Support/Claude"
CLAUDE_CONFIG="${CLAUDE_CONFIG_DIR}/claude_desktop_config.json"

# ----- Helpers ------------------------------------------------------------

log()  { printf "\033[1;34m==>\033[0m %s\n" "$*"; }
ok()   { printf "\033[1;32m✓\033[0m  %s\n" "$*"; }
warn() { printf "\033[1;33m!!\033[0m  %s\n" "$*" >&2; }
die()  { printf "\033[1;31m✗\033[0m  %s\n" "$*" >&2; exit 1; }

# ----- 1. Platform check --------------------------------------------------

[[ "$(uname -s)" == "Darwin" ]] || die "This installer supports macOS only."

# ----- 2. uv --------------------------------------------------------------

# ~/.local/bin is where uv installs its own binary and tool binaries.
export PATH="${HOME}/.local/bin:${PATH}"

if ! command -v uv >/dev/null 2>&1; then
    log "Installing uv (Python package/tool runner)…"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    command -v uv >/dev/null 2>&1 || die "uv install appeared to succeed but 'uv' is not on PATH."
else
    log "uv already installed ($(uv --version))."
fi

# ----- 3. gcloud + ADC ----------------------------------------------------

if ! command -v gcloud >/dev/null 2>&1; then
    if [[ -x "${HOME}/google-cloud-sdk/bin/gcloud" ]]; then
        export PATH="${HOME}/google-cloud-sdk/bin:${PATH}"
        log "Found existing gcloud at ~/google-cloud-sdk (added to PATH for this session)."
    else
        log "gcloud not found — installing Google Cloud SDK to ~/google-cloud-sdk…"
        log "(This is the official Google installer. ~500MB, ~30 seconds.)"
        curl -sSL https://sdk.cloud.google.com > /tmp/gcloud-install-$$.sh
        bash /tmp/gcloud-install-$$.sh --disable-prompts --install-dir="${HOME}" </dev/null
        rm -f /tmp/gcloud-install-$$.sh
        export PATH="${HOME}/google-cloud-sdk/bin:${PATH}"
        command -v gcloud >/dev/null 2>&1 || die "gcloud install appeared to succeed but 'gcloud' is not on PATH."
        log "gcloud installed. (Open a new terminal later to use 'gcloud' outside this script.)"
    fi
fi

if ! gcloud auth application-default print-access-token >/dev/null 2>&1; then
    log "Logging in with Application Default Credentials…"
    log "A browser window will open. Use your @bairesdev.com Google account."
    gcloud auth application-default login
else
    log "gcloud ADC already set up."
fi

log "Setting ADC quota project…"
gcloud auth application-default set-quota-project "${INSTANCE_CONNECTION_NAME%%:*}" 2>/dev/null \
    && ok "Quota project set." \
    || warn "Could not set quota project — run manually: gcloud auth application-default set-quota-project planar-ray-494004-b8"

# ----- 4. Install as a persistent uv tool --------------------------------
# `uv tool install` creates a locked venv that persists across Claude Desktop
# restarts. It does NOT re-resolve dependencies on every launch — unlike uvx,
# which re-resolves on every invocation and can silently pull breaking upstream
# SDK releases overnight.
#
# `--force` reinstalls even if the same version is already present, ensuring
# a clean venv on re-runs (e.g. after a broken partial install).

log "Installing ${PACKAGE}==${PACKAGE_VERSION} as a persistent uv tool…"
uv tool install --python "${PYTHON_VERSION}" "${PACKAGE}==${PACKAGE_VERSION}" --force \
    || die "uv tool install failed — see above for details."
ok "Installed ${PACKAGE}==${PACKAGE_VERSION} on Python ${PYTHON_VERSION}."

# The tool binary lands in ~/.local/bin (already on PATH above).
BINARY_PATH="$(command -v "${PACKAGE}")" \
    || die "Binary not found after install — expected at ${HOME}/.local/bin/${PACKAGE}"
log "Tool binary at: ${BINARY_PATH}"

# ----- 5. Write Claude Desktop config ------------------------------------

mkdir -p "${CLAUDE_CONFIG_DIR}"

if [[ -f "${CLAUDE_CONFIG}" && ! -f "${CLAUDE_CONFIG}.bak" ]]; then
    cp "${CLAUDE_CONFIG}" "${CLAUDE_CONFIG}.bak"
    log "Backed up existing config to $(basename "${CLAUDE_CONFIG}").bak"
fi

log "Registering MCP server '${SERVER_NAME}' in Claude Desktop config…"
CLAUDE_CONFIG="${CLAUDE_CONFIG}" \
SERVER_NAME="${SERVER_NAME}" \
BINARY_PATH="${BINARY_PATH}" \
INSTANCE_CONNECTION_NAME="${INSTANCE_CONNECTION_NAME}" \
DB_NAME="${DB_NAME}" \
IP_TYPE="${IP_TYPE}" \
python3 - <<'PY'
import json, os
path    = os.environ["CLAUDE_CONFIG"]
entry = {
    # Absolute path to the uv-installed binary — no uvx wrapper, no args.
    # Claude Desktop has a restricted PATH that excludes ~/.local/bin, so
    # the absolute path is required.
    "command": os.environ["BINARY_PATH"],
    "args": [],
    "env": {
        "INSTANCE_CONNECTION_NAME": os.environ["INSTANCE_CONNECTION_NAME"],
        "DB_NAME":                  os.environ["DB_NAME"],
        "IP_TYPE":                  os.environ["IP_TYPE"],
    },
}
try:
    with open(path) as f:
        cfg = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    cfg = {}
cfg.setdefault("mcpServers", {})[os.environ["SERVER_NAME"]] = entry
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
print(f"Wrote {path}")
PY

# ----- 6. Verify placeholders -------------------------------------------

if [[ "${INSTANCE_CONNECTION_NAME}" == *REPLACE_ME* || "${DB_NAME}" == "REPLACE_ME" ]]; then
    warn "This installer still has REPLACE_ME placeholders for GCP settings."
    warn "Ask the tool owner for the correct INSTANCE_CONNECTION_NAME and DB_NAME,"
    warn "then edit ${CLAUDE_CONFIG} and restart Claude Desktop."
    exit 0
fi

# ----- 7. Smoke test — confirm the binary starts cleanly -----------------

log "Running smoke test…"
if "${BINARY_PATH}" --version >/dev/null 2>&1; then
    VERSION_OUT="$("${BINARY_PATH}" --version 2>/dev/null)"
    ok "Smoke test passed — ${VERSION_OUT} installed and starts cleanly."
else
    warn "Smoke test failed. Check ~/Library/Logs/Claude/ after restarting Claude Desktop."
    warn "Common fix: re-run this installer."
fi

log "Done. Restart Claude Desktop to pick up the new MCP server."
log "After restart, try asking Claude: \"List the schemas in gong-nl-db\"."
