# Registers Windows Task Scheduler jobs so a reboot self-heals:
#   task 1: start MT5 terminal at boot
#   task 2: start the FastAPI control plane (BotControlService is the single
#           authoritative runtime owner — it owns the TradingLoop via the API)
#   task 3: auto-start the trading loop via POST /api/v1/runtime/start
#
# Run as Administrator:  .\register-autostart.ps1 -BotDir C:\mt5bbot
#
# WHY ONE OWNER: the old script ran a standalone "python -m mt5_platform.runtime run"
# that competed with the API's BotControlService.  Now only the API owns the loop.
# The dashboard (or the startup task below) triggers it via POST.

param(
    [string]$BotDir = "C:\mt5bbot",
    [string]$Symbol = "XAUUSD",
    [string]$Timeframe = "M15",
    [string]$MT5Exe = "$env:ProgramFiles\MetaTrader 5\terminal64.exe",
    [int]$ApiPort = 8000
)

$ErrorActionPreference = "Stop"
$python = "$BotDir\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "venv python not found at $python — run install.ps1 first." }

# --- Task 1: MT5 terminal at boot -------------------------------------------
$mt5Action = New-ScheduledTaskAction -Execute $MT5Exe -Argument "/portable"
$bootTrigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
Register-ScheduledTask -TaskName "MT5-Terminal" -Action $mt5Action -Trigger $bootTrigger `
    -Settings $settings -User "SYSTEM" -RunLevel Highest -Force | Out-Null
Write-Host "Registered task: MT5-Terminal ($MT5Exe)"

# --- Task 2: FastAPI control plane (auto-restart every 5 min if dead) --------
# This creates BotControlService which is the single authoritative runtime owner.
$apiCmd = "-m mt5_platform.main"
$apiAction = New-ScheduledTaskAction -Execute $python -Argument $apiCmd -WorkingDirectory $BotDir
$apiTrigger = New-ScheduledTaskTrigger -AtStartup
$apiTrigger.Delay = "PT2M"  # let MT5 terminal start first
$apiSettings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 5) -StartWhenAvailable
Register-ScheduledTask -TaskName "MT5-API" -Action $apiAction -Trigger $apiTrigger `
    -Settings $apiSettings -User "SYSTEM" -RunLevel Highest -Force | Out-Null
Write-Host "Registered task: MT5-API (BotControlService owns the runtime loop)"

# --- Task 3: auto-start trading loop via API (waits for MT5 + API) ----------
# Curls POST /api/v1/runtime/start after a delay.  The API token is read from
# .env by the helper script below so it never appears in the task definition.
$startScript = @"
`$env:BOTDIR = '$BotDir'
. "`$BotDir\.env" 2>`$null
`$body = @{symbol='$Symbol';timeframe='$Timeframe'} | ConvertTo-Json
try {
    `$headers = @{}
    if (`$env:API_TOKEN) { `$headers['Authorization'] = 'Bearer ' + `$env:API_TOKEN }
    Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:$ApiPort/api/v1/runtime/start' `
        -Body `$body -ContentType 'application/json' -Headers `$headers -ErrorAction Stop
    Write-Host 'Trading loop started via API.'
} catch {
    Write-Host "Could not start trading loop: `$_" -ForegroundColor Yellow
}
"@
$startScriptPath = Join-Path $BotDir "start-trading.ps1"
$startScript | Out-File -FilePath $startScriptPath -Encoding UTF8 -Force

$autoAction = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-ExecutionPolicy Bypass -File `"$startScriptPath`"" -WorkingDirectory $BotDir
$autoTrigger = New-ScheduledTaskTrigger -AtStartup
$autoTrigger.Delay = "PT12M"  # 2 min for API + 10 min for MT5 login
$autoSettings = New-ScheduledTaskSettingsSet -RestartCount 1 -StartWhenAvailable
Register-ScheduledTask -TaskName "MT5-AutoStart" -Action $autoAction -Trigger $autoTrigger `
    -Settings $autoSettings -User "SYSTEM" -RunLevel Highest -Force | Out-Null
Write-Host "Registered task: MT5-AutoStart (POST /api/v1/runtime/start after 12 min)"

Write-Host "=== Autostart registered. Reboot to test: shutdown /r /t 0 ===" -ForegroundColor Green
Write-Host "Verify after reboot: Get-ScheduledTask | Where TaskName -like 'MT5-*'"
