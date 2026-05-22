#!/usr/bin/env bash
# gong-nl-db-mcp installer for macOS.
#
# Usage (the one-liner colleagues run):
#   curl -LsSf https://raw.githubusercontent.com/andyhorvitz/gong-nl-db-mcp/main/scripts/install.sh | bash
#
# What this does:
#   1. Confirms we're on macOS.
#   2. Ensures `uv` is installed (installs via astral.sh if missing).
#   3. Ensures `gcloud` is installed (auto-installs via sdk.cloud.google.com
#      if missing) and ADC is set up.
#   4. Clears any cached old version of the package so the next run is fresh.
#   5. Writes an MCP server entry into Claude Desktop's config (pinned to
#      Python 3.12 for SSL compatibility).
#   6. Runs a smoke test to confirm the package imports cleanly.
#   7. Tells the colleague to restart Claude Desktop.
#
# Re-running is safe: the script is idempotent.

set -euo pipefail

# ----- Settings -----------------------------------------------------------
# These three values must match the production Cloud SQL instance. Update
# them here when the instance moves; installers will pick up the new values
# on their next re-run.
INSTANCE_CONNECTION_NAME="${INSTANCE_CONNECTION_NAME:-planar-ray-494004-b8:us-central1:gong-nl-db}"
DB_NAME="${DB_NAME:-gong}"
IP_TYPE="${IP_TYPE:-PUBLIC}"

PACKAGE="gong-nl-db-mcp"
# Pin to Python 3.12. The package is tested on 3.12 in CI and cloud-sql-
# python-connector has SSL behaviour differences on 3.13+ (and outright
# breakage on 3.14) in uvx's isolated environment.
PYTHON_VERSION="3.12"
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
    # Try the standard install location in case it's installed but not on PATH
    # in the current shell (Google's installer adds it to ~/.bashrc/.zshrc but
    # those aren't sourced by a non-interactive pipe-to-bash invocation).
    if [[ -x "${HOME}/google-cloud-sdk/bin/gcloud" ]]; then
        export PATH="${HOME}/google-cloud-sdk/bin:${PATH}"
        log "Found existing gcloud at ~/google-cloud-sdk (added to PATH for this session)."
    else
        log "gcloud not found — installing Google Cloud SDK to ~/google-cloud-sdk…"
        log "(This is the official Google installer. ~500MB, ~30 seconds.)"
        # --disable-prompts: non-interactive (uses defaults, modifies shell rc).
        # --install-dir: where to install (defaults to $HOME).
        # Pipe an empty string to stdin so any residual prompt reads as EOF.
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

# ----- 4. Resolve the full path to uvx -----------------------------------
# Claude Desktop launches with a restricted PATH (/usr/local/bin,
# /opt/homebrew/bin, /usr/bin, /bin, /usr/sbin, /sbin). If uv was installed
# via the astral.sh script it lands in ~/.local/bin, which is NOT in that
# list — causing "Failed to spawn process: No such file or directory" even
# though uvx works fine in the terminal.
# Fix: write the absolute path to uvx into the Claude Desktop config, and
# ensure it's also symlinked into /usr/local/bin for good measure.

UVX_PATH="$(command -v uvx)"
[[ -n "${UVX_PATH}" ]] || die "uvx not found on PATH — this should not happen after the install step above."

# Symlink into /usr/local/bin if it isn't already reachable there.
if [[ ! -x "/usr/local/bin/uvx" ]]; then
    log "Symlinking uvx → /usr/local/bin/uvx so Claude Desktop can find it…"
    sudo ln -sf "${UVX_PATH}" /usr/local/bin/uvx \
        && ok "Symlinked ${UVX_PATH} → /usr/local/bin/uvx" \
        || warn "Could not create symlink (sudo failed). Will use full path in config instead."
fi

# Always use the absolute path in the config regardless — belt and suspenders.
log "Using uvx at: ${UVX_PATH}"

# ----- 6. Clear cached package (ensures the latest version is used) -------

