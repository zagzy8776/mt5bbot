# One-shot setup for a fresh Windows EC2 instance (run as Administrator in PowerShell).
# Prereqs: Python 3.11+ on PATH, MT5 terminal installed at the default location or MT5_TERMINAL_PATH set.

param(
    [string]$BotDir = "C:\mt5bbot",
    [string]$Symbol = "XAUUSD",
    [string]$Timeframe = "M15"
)

$ErrorActionPreference = "Stop"
Write-Host "=== mt5bbot Windows setup ===" -ForegroundColor Cyan

# 1. Verify Python
try { $py = (python --version) } catch { throw "Python not on PATH. Install Python 3.11+ first." }
Write-Host "Python: $py"

# 2. Create venv and install the project + MT5 extra
Write-Host "Creating venv at $BotDir\.venv ..."
if (-not (Test-Path $BotDir)) { New-Item -ItemType Directory -Path $BotDir | Out-Null }
Push-Location $BotDir
python -m venv .venv
& "$BotDir\.venv\Scripts\python.exe" -m pip install --upgrade pip
& "$BotDir\.venv\Scripts\pip.exe" install -e ".[mt5,storage,dev]"
& "$BotDir\.venv\Scripts\pip.exe" install MetaTrader5

# 3. Copy .env template if absent
if (-not (Test-Path "$BotDir\.env")) {
    Copy-Item "$BotDir\.env.example" "$BotDir\.env"
    Write-Host "Created $BotDir\.env — EDIT IT NOW: MT5_LOGIN/PASSWORD/SERVER, EXECUTION_BACKEND=mt5, API_TOKEN." -ForegroundColor Yellow
}

# 4. Sanity check: runtime check against the broker (demo)
Write-Host "Running: python -m mt5_platform.runtime check --symbol $Symbol"
& "$BotDir\.venv\Scripts\python.exe" -m mt5_platform.runtime check --symbol $Symbol
Write-Host "=== Setup complete. Next: register-autostart.ps1 ===" -ForegroundColor Green
Pop-Location
