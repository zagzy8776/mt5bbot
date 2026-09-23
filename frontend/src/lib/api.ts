export type ApiError = {
  detail?: string;
};

const API_BASE = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");

let token = sessionStorage.getItem("mt5_api_token") || "";

export function getToken() {
  return token;
}

export function setToken(next: string) {
  token = next.trim();
  if (token) sessionStorage.setItem("mt5_api_token", token);
  else sessionStorage.removeItem("mt5_api_token");
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    if (msg.includes("Failed to fetch") || msg.includes("NetworkError")) {
      throw new Error(`Cannot reach API at ${API_BASE} — check URL and CORS settings.`);
    }
    throw new Error(msg);
  }
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    if (response.status === 401) {
      detail = "Unauthorized — paste your API token in Settings.";
    }
    try {
      const body = (await response.json()) as ApiError;
      if (body.detail) detail = body.detail;
    } catch {
      // keep default message
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

export type StrategyDiagnostics = {
  evaluations: number;
  signals: number;
  rejections: number;
  replay_evaluations: number;
  replay_signals: number;
  replay_rejections: number;
  strategy_errors: number;
  reject_reasons: Record<string, number>;
};

export type StrategyValidationStatus =
  | "promotable"
  | "rejected"
  | "tested_not_surviving"
  | "unvalidated";

export type StrategyRow = {
  name: string;
  description?: string;
  version?: string;
  enabled: boolean;
  symbols?: string[] | null;
  parameters?: Record<string, unknown>;
  loaded: boolean;
  validation_status: StrategyValidationStatus;
  validated: boolean;
  raw_gate_passed: boolean;
  last_p_value: number | null;
  research_family: string;
  locked: boolean;
  lock_reason: string;
};

export type StrategyPanel = {
  strategies: StrategyRow[];
  configured: string[];
  validated_count: number;
  enabled_count: number;
  note: string;
};

export type OrderStats = {
  total: number;
  orders_by_status: Record<string, number>;
  stats: {
    created?: number;
    submitted?: number;
    filled?: number;
    partially_filled?: number;
    broker_rejected?: number;
    risk_rejected?: number;
    cancelled?: number;
    failed?: number;
    invalid_transitions?: number;
    reconciliation_mismatches?: number;
  };
};

export type Candle = {
  timestamp: string;
  symbol: string;
  timeframe: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
  tick_count?: number;
  spread_avg?: number;
};

export type SignalEngineDiagnostics = {
  evaluations: number;
  events_processed: number;
  signals_generated: number;
  signals_rejected: number;
  replay_evaluations: number;
  replay_signals_generated: number;
  replay_signals_rejected: number;
  replay_persisted: boolean;
  reject_reasons: Record<string, number>;
  last_evaluation_time: string | null;
  last_evaluation_symbol: string | null;
  last_signal_time: string | null;
  last_signal_strategy: string | null;
  last_signal_side: string | null;
  last_signal_reason: string | null;
  last_signal_entry: number | null;
  last_signal_stop_loss: number | null;
  last_signal_take_profit: number | null;
  last_rejection: {
    at: string;
    strategy: string;
    side: string;
    reasons: string[];
    signal_id: string;
  } | null;
  total_strategies: number;
  active_strategies: number;
  strategy_stats: Record<string, StrategyDiagnostics>;
};

export type PipelineTrace = {
  cycle?: number;
  stage?: string;
  at?: string;
  symbol?: string | null;
  bar_time?: string;
  last_processed_bar?: string | null;
  waiting?: boolean;
  reasons?: string[];
  status?: string;
  order_id?: string;
  side?: string;
  strategy?: string;
  stop_loss?: number | null;
  take_profit?: number | null;
  volume?: number;
  rejection_reason?: string | null;
  signal?: { strategy?: string; side?: string; reason?: string; entry?: number | null };
};

export type RuntimeStats = {
  cycles?: number;
  bars_processed?: number;
  signals?: number;
  orders_sent?: number;
  errors?: number;
  consecutive_errors?: number;
  skipped?: Record<string, number>;
  pipeline_stage?: string;
  pipeline?: PipelineTrace;
  warmup_replay?: {
    bars: number;
    signals: number;
    symbols?: Record<string, { bars: number; signals_discarded: number }>;
  };
  signal_engine?: SignalEngineDiagnostics;
  execution?: ExecutionAvailability;
  position_management?: {
    last?: {
      at?: string;
      outcome?: string;
      ticket?: string | null;
      action?: string | null;
      reason?: string;
      entries_enabled?: boolean;
    };
    counts?: Record<string, number>;
    entries_enabled?: boolean;
  };
  intelligence?: {
    contexts_built?: number;
    theses_emitted?: number;
    theses_no_trade?: number;
    signals_from_thesis?: number;
    position_evaluations?: number;
    position_exits?: number;
    learning_records?: number;
    errors?: number;
  };
  outcomes?: OutcomeSummary & { recorder?: Record<string, unknown> };
  learning?: LearningStats;
  evidence?: EvidenceStatus;
};

export type Runtime = {
  state: string;
  symbol: string;
  timeframe: string;
  started_at: string | null;
  last_error: string | null;
  connected: boolean;
  kill_switch: boolean;
  stats: RuntimeStats;
};

export type Account = {
  balance: number;
  equity: number;
  free_margin: number;
  used_margin: number;
  margin_level: number | null;
  floating_pnl: number;
  daily_pnl: number;
  drawdown_pct: number;
  open_positions: number;
  exposure: number;
};

export type Position = {
  ticket: string;
  order_id: string | null;
  symbol: string;
  side: "buy" | "sell";
  volume: number;
  entry_price: number;
  current_price: number;
  floating_pnl: number;
  stop_loss: number | null;
  take_profit: number | null;
  opened_at: string;
  magic: number | null;
  comment: string;
  is_external: boolean;
};

export type Positions = {
  positions: Position[];
  manual_position_policy: string;
};

export type Order = {
  order_id: string;
  symbol: string;
  side: "buy" | "sell";
  volume: number;
  entry: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  status: string;
  created_at: string;
  rejection_reason?: string | null;
};

export type ExecutionAvailability = {
  terminal?: {
    available?: boolean;
    trade_allowed?: boolean;
    connected?: boolean;
    tradeapi_disabled?: boolean;
    build?: number;
    reason?: string;
    expected_retcode?: string | null;
    expected_retcode_code?: number | null;
    expected_comment?: string | null;
  };
  blocked?: boolean;
  block?: {
    reason?: string;
    since?: string;
    attempts_while_blocked?: number;
    expected_retcode?: string | null;
    expected_retcode_code?: number | null;
    expected_comment?: string | null;
  } | null;
};

export type Execution = {
  execution_id: string;
  order_id: string;
  timestamp: string;
  symbol: string;
  side: "buy" | "sell";
  requested_volume: number;
  requested_price?: number | null;
  stop_loss?: number | null;
  take_profit?: number | null;
  execution_price?: number | null;
  rejection_reason?: string | null;
  final_status: string;
  mt5_response?: {
    retcode?: string;
    retcode_code?: number;
    comment?: string;
    transmitted?: boolean;
    local_reject?: string;
    diagnostics?: {
      warnings?: string[];
      [key: string]: unknown;
    };
    [key: string]: unknown;
  };
};

export type Signal = {
  signal_id: string;
  symbol: string;
  direction: "buy" | "sell";
  entry: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  confidence: number;
  reason: string;
  timestamp: string;
  strategy_name: string;
};

export type AuditEvent = {
  timestamp: string;
  component: string;
  event_type: string;
  severity: string;
  symbol: string | null;
  correlation_id: string;
  payload: Record<string, unknown>;
};

export type Risk = {
  kill_switch: boolean;
  paused: boolean;
  halt_reasons: string[];
  pause_reasons: string[];
  settings: Record<string, number | boolean>;
  stats: Record<string, unknown>;
};

export type OutcomeSummary = {
  open?: number;
  completed?: number;
  autonomous?: number;
  external?: number;
  backtest?: number;
  by_exit_cause?: Record<string, number>;
  mae_mfe_available?: number;
  r_multiple_available?: number;
  realized_pnl_available?: number;
  with_partial_exits?: number;
  last_completed?: {
    trade_id?: string;
    ticket?: string | null;
    symbol?: string;
    strategy?: string;
    source?: string;
    exit_cause?: string;
    exit_cause_source?: string;
    realized_pnl?: number;
    r_multiple?: number | null;
    exit_time?: string | null;
  } | null;
  recorder?: Record<string, unknown>;
};

export type LearningStats = {
  reviews?: number;
  lessons_proposed?: number;
  memory_records?: number;
  hypotheses?: number;
  errors?: number;
  last_error?: string | null;
};

export type EvidenceStatus = {
  instrument?: string;
  available_outcomes?: number;
  usable_outcomes?: number;
  open_outcomes?: number;
  sample_size?: number;
  evidence_quality?: string;
  minimum_sample_required?: number;
  min_sample_weak?: number;
  min_sample_moderate?: number;
  min_sample_strong?: number;
  sources?: Record<string, number>;
  backtest_excluded?: boolean;
  thresholds_lowered?: boolean;
};

export type RecordedOutcome = {
  trade_id: string;
  status: string;
  source: string;
  instrument: string;
  timeframe?: string;
  strategy?: string;
  side: string;
  entry: number;
  exit_price: number | null;
  exit_time: string | null;
  exit_cause: string;
  exit_cause_source?: string;
  realized_pnl: number;
  r_multiple: number | null;
  mae: number;
  mfe: number;
  duration_s: number;
  broker_ticket: string | null;
  evidence?: Record<string, unknown>;
};

export type OutcomesPayload = {
  count: number;
  summary: OutcomeSummary;
  learning: LearningStats;
  evidence: EvidenceStatus;
  outcomes: RecordedOutcome[];
};

export type ResearchSensitivity = { alpha?: number; survivors?: number };

export type ResearchForwardRow = {
  strategy?: string;
  trades?: number;
  grade?: string;
  expectancy_r?: number;
  win_rate?: number;
  verdict?: string;
};

export type ResearchStatus = {
  report: { available: boolean; generated_at?: string | null; path?: string };
  candidates_searched: number;
  raw_gate_survivors: number;
  multiplicity_survivors: number;
  promotable: number;
  primary_correction: { method?: string | null; alpha?: number | null };
  sensitivity: Record<string, ResearchSensitivity>;
  conclusion: {
    verdict?: string | null;
    interpretation?: string | null;
    alpha_rank1?: number | null;
  };
  power: {
    median_n_effective?: number | null;
    smallest_detectable_edge_r?: number | null;
    trades_needed_for_0_05r_edge?: number | null;
    "trades_needed_for_0.05r_edge"?: number | null;
  };
  holdout: {
    sealed: boolean | null;
    bars: number;
    window?: string;
    confirmation_records: number;
    last_confirmed_at?: string | null;
  };
  family: { family_id?: string; hypothesis_version?: string; candidates?: number };
  manifest: {
    available: boolean;
    code_commit?: string;
    dataset_sha256?: string;
    built_at?: string | null;
  };
  forward: { strategy: ResearchForwardRow[]; coverage: Record<string, unknown> };
  live_trading_enabled: boolean;
  trading_mode?: string;
  runtime_state?: string;
};

export type RuntimeHeartbeat = {
  observed_at: string;
  read_only: boolean;
  symbol: string;
  timeframe: string;
  state: string;
  connected?: boolean | null;
  started_at?: string | null;
  cycle?: number | null;
  cycles?: number | null;
  bars_processed?: number | null;
  signals?: number | null;
  orders_sent?: number | null;
  errors?: number | null;
  consecutive_errors?: number | null;
  pipeline_stage?: string;
  last_processed_bar?: string | null;
  last_processed_bar_close?: string | null;
  latest_closed_bar_open?: string | null;
  latest_closed_bar_close?: string | null;
  bar_convention?: string;
  freshness_floor?: string | null;
  bar_age_seconds?: number | null;
  bar_open_age_seconds?: number | null;
  lag_bars?: number | null;
  data_fresh?: boolean | null;
  waiting?: boolean | null;
  poll_s?: number | null;
  signal_engine: {
    evaluations?: number | null;
    generated?: number | null;
    rejected?: number | null;
    events_processed?: number | null;
    strategy_errors?: number | null;
    last_evaluation_time?: string | null;
    last_rejection?: unknown;
  };
  risk: {
    checks?: number | null;
    approved?: number | null;
    rejected?: number | null;
    reject_reasons?: Record<string, number>;
  };
  execution: { blocked?: boolean | null; trade_allowed?: boolean | null; reason?: string };
  assessment: string;
  notes: string[];
};

export const api = {
  runtime: () => request<Runtime>("/api/v1/runtime"),
  start: (symbol: string, timeframe: string) =>
    request<Runtime>("/api/v1/runtime/start", {
      method: "POST",
      body: JSON.stringify({ symbol, timeframe })
    }),
  stop: () => request<Runtime>("/api/v1/runtime/stop", { method: "POST" }),
  restart: () => request<Runtime>("/api/v1/runtime/restart", { method: "POST" }),
  account: () => request<Account>("/api/v1/account"),
  positions: () => request<Positions>("/api/v1/positions"),
  audit: (limit = 50) => request<{ events: AuditEvent[] }>(`/api/v1/audit/recent?limit=${limit}`),
  orders: async () => (await request<{ orders: Order[] }>("/api/v1/orders?limit=25")).orders,
  executions: async () =>
    (await request<{ executions: Execution[] }>("/api/v1/executions?limit=25")).executions,
  signals: async () => (await request<{ signals: Signal[] }>("/api/v1/signals?limit=25")).signals,
  risk: () => request<Risk>("/api/v1/risk/status"),
  outcomes: (limit = 25) => request<OutcomesPayload>(`/api/v1/outcomes?limit=${limit}`),
  status: () => request<Record<string, unknown>>("/api/v1/status"),
  heartbeat: () => request<RuntimeHeartbeat>("/api/v1/runtime/heartbeat"),
  strategyPanel: () => request<StrategyPanel>("/api/v1/strategies/effective"),
  orderStats: () => request<OrderStats>("/api/v1/orders/stats"),
  enableStrategy: (name: string) =>
    request<{ name: string; enabled: boolean }>(
      `/api/v1/strategies/${encodeURIComponent(name)}/enable`,
      { method: "POST" }
    ),
  disableStrategy: (name: string) =>
    request<{ name: string; enabled: boolean }>(
      `/api/v1/strategies/${encodeURIComponent(name)}/disable`,
      { method: "POST" }
    ),
  researchStatus: () => request<ResearchStatus>("/api/v1/research/status"),
  quote: (symbol: string) => request<{ symbol: string; bid: number; ask: number; spread_points: number }>(
    `/api/v1/market/quote?symbol=${encodeURIComponent(symbol)}`
  ),
  kill: (engaged: boolean, reason = "dashboard") =>
    request<Record<string, unknown>>("/api/v1/risk/killswitch", {
      method: "POST",
      body: JSON.stringify({ engaged, reason })
    })
};