log "Clearing any cached version of ${PACKAGE}…"
# Run with a timeout so a slow cache eviction never blocks the install.
# Errors are non-fatal — worst case the user runs the old cached version
# once and uvx auto-updates on the next Claude Desktop restart.
uv cache clean "${PACKAGE}" 2>/dev/null &
UV_CACHE_PID=$!
# Give it 10 seconds; kill if still running.
for i in $(seq 1 10); do
    kill -0 "${UV_CACHE_PID}" 2>/dev/null || break
    sleep 1
done
kill "${UV_CACHE_PID}" 2>/dev/null || true
wait "${UV_CACHE_PID}" 2>/dev/null || true
ok "Cache cleared."

# ----- 5. Write Claude Desktop config ------------------------------------

mkdir -p "${CLAUDE_CONFIG_DIR}"

# Backup any existing config once.
if [[ -f "${CLAUDE_CONFIG}" && ! -f "${CLAUDE_CONFIG}.bak" ]]; then
    cp "${CLAUDE_CONFIG}" "${CLAUDE_CONFIG}.bak"
    log "Backed up existing config to $(basename "${CLAUDE_CONFIG}").bak"
fi

# Merge or create using Python (always available on macOS).
# Explicitly export the shell vars into Python's environment — shell-local
# variables are not inherited into child processes by default.
log "Registering MCP server '${SERVER_NAME}' in Claude Desktop config…"
CLAUDE_CONFIG="${CLAUDE_CONFIG}" \
SERVER_NAME="${SERVER_NAME}" \
PACKAGE="${PACKAGE}" \
PYTHON_VERSION="${PYTHON_VERSION}" \
UVX_PATH="${UVX_PATH}" \
INSTANCE_CONNECTION_NAME="${INSTANCE_CONNECTION_NAME}" \
DB_NAME="${DB_NAME}" \
IP_TYPE="${IP_TYPE}" \
python3 - <<'PY'
import json, os
path = os.environ["CLAUDE_CONFIG"]
server_name = os.environ["SERVER_NAME"]
package = os.environ["PACKAGE"]
python_version = os.environ["PYTHON_VERSION"]
uvx_path = os.environ["UVX_PATH"]
entry = {
    # Use the absolute path to uvx. Claude Desktop launches with a stripped
    # PATH that typically excludes ~/.local/bin where uv installs its tools.
    "command": uvx_path,
    # --python pins the interpreter; @latest selects the newest published release.
    # Pinning to 3.12 avoids SSL compatibility issues in Python 3.13/3.14's
    # isolated uvx environment on macOS (cloud-sql-python-connector / aiohttp).
    "args": ["--python", python_version, f"{package}@latest"],
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

# ----- 7. Verify placeholders -------------------------------------------

if [[ "${INSTANCE_CONNECTION_NAME}" == *REPLACE_ME* || "${DB_NAME}" == "REPLACE_ME" ]]; then
    warn "This installer still has REPLACE_ME placeholders for GCP settings."
    warn "Ask the tool owner for the correct INSTANCE_CONNECTION_NAME and DB_NAME,"
    warn "then edit ${CLAUDE_CONFIG} and restart Claude Desktop."
    exit 0
fi

# ----- 8. Smoke test — confirm the package imports cleanly ---------------

log "Running smoke test (downloading package if needed, ~10 seconds first time)…"
if "${UVX_PATH}" --python "${PYTHON_VERSION}" "${PACKAGE}@latest" --help >/dev/null 2>&1; then
    ok "Smoke test passed — package installed and starts cleanly on Python ${PYTHON_VERSION}."
else
    # Non-fatal: the server may still work; Claude Desktop's stderr logs will
    # have the real error. Warn rather than die so the config is still written.
    warn "Smoke test failed. Check ~/Library/Logs/Claude/ after restarting Claude Desktop."
    warn "Common fix: run  uv cache clean ${PACKAGE}  then re-run this installer."
fi

log "Done. Restart Claude Desktop to pick up the new MCP server."
log "After restart, try asking Claude: \"List the schemas in gong-nl-db\"."
