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
  strategy_errors: number;
  reject_reasons: Record<string, number>;
};

export type SignalEngineDiagnostics = {
  evaluations: number;
  events_processed: number;
  signals_generated: number;
  signals_rejected: number;
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
  signals: async () => (await request<{ signals: Signal[] }>("/api/v1/signals?limit=25")).signals,
  risk: () => request<Risk>("/api/v1/risk/status"),
  status: () => request<Record<string, unknown>>("/api/v1/status"),
  quote: (symbol: string) => request<{ symbol: string; bid: number; ask: number; spread_points: number }>(
    `/api/v1/market/quote?symbol=${encodeURIComponent(symbol)}`
  ),
  kill: (engaged: boolean, reason = "dashboard") =>
    request<Record<string, unknown>>("/api/v1/risk/killswitch", {
      method: "POST",
      body: JSON.stringify({ engaged, reason })
    })
};
