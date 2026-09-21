# AWS Windows EC2 Runbook — MT5 worker (demo first)

One machine runs everything: FastAPI + TradingLoop + MT5 terminal + Exness demo.
The Vercel dashboard talks to it over HTTPS.

```
Vercel (React dashboard)
        │ HTTPS + Bearer API_TOKEN
        ▼
Windows EC2:  Caddy :443 ──► FastAPI :8000 ──► BotControlService/TradingLoop
        │                                        │
   MT5 terminal (Task Scheduler "MT5-Terminal") ◄┘
        ▼
      Exness DEMO
```

## 1. Provision
* EC2: Windows Server 2022, `t3.medium` (2 vCPU / 4 GB — MT5 needs 2 GB+), 30 GB gp3.
* Security group: inbound 443, 80 (Caddy), **and nothing else**. RDP 3389 restricted to YOUR IP.
* Elastic IP, then DNS: `api.YOURDOMAIN.com` → that IP.

## 2. Software (RDP in, run as Administrator)
1. Install Python 3.13 (x64): <https://python.org> — tick "Add to PATH".
2. Install Git: <https://git-scm.com/download/win>
3. Install Node.js LTS: <https://nodejs.org> (for frontend build)
4. Install MT5 terminal from Exness, log into the **DEMO** account once.
   Tools → Options → Expert Advisors → tick **Allow algorithmic trading**.
5. Clone and set up:
   ```powershell
   git clone https://github.com/zagzy8776/mt5bbot.git C:\mt5bbot
   cd C:\mt5bbot
   .\deploy\windows\install.ps1 -BotDir C:\mt5bbot -Symbol XAUUSD
   ```
6. Edit `C:\mt5bbot\.env` (minimum):
   ```
   EXECUTION_BACKEND=mt5
   MT5_LOGIN=<demo login>
   MT5_PASSWORD=<demo password>
   MT5_SERVER=<Exness-Demo server>
   TRADING_MODE=demo
   RISK_STATE_PATH=C:\mt5bbot\risk_state.json
   STORAGE_BACKEND=postgres
   DATABASE_URL=<set via environment variable, never in .env committed to git>
   API_TOKEN=<long random string>          # REQUIRED for non-loopback host
   API_HOST=0.0.0.0
   CORS_ORIGINS=https://your-dashboard.vercel.app
   VALIDATION_REPORT_PATH=C:\mt5bbot\validation_report.json
   INTELLIGENCE_ENABLED=false              # turn on later, behind a flag
   ```
   Exness symbols usually have a suffix (`XAUUSDm`, `XAUUSDm_mini`) — use exactly what Market Watch shows.

## 3. Verify the chain (demo)
```powershell
# broker truth: contract size, min lot, tick value, min equity per stop
.\.venv\Scripts\python.exe -m mt5_platform.runtime check --symbol XAUUSDm

# start the control plane + dashboard-driven runtime
.\.venv\Scripts\python.exe -m mt5_platform.main        # window 1
# then from the dashboard (or curl): POST /api/v1/runtime/start {"symbol":"XAUUSDm","timeframe":"M15"}
```
Watch one full cycle: candle closes → signal → sizing → risk → MT5 demo fill.

## 4. Autostart (survives reboots)
```powershell
.\deploy\windows\register-autostart.ps1 -BotDir C:\mt5bbot -Symbol XAUUSDm
shutdown /r /t 0
# after reboot:
Get-ScheduledTask | Where-Object TaskName -like 'MT5-*'
curl.exe https://api.YOURDOMAIN.com/health
# BotControlService auto-starts the trading loop after ~12 min via MT5-AutoStart task.
# To start/stop manually: POST /api/v1/runtime/start or /stop
```

## 5. HTTPS with Caddy
```powershell
# Caddy is installed at C:\caddy.exe
# Edit deploy/caddy/Caddyfile to set api.YOURDOMAIN.com
New-NetFirewallRule -DisplayName Caddy -Direction Inbound -Protocol TCP -LocalPort 80,443 -Action Allow
C:\caddy.exe run --config C:\mt5bbot\deploy\caddy\Caddyfile
```

## 6. Aiven PostgreSQL
```powershell
# Set DATABASE_URL as environment variable (NEVER in .env committed to git)
[System.Environment]::SetEnvironmentVariable('DATABASE_URL', 'postgres://avnadmin:PASS@HOST:PORT/defaultdb?sslmode=require', 'Machine')
# Or use a Windows scheduled task / secrets manager
```
Tables are auto-created on first API startup via `init_db()`.

## 7. Operations checklist
* **Backups**: nightly copy of `mt5_platform.db` + `risk_state.json` + `.env` (encrypted) to S3.
* **Monitoring**: poll `GET /health` from an external pinger; alert when `components.mt5 != up` or `/api/v1/runtime` state != `running` for 15 min.
* **Log rotation**: uvicorn logs to `C:\mt5bbot\logs\uvicorn.log`; audit events in PostgreSQL.
* **Kill switch**: dashboard button, or `POST /api/v1/risk/killswitch {"engaged": true}`. Persists via `risk_state.json` — survives restarts.
* **Never** put the real account on this box until Stage C validation evidence exists.

## 8. If something breaks
| Symptom | First check |
|---|---|
| `MT5NotAvailable` | MetaTrader5 package installed in the venv? 64-bit Python? Terminal installed and path set in `.env`? |
| `RealAccountBlocked` | Account is real-money but TRADING_MODE=demo — use demo, or complete the live gate |
| Task didn't start | `Get-ScheduledTaskInfo MT5-API` or `MT5-AutoStart` → LastTaskResult |
| API 401 from dashboard | API_TOKEN mismatch / CORS_ORIGINS missing dashboard URL |
| Bot `state: error` | `GET /api/v1/runtime` → last_error; audit log via API |
| IPC timeout | MT5 terminal must be running with "Allow algorithmic trading" enabled |
