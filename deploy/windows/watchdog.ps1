# Self-healing watchdog for the mt5bbot control plane (Phase 7 operational resilience).
#
# Run every few minutes by Task Scheduler (see register-watchdog.ps1). It is deliberately
# conservative:
#   * it never starts a second runtime owner -- the API (BotControlService) owns the loop, so the
#     runtime is only ever (re)started through POST /api/v1/runtime/start on the API;
#   * if the API is healthy and the loop is already running it does nothing at all;
#   * the API token is read from .env at run time and is never written into a task definition,
#     printed, or logged.
#
# Exit codes: 0 = healthy/no action needed, 1 = API still down after a restart attempt,
#             2 = environment is broken (no venv python).

param(
    [string]$BotDir = "C:\mt5bbot",
    [int]$ApiPort = 8000,
    [switch]$StartRuntime,
    [int]$StartupWaitSeconds = 20
)

$ErrorActionPreference = "Continue"
$logDir = Join-Path $BotDir "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir "watchdog.log"

function Write-Log([string]$Message) {
    "$(Get-Date -Format o) $Message" | Add-Content -Path $logFile -Encoding UTF8
}

function Get-ApiToken([string]$Path) {
    if (-not (Test-Path $Path)) { return $null }
    foreach ($line in Get-Content $Path) {
        if ($line -match '^\s*API_TOKEN\s*=\s*(.+?)\s*$') {
            return $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
    return $null
}

$token = Get-ApiToken (Join-Path $BotDir ".env")
$headers = @{}
if ($token) { $headers['Authorization'] = "Bearer $token" }
$base = "http://127.0.0.1:$ApiPort"

function Test-ApiHealth {
    try {
        return Invoke-RestMethod -Method Get -Uri "$base/health" -Headers $headers -TimeoutSec 5 `
            -ErrorAction Stop
    } catch {
        return $null
    }
}

$health = Test-ApiHealth
if (-not $health) {
    Write-Log "API not healthy at $base -- attempting restart"
    $python = Join-Path $BotDir ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) {
        Write-Log "FATAL: interpreter missing at $python"
        exit 2
    }
    # The API binds the port itself; if another instance won the race it fails to bind and exits,
    # so this can never leave two listeners.
    Start-Process -FilePath $python -ArgumentList "-m", "mt5_platform.main" `
        -WorkingDirectory $BotDir -WindowStyle Hidden
    Start-Sleep -Seconds $StartupWaitSeconds
    $health = Test-ApiHealth
    if (-not $health) {
        Write-Log "API still unhealthy after restart attempt"
        exit 1
    }
    Write-Log "API recovered (status=$($health.status))"
} else {
    Write-Log "API healthy (status=$($health.status))"
}

if ($StartRuntime) {
    try {
        $runtime = Invoke-RestMethod -Method Get -Uri "$base/api/v1/runtime" -Headers $headers `
            -TimeoutSec 5 -ErrorAction Stop
        $state = "$($runtime.state)"
        if ($state -eq "running") {
            Write-Log "runtime already running -- no action"
        } else {
            $body = @{}
            if ($runtime.symbol) { $body['symbol'] = $runtime.symbol }
            if ($runtime.timeframe) { $body['timeframe'] = $runtime.timeframe }
            Invoke-RestMethod -Method Post -Uri "$base/api/v1/runtime/start" -Headers $headers `
                -Body ($body | ConvertTo-Json) -ContentType "application/json" `
                -TimeoutSec 15 -ErrorAction Stop | Out-Null
            Write-Log "runtime was '$state' -- start requested through the API"
        }
    } catch {
        Write-Log "runtime check failed: $($_.Exception.Message)"
    }
}

exit 0
