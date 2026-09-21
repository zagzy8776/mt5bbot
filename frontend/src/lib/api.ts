export type ApiError = {
  detail?: string;
};

const API_BASE = (import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000").replace(/\/$/, "");

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

  const response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
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

export type Runtime = {
  state: string;
  symbol: string;
  timeframe: string;
  started_at: string | null;
  last_error: string | null;
  connected: boolean;
  kill_switch: boolean;
  stats: Record<string, unknown>;
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
  positions: async () => (await request<{ positions: Position[] }>("/api/v1/positions")).positions,
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
