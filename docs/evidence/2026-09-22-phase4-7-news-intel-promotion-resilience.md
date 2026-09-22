# Phases 4–7: news ingestion, web intelligence, promotion bridge, operational resilience

Date: 2026-09-22
Scope: Phases 4–7 of the roadmap, on top of Phases 1–3 (`f79b114`, `13e62f5`, `f0b89be`).
Mode: demo only. No gate, limit, credential or mode was relaxed. Nothing was promoted automatically.

## What was built

### Phase 4 — news / macro calendar (`src/mt5_platform/ingestion/news.py`)
- `NewsEvent` model + `news_events` table (unique `dedup_key`) with upsert in both stores.
- Providers: `FileNewsProvider` (operator-maintained JSON, no network), `HttpNewsProvider`
  (mandatory allow-list, timeout, byte cap, no redirects), `StaticNewsProvider` (tests/manual).
- `NewsIngestor`: fetch → normalise → dedup → store. Never raises into the loop; failures land in
  `stats.news` and one `news_ingest_failed` audit event.
- `blackout_state()` + `currencies_for_symbol()` (handles broker suffixes like `XAUUSDm`).
- `RiskContext.news_blackout` → a new risk reason `news_blackout`, active **only** when
  `NEWS_BLACKOUT_ENABLED=true` (default false). It refuses *new* entries; open positions are
  untouched by it and an unknown instrument is never blocked by a guess.
- Verified at runtime: this MetaTrader5 build exposes **no** calendar API
  (`[a for a in dir(MetaTrader5) if 'calendar' in a] == []`), so the terminal is not used as a source.

### Phase 5 — autonomous web research (`src/mt5_platform/intelligence/web.py`)
- `html_to_text`, `extract_title`, `content_hash`; `StaticFetcher` and `HttpFetcher`.
- `HttpFetcher` streams and stops at the byte cap when the client supports streaming (so a huge page
  is never buffered), falls back to slice-after-read otherwise; an empty allow-list refuses every URL.
- `WebIntelCollector`: per-source cap, per-page cap, total byte budget, dedup by content hash,
  per-note provenance (URL, domain, fetch time, status, bytes, truncation, extractor version).
- `research_notes` table; `propose_research_questions` returns `ResearchQuestion` objects with
  `proposed_change_type=None`, `evidence_quality="insufficient"` and `applies_to_config=False`.
- CLI `scripts/run_web_intel.py`. Off by default; the layer has no file/env/write path at all
  (asserted by a test that scans the module source).

### Phase 6 — research → runtime promotion (`src/mt5_platform/research/promotion.py`)
- `find_candidates` / `eligible_candidates`: eligibility is the report's **own** verdict
  (`validation_passed` and no `rejection_reasons`) plus symbol/timeframe match and the parameters
  actually constructing a registered strategy. No new gates, no lowered gates.
- `approve_proposal`: refuses ineligible candidates and anonymous approvals, writes a versioned
  approval entry (`version_id`, `approved_by`, `approved_at`, metrics, gates, report sha256).
- `build_promoted_strategies`: instantiates approved strategies and stamps `strategy.version` with
  the promotion `version_id`; the loop now falls back to the strategy's version when attributing an
  outcome, so Phase 1's ledger and Phase 6's approvals are connected end to end.
- `build_signal_engine` uses the promotion file only when `PROMOTION_CONFIG_PATH` points at it;
  absent/empty/corrupt files fall back to `STRATEGIES` unchanged.
- `CandidateReport.strategy` added (factory key) so future reports do not need label resolution.
- CLI `scripts/promote_candidate.py list|approve|show`.

### Phase 7 — resilience + strategy families
- `deploy/windows/watchdog.ps1` + `register-watchdog.ps1`: logon + every-5-minute task that probes
  `/health`, restarts the API when it is down, and starts the **runtime only through the API**
  (`POST /api/v1/runtime/start`) so a second loop can never be spawned. Token read from `.env` at
  run time; log at `logs/watchdog.log`; exit codes 0/1/2.
- Seven new families in `strategy/families.py` (+ pure helpers in `strategy/indicators.py`):
  `atr_breakout`, `ema_adx_trend`, `bollinger_reversion`, `rsi_ema_pullback`, `session_breakout`,
  `mtf_trend`, `structure_breakout`; all registered and added as research candidates.
- `bar_to_event` now carries the bar's OHLC in `event.metadata`, so range-based families see the same
  bars in backtest and live (tick-only events keep the old behaviour).

## Evidence

- `pytest -q` → **637 passed** (44 new across `test_news_ingestion.py`, `test_web_intel.py`,
  `test_promotion.py`, `test_strategy_families.py`).
- `ruff check .` → clean.
- Watchdog, run manually and then as the scheduled task (both against the live API):
  `API healthy (status=up)` then `runtime already running -- no action`, exit 0, python process
  count unchanged at 2 (no duplicate owner).
- `schtasks /Query /TN MT5-Watchdog /V /FO LIST` → Enabled, Run As `Administrator`, triggers
  `MSFT_TaskLogonTrigger` + `MSFT_TaskTimeTrigger interval=PT5M duration=P3650D`, run level Highest.
- Promotion CLI against the committed report: 17 candidates, 7 eligible
  (`python scripts/promote_candidate.py list --eligible-only`); nothing was approved.
- Families on real data (one pass each, `data/xauusd_m15.csv`, 23,627 bars, 0.26 spread,
  0.05 slippage, 1% risk): `ema_adx_trend` 111 trades PF 1.31 (42.3% win), `structure_breakout`
  87 PF 1.23 (40.2%), `rsi_ema_pullback` 175 PF 1.04 (36.0%), `session_breakout` 86 PF 1.04 (50.0%),
  `mtf_trend` 87 PF 0.97, `atr_breakout` 17 PF 0.53, `bollinger_reversion` 34 PF 0.49.
  Single-pass, one symbol, one year: a research starting point, **not** evidence of edge.

## Non-negotiables held

- Demo only; `LIVE_TRADING_*` untouched; no gate, limit or threshold changed.
- The risk engine remains authoritative; the news blackout is an added, opt-in restriction.
- No automatic configuration change from research, learning or news: promotion requires a named
  human approver, and research output cannot carry a configuration change at all.
- No secrets added to task definitions, logs or files; `.env` stays untracked.
