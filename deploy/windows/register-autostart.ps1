# Registers Windows Task Scheduler jobs so a reboot self-heals:
#   task 1: start MT5 terminal at boot
#   task 2: start the bot runtime (waits for MT5) at boot
# Run as Administrator:  .\register-autostart.ps1 -BotDir C:\mt5bbot

param(
    [string]$BotDir = "C:\mt5bbot",
    [string]$Symbol = "XAUUSD",
    [string]$Timeframe = "M15",
    [string]$MT5Exe = "$env:ProgramFiles\MetaTrader 5\terminal64.exe"
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

# --- Task 2: bot runtime after MT5 (10 min delay covers terminal login) ------
$botCmd = "-m mt5_platform.runtime run --symbol $Symbol --timeframe $Timeframe"
$botAction = New-ScheduledTaskAction -Execute $python -Argument $botCmd -WorkingDirectory $BotDir
$botTrigger = New-ScheduledTaskTrigger -AtStartup
$botTrigger.Delay = "PT10M"  # let MT5 terminal come up and log in first
Register-ScheduledTask -TaskName "MT5-Bot-Runtime" -Action $botAction -Trigger $botTrigger `
    -Settings $settings -User "SYSTEM" -RunLevel Highest -Force | Out-Null
Write-Host "Registered task: MT5-Bot-Runtime (10 min boot delay)"

# --- Task 3: FastAPI control plane (auto-restart every 5 min if dead) --------
$apiCmd = "-m mt5_platform.main"
$apiAction = New-ScheduledTaskAction -Execute $python -Argument $apiCmd -WorkingDirectory $BotDir
$apiTrigger = New-ScheduledTaskTrigger -AtStartup
$apiTrigger.Delay = "PT2M"
$apiSettings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 5) -StartWhenAvailable
Register-ScheduledTask -TaskName "MT5-API" -Action $apiAction -Trigger $apiTrigger `
    -Settings $apiSettings -User "SYSTEM" -RunLevel Highest -Force | Out-Null
Write-Host "Registered task: MT5-API"

Write-Host "=== Autostart registered. Reboot to test: shutdown /r /t 0 ===" -ForegroundColor Green
Write-Host "Verify after reboot: Get-ScheduledTask | Where TaskName -like 'MT5-*'"
