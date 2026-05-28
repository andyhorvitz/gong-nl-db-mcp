# gong-nl-db-mcp installer for Windows.
#
# Usage (the one-liner colleagues run — paste into PowerShell):
#   irm https://raw.githubusercontent.com/andyhorvitz/gong-nl-db-mcp/main/scripts/install.ps1 | iex
#
# What this does:
#   1. Confirms we're on Windows.
#   2. Ensures `uv` is installed (installs via astral.sh if missing).
#   3. Ensures `gcloud` is installed (auto-installs if missing) and ADC is set up.
#   4. Clears any cached old version of the package.
#   5. Writes an MCP server entry into Claude Desktop's config (pinned to
#      Python 3.12 for SSL compatibility, using the full path to uvx).
#   6. Runs a smoke test to confirm the package starts cleanly.
#   7. Tells the colleague to restart Claude Desktop.
#
# Re-running is safe: the script is idempotent.

#Requires -Version 5.1
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ----- Settings -----------------------------------------------------------
$INSTANCE_CONNECTION_NAME = if ($env:INSTANCE_CONNECTION_NAME) { $env:INSTANCE_CONNECTION_NAME } else { "planar-ray-494004-b8:us-central1:gong-nl-db" }
$DB_NAME                  = if ($env:DB_NAME)                  { $env:DB_NAME }                  else { "gong" }
$IP_TYPE                  = if ($env:IP_TYPE)                  { $env:IP_TYPE }                  else { "PUBLIC" }

$PACKAGE        = "gong-nl-db-mcp"
$PYTHON_VERSION = "3.12"
$SERVER_NAME    = "gong-nl-db"
$CLAUDE_CONFIG_DIR = Join-Path $env:APPDATA "Claude"
$CLAUDE_CONFIG     = Join-Path $CLAUDE_CONFIG_DIR "claude_desktop_config.json"

# ----- Helpers ------------------------------------------------------------

function Log  { param($msg) Write-Host "==> $msg" -ForegroundColor Blue }
function Ok   { param($msg) Write-Host "✓  $msg"  -ForegroundColor Green }
function Warn { param($msg) Write-Host "!! $msg"  -ForegroundColor Yellow }
function Die  {
    param($msg)
    Write-Host "✗  $msg" -ForegroundColor Red
    exit 1
}

# Refresh PATH in the current session after an installer modifies it.
function Refresh-Path {
    $machine = [System.Environment]::GetEnvironmentVariable("PATH", "Machine")
    $user    = [System.Environment]::GetEnvironmentVariable("PATH", "User")
    $env:PATH = "$machine;$user"
}

# ----- 1. Platform check --------------------------------------------------

if (-not $IsWindows -and $env:OS -notmatch "Windows") {
    Die "This installer supports Windows only. On macOS, run the install.sh script instead."
}

# ----- 2. uv --------------------------------------------------------------

$uvExe = $null
try { $uvExe = (Get-Command uv -ErrorAction Stop).Source } catch {}

if (-not $uvExe) {
    Log "Installing uv (Python package/tool runner)..."
    try {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    } catch {
        Die "Failed to install uv: $_"
    }
    Refresh-Path
    try { $uvExe = (Get-Command uv -ErrorAction Stop).Source } catch {}
    if (-not $uvExe) {
        # uv installs to %USERPROFILE%\.local\bin on Windows
        $candidate = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
        if (Test-Path $candidate) {
            $uvExe = $candidate
            $env:PATH = "$env:PATH;$(Split-Path $candidate)"
        } else {
            Die "uv install appeared to succeed but 'uv' is not on PATH. Open a new PowerShell window and re-run."
        }
    }
} else {
    $uvVersion = & $uvExe --version 2>&1
    Log "uv already installed ($uvVersion)."
}

# Derive uvx path from uv path (same directory).
$uvxExe = Join-Path (Split-Path $uvExe) "uvx.exe"
if (-not (Test-Path $uvxExe)) {
    # Fallback: try PATH
    try { $uvxExe = (Get-Command uvx -ErrorAction Stop).Source } catch {
        Die "Could not locate uvx.exe alongside uv at '$uvExe'. Try opening a new PowerShell window and re-running."
    }
}
Log "Using uvx at: $uvxExe"

