# Registers the mt5bbot watchdog so the API and runtime self-heal without manual intervention.
#
#   .\register-watchdog.ps1 -BotDir C:\mt5bbot
#
# The watchdog runs every 5 minutes (and once at logon). It is idempotent and single-owner aware:
# it only ever starts the runtime through the API, never as a second process.
#
# Run as Administrator (Task Scheduler registration needs it).

param(
    [string]$BotDir = "C:\mt5bbot",
    [int]$ApiPort = 8000,
    [int]$EveryMinutes = 5,
    [string]$TaskName = "MT5-Watchdog"
)

$ErrorActionPreference = "Stop"
$watchdog = Join-Path $BotDir "deploy\windows\watchdog.ps1"
if (-not (Test-Path $watchdog)) { throw "watchdog not found at $watchdog" }

$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$watchdog`" " +
    "-BotDir `"$BotDir`" -ApiPort $ApiPort -StartRuntime"

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments `
    -WorkingDirectory $BotDir

# Two triggers: logon (covers an interactive restart) and a repeating 5-minute tick (covers a
# hung/dead API while the machine stays up).
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn
$repeatTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType S4U -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $action `
    -Trigger $logonTrigger, $repeatTrigger -Settings $settings -Principal $principal `
    -Description "mt5bbot self-heal: restart the API if unhealthy; start the loop via the API." `
    -Force | Out-Null

Write-Host "Registered '$TaskName' (every $EveryMinutes min + at logon)" -ForegroundColor Green
Write-Host "Verify:  schtasks /Query /TN $TaskName /V /FO LIST"
Write-Host "Run now: schtasks /Run /TN $TaskName"
Write-Host "Log:     $BotDir\logs\watchdog.log"
