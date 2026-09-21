# Runbook: from zero to a demo bot (no prior VPS experience needed)

## The one fact that decides your hosting
The `MetaTrader5` Python package only works on **Windows**, next to an installed MT5 terminal.
Vercel, Fly.io, Render and Neon are Linux/serverless, so they **cannot run the trading part**.
Use them for what they are good at (the dashboard website, the database) and put the bot on a
Windows machine.

## Where the bot can run
| Option | Cost | Notes |
|---|---|---|
| Your own Windows PC/laptop | free | Best place to start. Must stay on and online while trading. |
| A rented Windows VPS | usually paid | A VPS is just a Windows computer in a data center you control over Remote Desktop. Free tiers (e.g. AWS) and broker-provided VPS offers exist but have conditions (card required, size limits, balance/volume thresholds). Check current terms; they change. MT5 is happiest with 2 GB+ RAM. |
| A cloud MT5 bridge (e.g. MetaApi) | free tier limited | Runs MT5 for you behind an API; would need a new adapter. |

## First run on Windows (demo account only)
1. Open a **demo** account with your broker; install the MT5 terminal; log in once; in
   Tools > Options > Expert Advisors tick "Allow algorithmic trading".
2. Install Python 3.11+, then: `git clone https://github.com/zagzy8776/mt5bbot.git`,
   `cd mt5bbot`, `pip install -e ".[dev]" MetaTrader5`.
3. Copy `.env.example` to `.env` and set `EXECUTION_BACKEND=mt5`, `MT5_LOGIN`, `MT5_PASSWORD`,
   `MT5_SERVER` (from the demo account), `RISK_STATE_PATH=./risk_state.json`.
4. `python -m mt5_platform.runtime check --symbol XAUUSD` shows your account type, the symbol's
   real contract size, and the minimum balance each stop distance needs. Some brokers add a
   suffix to symbol names (e.g. `XAUUSDm`); use the name shown in your MT5 Market Watch.
5. Backtest (see README), then `python -m mt5_platform.runtime run`.

## Never
Never paste passwords or API tokens into chats or commit them. `.env` is git-ignored; keep it that way.