# ----- 3. gcloud + ADC ----------------------------------------------------

$gcloudExe = $null
try { $gcloudExe = (Get-Command gcloud -ErrorAction Stop).Source } catch {}

if (-not $gcloudExe) {
    # Check the default install location Google's installer uses.
    $candidate = Join-Path $env:LOCALAPPDATA "Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
    if (Test-Path $candidate) {
        $gcloudExe = $candidate
        $env:PATH = "$env:PATH;$(Split-Path $candidate)"
        Log "Found existing gcloud at default install location."
    } else {
        Log "gcloud not found — downloading and installing Google Cloud SDK..."
        Log "(This is the official Google installer. ~500MB, takes about a minute.)"
        $installer = Join-Path $env:TEMP "GoogleCloudSDKInstaller.exe"
        try {
            Invoke-WebRequest -Uri "https://dl.google.com/dl/cloudsdk/channels/rapid/GoogleCloudSDKInstaller.exe" `
                              -OutFile $installer -UseBasicParsing
            # /S = silent, /allusers not used so no admin required.
            Start-Process -FilePath $installer -ArgumentList "/S" -Wait
            Remove-Item $installer -Force -ErrorAction SilentlyContinue
        } catch {
            Die "Failed to download or install Google Cloud SDK: $_"
        }
        Refresh-Path
        $candidate = Join-Path $env:LOCALAPPDATA "Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
        if (Test-Path $candidate) {
            $gcloudExe = $candidate
            $env:PATH = "$env:PATH;$(Split-Path $candidate)"
        } else {
            try { $gcloudExe = (Get-Command gcloud -ErrorAction Stop).Source } catch {
                Die "gcloud install appeared to succeed but 'gcloud' is not on PATH. Open a new PowerShell window and re-run."
            }
        }
        Log "gcloud installed. Open a new terminal later to use 'gcloud' outside this script."
    }
}

# Check ADC.
$adcOk = $false
try {
    $token = & $gcloudExe auth application-default print-access-token 2>&1
    if ($LASTEXITCODE -eq 0 -and $token -notmatch "ERROR") { $adcOk = $true }
} catch {}

if (-not $adcOk) {
    Log "Logging in with Application Default Credentials..."
    Log "A browser window will open. Use your @bairesdev.com Google account."
    & $gcloudExe auth application-default login
    if ($LASTEXITCODE -ne 0) { Die "gcloud ADC login failed." }
} else {
    Log "gcloud ADC already set up."
}

# Set the quota project so ADC requests are billed/attributed to the correct
# GCP project. Without this, gcloud auth application-default set-quota-project
# will fail with a serviceusage.services.use permissions error on first use.
Log "Setting ADC quota project..."
$project = $INSTANCE_CONNECTION_NAME.Split(":")[0]
try {
    & $gcloudExe auth application-default set-quota-project $project 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Ok "Quota project set."
    } else {
        Warn "Could not set quota project automatically."
        Warn "Run this manually after install: gcloud auth application-default set-quota-project $project"
    }
} catch {
    Warn "Could not set quota project: $_"
}

# ----- 4. Clear cached package --------------------------------------------

Log "Clearing any cached version of ${PACKAGE}..."
# Run with a timeout so a slow cache eviction never blocks the install.
$cacheJob = Start-Job -ScriptBlock {
    param($uvExe, $pkg)
    & $uvExe cache clean $pkg 2>&1
} -ArgumentList $uvExe, $PACKAGE
$completed = Wait-Job $cacheJob -Timeout 10
if (-not $completed) {
    Stop-Job $cacheJob -ErrorAction SilentlyContinue
}
Remove-Job $cacheJob -Force -ErrorAction SilentlyContinue
Ok "Cache cleared."

# ----- 5. Write Claude Desktop config ------------------------------------

if (-not (Test-Path $CLAUDE_CONFIG_DIR)) {
    New-Item -ItemType Directory -Path $CLAUDE_CONFIG_DIR -Force | Out-Null
}

# Backup any existing config once.
$backupPath = "$CLAUDE_CONFIG.bak"
if ((Test-Path $CLAUDE_CONFIG) -and (-not (Test-Path $backupPath))) {
    Copy-Item $CLAUDE_CONFIG $backupPath
    Log "Backed up existing config to claude_desktop_config.json.bak"
}

Log "Registering MCP server '$SERVER_NAME' in Claude Desktop config..."

# Read existing config or start fresh.
$cfg = @{ mcpServers = @{} }
if (Test-Path $CLAUDE_CONFIG) {
    try {
        $raw = Get-Content $CLAUDE_CONFIG -Raw -Encoding UTF8
        $parsed = $raw | ConvertFrom-Json
        # ConvertFrom-Json returns a PSCustomObject; convert to hashtable for easy mutation.
        $cfg = @{}
        $parsed.PSObject.Properties | ForEach-Object { $cfg[$_.Name] = $_.Value }
        if (-not $cfg.ContainsKey("mcpServers")) { $cfg["mcpServers"] = @{} }
        # Convert mcpServers PSCustomObject to hashtable too.
        $ms = @{}
        $cfg["mcpServers"].PSObject.Properties | ForEach-Object { $ms[$_.Name] = $_.Value }
        $cfg["mcpServers"] = $ms
    } catch {
        Warn "Could not parse existing config — will overwrite. Original backed up."
        $cfg = @{ mcpServers = @{} }
    }
}

# Build the new entry.
# Use the absolute path to uvx so Claude Desktop doesn't need it on its PATH.
$entry = [ordered]@{
    command = $uvxExe
    args    = @("--python", $PYTHON_VERSION, "${PACKAGE}@latest")
    env     = [ordered]@{
        INSTANCE_CONNECTION_NAME = $INSTANCE_CONNECTION_NAME
        DB_NAME                  = $DB_NAME
        IP_TYPE                  = $IP_TYPE
    }
}

$cfg["mcpServers"][$SERVER_NAME] = $entry

# Serialize. Depth 10 ensures nested objects aren't truncated.
# -Compress is NOT used so the file stays human-readable.
$json = $cfg | ConvertTo-Json -Depth 10
Set-Content -Path $CLAUDE_CONFIG -Value $json -Encoding UTF8
Ok "Wrote $CLAUDE_CONFIG"

# ----- 6. Verify placeholders -------------------------------------------

if ($INSTANCE_CONNECTION_NAME -match "REPLACE_ME" -or $DB_NAME -eq "REPLACE_ME") {
    Warn "This installer still has REPLACE_ME placeholders for GCP settings."
    Warn "Ask the tool owner for the correct INSTANCE_CONNECTION_NAME and DB_NAME,"
    Warn "then edit $CLAUDE_CONFIG and restart Claude Desktop."
    exit 0
}

# ----- 7. Smoke test ------------------------------------------------------

Log "Running smoke test (downloading package if needed, ~30 seconds first time)..."
try {
    $smokeOutput = & $uvxExe --python $PYTHON_VERSION "${PACKAGE}@latest" --help 2>&1
    if ($LASTEXITCODE -eq 0) {
        Ok "Smoke test passed — package installed and starts cleanly on Python $PYTHON_VERSION."
    } else {
        Warn "Smoke test returned exit code $LASTEXITCODE."
        Warn "Check $env:APPDATA\Claude\logs\ after restarting Claude Desktop."
        Warn "Common fix: close this window, open a new PowerShell, and re-run the installer."
    }
} catch {
    Warn "Smoke test failed: $_"
    Warn "The config has been written — try restarting Claude Desktop anyway."
}

# ----- Done ---------------------------------------------------------------

Write-Host ""
Log "Done. Restart Claude Desktop to pick up the new MCP server."
Log "After restart, try asking Claude: `"List the schemas in gong-nl-db.`""
Write-Host ""
