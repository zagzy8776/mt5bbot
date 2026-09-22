import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  api,
  getToken,
  setToken,
  type Account,
  type AuditEvent,
  type Order,
  type Position,
  type Risk,
  type Runtime,
  type Signal
} from "./lib/api";

type View = "overview" | "positions" | "orders" | "signals" | "risk" | "settings";

const fmt = (value: number | null | undefined, digits = 2) =>
  value == null || Number.isNaN(value) ? "—" : value.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  });

const money = (value: number | null | undefined) =>
  value == null || Number.isNaN(value) ? "—" : `${value < 0 ? "-" : ""}$${fmt(Math.abs(value), 2)}`;

function Badge({ tone = "neutral", children }: { tone?: "good" | "bad" | "warn" | "neutral"; children: ReactNode }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

function Card({ title, value, note }: { title: string; value: ReactNode; note?: string }) {
  return (
    <section className="card stat-card">
      <div className="eyebrow">{title}</div>
      <div className="stat-value">{value}</div>
      {note && <div className="muted">{note}</div>}
    </section>
  );
}

export default function App() {
  const [view, setView] = useState<View>("overview");
  const [tokenInput, setTokenInput] = useState(getToken());
  const [runtime, setRuntime] = useState<Runtime | null>(null);
  const [account, setAccount] = useState<Account | null>(null);
  const [positions, setPositions] = useState<Position[]>([]);
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [manualPolicy, setManualPolicy] = useState("");
  const [orders, setOrders] = useState<Order[]>([]);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [risk, setRisk] = useState<Risk | null>(null);
  const [status, setStatus] = useState<Record<string, unknown> | null>(null);
  const [symbol, setSymbol] = useState("");
  const [timeframe, setTimeframe] = useState("M15");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setError("");
    try {
      const [rt, ac, pos, ord, sig, rk, st, ev] = await Promise.all([
        api.runtime(),
        api.account().catch(() => null),
        api.positions().catch(() => ({ positions: [] as Position[], manual_position_policy: "" })),
        api.orders().catch(() => []),
        api.signals().catch(() => []),
        api.risk(),
        api.status(),
        api.audit(50).catch(() => ({ events: [] as AuditEvent[] }))
      ]);
      setRuntime(rt);
      setAccount(ac);
      setPositions(pos.positions);
      setManualPolicy(pos.manual_position_policy);
      setOrders(ord);
      setSignals(sig);
      setRisk(rk);
      setStatus(st);
      setEvents(ev.events);
      if (rt.symbol) setSymbol(rt.symbol);
      if (rt.timeframe) setTimeframe(rt.timeframe);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to reach the API");
    }
  }, []);

  useEffect(() => {
    void load();
    const id = window.setInterval(() => void load(), 4000);
    return () => window.clearInterval(id);
  }, [load]);

  const doAction = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    try {
      await fn();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Action failed");
    } finally {
      setBusy(false);
    }
  };

  const connected = runtime?.connected === true;
  const running = runtime?.state === "running";
  const lifecycle = useMemo(
    () => events.filter((e) => e.component === "position_lifecycle"),
    [events]
  );
  const dailyPnL = account?.daily_pnl ?? 0;
  const navLabel = useMemo(() => ({
    overview: "Overview",
    positions: "Positions",
    orders: "Orders",
    signals: "Signals",
    risk: "Risk",
    settings: "Settings"
  }[view]), [view]);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">MT</div>
          <div>
            <strong>MT5 Control</strong>
            <span>Trading operations</span>
          </div>
        </div>

        <nav>
          {([
            ["overview", "Overview", "⌂"],
            ["positions", "Positions", "◈"],
            ["orders", "Orders", "⇄"],
            ["signals", "Signals", "⌁"],
            ["risk", "Risk", "△"],
            ["settings", "Settings", "⚙"]
          ] as [View, string, string][]).map(([key, label, icon]) => (
            <button
              key={key}
              className={`nav-item ${view === key ? "active" : ""}`}
              onClick={() => setView(key)}
            >
              <span>{icon}</span>{label}
            </button>
          ))}
        </nav>

        <div className="sidebar-footer">
          <div className="connection-line">
            <span className={`status-dot ${connected ? "online" : "offline"}`} />
            <span>{connected ? "MT5 connected" : "MT5 disconnected"}</span>
          </div>
          <button className="refresh-link" onClick={() => void load()}>Refresh data</button>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <div>
            <div className="eyebrow">CONTROL ROOM</div>
            <h1>{navLabel}</h1>
          </div>
          <div className="top-actions">
            {runtime && <Badge tone={running ? "good" : runtime.state === "error" ? "bad" : "warn"}>{runtime.state.toUpperCase()}</Badge>}
            <Badge tone="neutral">{status?.trading_mode === "demo" ? "DEMO" : status?.is_live ? "LIVE" : "UNKNOWN"}</Badge>
            {runtime && <Badge tone={connected ? "good" : "warn"}>{connected ? "MT5 CONNECTED" : "MT5 OFFLINE"}</Badge>}
            <Badge tone={risk?.kill_switch ? "bad" : "warn"}>{risk?.kill_switch ? "KILL SWITCH" : running ? "TRADING" : "STOPPED"}</Badge>
          </div>
        </header>

        {error && <div className="alert error">{error}</div>}

        {view === "overview" && (
          <>
            <div className="hero-grid">
              <Card title="Balance" value={money(account?.balance)} note="Broker account balance" />
              <Card title="Equity" value={money(account?.equity)} note={account ? `Drawdown ${fmt(account.drawdown_pct)}%` : "Waiting for MT5"} />
              <Card title="Daily P/L" value={<span className={dailyPnL >= 0 ? "positive" : "negative"}>{money(dailyPnL)}</span>} note="Realized + floating" />
              <Card title="Open positions" value={account?.open_positions ?? positions.length} note={runtime?.symbol || "XAUUSD"} />
            </div>

            <div className="content-grid">
              <section className="card">
                <div className="section-head">
                  <div>
                    <div className="eyebrow">RUNTIME</div>
                    <h2>Execution engine</h2>
                  </div>
                  <Badge tone={running ? "good" : "warn"}>{runtime?.state || "stopped"}</Badge>
                </div>
                <div className="runtime-panel">
                  <div><span>Symbol</span><strong>{runtime?.symbol || symbol}</strong></div>
                  <div><span>Timeframe</span><strong>{runtime?.timeframe || timeframe}</strong></div>
                  <div><span>Signals</span><strong>{Number(runtime?.stats?.signals ?? 0)}</strong></div>
                  <div><span>Orders sent</span><strong>{Number(runtime?.stats?.orders_sent ?? 0)}</strong></div>
                  <div><span>Closed candles</span><strong>{Number(runtime?.stats?.bars_processed ?? 0)}</strong></div>
                  <div><span>Signal evaluations</span><strong>{Number(runtime?.stats?.signal_engine?.evaluations ?? 0)}</strong></div>
                </div>
                <div className="button-row">
                  <button className="primary" disabled={busy || running} onClick={() => void doAction(() => api.start(symbol, timeframe))}>Start bot</button>
                  <button disabled={busy || !running} onClick={() => void doAction(api.stop)}>Stop bot</button>
                  <button disabled={busy} onClick={() => void doAction(api.restart)}>Restart</button>
                </div>
              </section>

              <SignalPipeline runtime={runtime} />

              <section className="card">
                <div className="section-head">
                  <div>
                    <div className="eyebrow">MARKET</div>
                    <h2>{symbol}</h2>
                  </div>
                  <Badge tone={connected ? "good" : "warn"}>{connected ? "LIVE FEED" : "OFFLINE"}</Badge>
                </div>
                <QuotePanel symbol={symbol} connected={connected} />
              </section>
            </div>

            <section className="card">
              <div className="section-head"><h2>Recent activity</h2><button className="ghost" onClick={() => setView("orders")}>View orders</button></div>
              <OrderTable orders={orders.slice(0, 8)} />
            </section>
          </>
        )}

        {view === "positions" && (
          <>
            <section className="card"><div className="section-head"><h2>Open positions</h2><Badge tone="neutral">{positions.length}</Badge>{manualPolicy && <Badge tone={manualPolicy === "manage" ? "good" : "neutral"}>manual: {manualPolicy}</Badge>}</div><PositionTable positions={positions} /></section>
            <section className="card"><div className="section-head"><h2>Position lifecycle</h2><Badge tone="neutral">{lifecycle.length}</Badge></div><LifecycleTable events={lifecycle} /></section>
          </>
        )}
        {view === "orders" && <section className="card"><div className="section-head"><h2>Recent orders</h2><Badge tone="neutral">{orders.length}</Badge></div><OrderTable orders={orders} /></section>}
        {view === "signals" && <section className="card"><div className="section-head"><h2>Signals</h2><Badge tone="neutral">{signals.length}</Badge></div><SignalTable signals={signals} /></section>}
        {view === "risk" && <RiskView risk={risk} account={account} onKill={() => void doAction(() => api.kill(!risk?.kill_switch))} />}
        {view === "settings" && (
          <section className="card settings-card">
            <div className="eyebrow">CONNECTION</div>
            <h2>API and runtime settings</h2>
            <label>API base URL<input value={import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000"} readOnly /></label>
            <label>Bearer token<input value={tokenInput} onChange={(e) => setTokenInput(e.target.value)} type="password" placeholder="Paste API_TOKEN" /></label>
            <div className="button-row"><button className="primary" onClick={() => { setToken(tokenInput); void load(); }}>Save token</button></div>
            <div className="divider" />
            <div className="eyebrow">RUNTIME TARGET</div>
            <div className="form-grid">
              <label>Symbol<input value={symbol} onChange={(e) => setSymbol(e.target.value.toUpperCase())} /></label>
              <label>Timeframe<select value={timeframe} onChange={(e) => setTimeframe(e.target.value)}>{["M1","M5","M15","M30","H1","H4"].map(tf => <option key={tf}>{tf}</option>)}</select></label>
            </div>
            <p className="muted">Runtime changes apply the next time the bot is started.</p>
            <pre>{status ? JSON.stringify(status, null, 2) : "API not reachable — check token and URL above."}</pre>
          </section>
        )}
      </main>
    </div>
  );
}

const PIPELINE_STAGES: Record<
  string,
  { label: string; tone: "good" | "bad" | "warn" | "neutral"; hint: string }
> = {
  not_running: {
    label: "runtime stopped",
    tone: "neutral",
    hint: "Start the runtime to evaluate closed candles."
  },
  warmup_complete: {
    label: "primed · replaying history",
    tone: "neutral",
    hint: "Historical candles were replayed to prime the strategies. Those signals are never traded; live trading starts with the next closed candle."
  },
  waiting_for_closed_candle: {
    label: "A · waiting for a closed candle",
    tone: "neutral",
    hint: "The last candle was already evaluated; the next one has not closed yet."
  },
  no_candle_available: {
    label: "A · no candle data",
    tone: "bad",
    hint: "The feed returned no candles — check the symbol and timeframe."
  },
  candle_evaluated_no_setup: {
    label: "B · candle evaluated, no setup",
    tone: "neutral",
    hint: "Strategies ran on the closed candle; no entry condition was met."
  },
  signal_not_actionable: {
    label: "C · signal not actionable",
    tone: "warn",
    hint: "A signal existed but could not be sized or protected."
  },
  signal_rejected_by_risk: {
    label: "D · rejected by risk",
    tone: "warn",
    hint: "The risk gate refused the signal — see the reasons below."
  },
  order_submitted: {
    label: "E · order submitted",
    tone: "neutral",
    hint: "Sent to MT5; the outcome is not final yet."
  },
  order_rejected: {
    label: "F · order rejected by broker",
    tone: "bad",
    hint: "MT5 refused the order — the broker reason is shown below."
  },
  order_filled: {
    label: "G · order filled",
    tone: "good",
    hint: "The bot is in the market with broker-side SL/TP."
  }
};

function SignalPipeline({ runtime }: { runtime: Runtime | null }) {
  const stats = runtime?.stats;
  const engine = stats?.signal_engine;
  const trace = stats?.pipeline;
  const stage = stats?.pipeline_stage || "not_running";
  const info = PIPELINE_STAGES[stage] || { label: stage, tone: "neutral" as const, hint: "" };
  const rejects =
    engine && Object.keys(engine.reject_reasons || {}).length
      ? Object.entries(engine.reject_reasons)
          .map(([reason, count]) => `${reason} ×${count}`)
          .join(", ")
      : "none";
  return (
    <section className="card">
      <div className="section-head">
        <div>
          <div className="eyebrow">SIGNAL PIPELINE</div>
          <h2>Candle → signal → risk → order</h2>
        </div>
        <Badge tone={info.tone}>{info.label}</Badge>
      </div>
      <p className="muted">{info.hint}</p>
      <div className="runtime-panel">
        <div><span>Closed candles</span><strong>{Number(stats?.bars_processed ?? 0)}</strong></div>
        <div><span>Evaluations</span><strong>{Number(engine?.evaluations ?? 0)}</strong></div>
        <div><span>Signals</span><strong>{Number(engine?.signals_generated ?? 0)}</strong></div>
        <div><span>Rejected</span><strong>{Number(engine?.signals_rejected ?? 0)}</strong></div>
        <div><span>Orders sent</span><strong>{Number(stats?.orders_sent ?? 0)}</strong></div>
        <div><span>Last evaluation</span><strong>{engine?.last_evaluation_time ? new Date(engine.last_evaluation_time).toLocaleTimeString() : "—"}</strong></div>
      </div>
      <div className="kv"><span>Warm-up replay</span><strong>{stats?.warmup_replay?.bars ? `${stats.warmup_replay.bars} bars · ${stats.warmup_replay.signals} signals discarded (never traded)` : "—"}</strong></div>
      <div className="kv"><span>Last signal</span><strong>{engine?.last_signal_strategy ? `${engine.last_signal_strategy} ${engine.last_signal_side?.toUpperCase()} @ ${fmt(engine.last_signal_entry)} (SL ${fmt(engine.last_signal_stop_loss)})` : "none yet"}</strong></div>
      <div className="kv"><span>Last rejection</span><strong>{engine?.last_rejection ? `${engine.last_rejection.strategy}: ${engine.last_rejection.reasons.join(", ")}` : "none"}</strong></div>
      <div className="kv"><span>Reject counts</span><strong>{rejects}</strong></div>
      <div className="kv"><span>Last cycle</span><strong>{trace?.stage ? `${trace.stage}${trace.reasons?.length ? ` (${trace.reasons.join(", ")})` : ""}${trace.order_id ? ` · ${trace.order_id}` : ""}${trace.waiting ? " · waiting for the next closed candle" : ""}` : "—"}</strong></div>
      {engine && Object.keys(engine.strategy_stats || {}).length > 0 && (
        <div className="table-wrap"><table><thead><tr><th>Strategy</th><th>Evaluations</th><th>Signals</th><th>Rejections</th></tr></thead><tbody>
          {Object.entries(engine.strategy_stats).map(([name, s]) => <tr key={name}><td>{name}</td><td>{s.evaluations}</td><td>{s.signals}</td><td>{s.rejections}</td></tr>)}
        </tbody></table></div>
      )}
    </section>
  );
}

function QuotePanel({ symbol, connected }: { symbol: string; connected: boolean }) {
  const [quote, setQuote] = useState<{ bid: number; ask: number; spread_points: number } | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!connected) { setQuote(null); return; }
    const load = () => api.quote(symbol).then(setQuote).catch((e: unknown) => setError(e instanceof Error ? e.message : "Quote error"));
    void load();
    const id = window.setInterval(load, 2000);
    return () => window.clearInterval(id);
  }, [symbol, connected]);
  if (!connected) return <div className="quote-offline">Start the runtime to read a broker quote.</div>;
  if (error) return <div className="quote-offline">{error}</div>;
  return (
    <div className="quote">
      <div><span>Bid</span><strong>{fmt(quote?.bid)}</strong></div>
      <div><span>Ask</span><strong>{fmt(quote?.ask)}</strong></div>
      <div><span>Spread</span><strong>{fmt(quote?.spread_points, 1)} pts</strong></div>
    </div>
  );
}

function PositionTable({ positions }: { positions: Position[] }) {
  if (!positions.length) return <Empty text="No open positions at the broker." />;
  return <div className="table-wrap"><table><thead><tr><th>Ticket</th><th>Symbol</th><th>Source</th><th>Side</th><th>Volume</th><th>Entry</th><th>Current</th><th>P/L</th><th>SL</th><th>TP</th></tr></thead><tbody>
    {positions.map(p => <tr key={p.ticket} title={`magic ${p.magic ?? "—"}${p.comment ? ` · ${p.comment}` : ""}${p.is_external ? " · not opened by this bot" : ""}`}><td>{p.ticket}</td><td>{p.symbol}</td><td><Badge tone={p.is_external ? "warn" : "neutral"}>{p.is_external ? "manual" : "bot"}</Badge></td><td><Badge tone={p.side === "buy" ? "good" : "bad"}>{p.side.toUpperCase()}</Badge></td><td>{p.volume}</td><td>{fmt(p.entry_price)}</td><td>{fmt(p.current_price)}</td><td className={p.floating_pnl >= 0 ? "positive" : "negative"}>{money(p.floating_pnl)}</td><td>{fmt(p.stop_loss)}</td><td>{fmt(p.take_profit)}</td></tr>)}
  </tbody></table></div>;
}

function LifecycleTable({ events }: { events: AuditEvent[] }) {
  if (!events.length) return <Empty text="No position lifecycle events yet." />;
  const tone = (eventType: string): "good" | "bad" | "warn" | "neutral" => {
    if (eventType.includes("REJECTED")) return "warn";
    if (eventType.includes("CLOSED") || eventType.includes("REDUCED")) return "bad";
    if (eventType.includes("MODIFIED")) return "good";
    return "neutral";
  };
  const rows = [...events].reverse();
  return <div className="table-wrap"><table><thead><tr><th>Time</th><th>Ticket</th><th>Event</th><th>Decision</th><th>Policy</th><th>Reason</th></tr></thead><tbody>
    {rows.map((e, i) => <tr key={`${e.correlation_id}-${e.event_type}-${i}`} title={e.payload.external ? "external/manual position" : "bot-owned position"}><td>{new Date(e.timestamp).toLocaleTimeString()}</td><td>{e.correlation_id}</td><td><Badge tone={tone(e.event_type)}>{e.event_type}</Badge></td><td>{String(e.payload.decision ?? "—")}</td><td>{String(e.payload.policy ?? "—")}</td><td>{String(e.payload.reason ?? "—")}</td></tr>)}
  </tbody></table></div>;
}

function OrderTable({ orders }: { orders: Order[] }) {
  if (!orders.length) return <Empty text="No orders recorded yet." />;
  return <div className="table-wrap"><table><thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Volume</th><th>Status</th><th>Entry</th><th>SL</th><th>TP</th></tr></thead><tbody>
    {orders.map(o => <tr key={o.order_id}><td>{new Date(o.created_at).toLocaleTimeString()}</td><td>{o.symbol}</td><td>{o.side.toUpperCase()}</td><td>{o.volume}</td><td><Badge tone={o.status.includes("rejected") || o.status === "failed" ? "bad" : o.status === "filled" ? "good" : "warn"}>{o.status}</Badge></td><td>{fmt(o.entry)}</td><td>{fmt(o.stop_loss)}</td><td>{fmt(o.take_profit)}</td></tr>)}
  </tbody></table></div>;
}

function SignalTable({ signals }: { signals: Signal[] }) {
  if (!signals.length) return <Empty text="No signals recorded yet." />;
  return <div className="table-wrap"><table><thead><tr><th>Time</th><th>Strategy</th><th>Direction</th><th>Confidence</th><th>Entry</th><th>Reason</th></tr></thead><tbody>
    {signals.map(s => <tr key={s.signal_id}><td>{new Date(s.timestamp).toLocaleTimeString()}</td><td>{s.strategy_name}</td><td><Badge tone={s.direction === "buy" ? "good" : "bad"}>{s.direction.toUpperCase()}</Badge></td><td>{(s.confidence * 100).toFixed(0)}%</td><td>{fmt(s.entry)}</td><td>{s.reason}</td></tr>)}
  </tbody></table></div>;
}

function RiskView({ risk, account, onKill }: { risk: Risk | null; account: Account | null; onKill: () => void }) {
  return <div className="risk-grid">
    <section className="card">
      <div className="eyebrow">PROTECTION</div><h2>Risk controls</h2>
      <div className="risk-state"><Badge tone={risk?.kill_switch ? "bad" : "good"}>{risk?.kill_switch ? "HALTED" : "ARMED"}</Badge><Badge tone={risk?.paused ? "warn" : "good"}>{risk?.paused ? "PAUSED" : "RUNNING"}</Badge></div>
      <button className={risk?.kill_switch ? "danger" : "danger"} onClick={onKill}>{risk?.kill_switch ? "Release kill switch" : "Engage kill switch"}</button>
      {risk?.halt_reasons?.length ? <div className="reason-list"><strong>Halt reasons</strong>{risk.halt_reasons.map(r => <span key={r}>{r}</span>)}</div> : <p className="muted">No active halt reasons.</p>}
    </section>
    <section className="card">
      <div className="eyebrow">ACCOUNT RISK</div><h2>Limits snapshot</h2>
      <div className="kv"><span>Drawdown</span><strong>{fmt(account?.drawdown_pct)}%</strong></div>
      <div className="kv"><span>Daily P/L</span><strong>{money(account?.daily_pnl)}</strong></div>
      <div className="kv"><span>Margin level</span><strong>{account?.margin_level == null ? "—" : `${fmt(account.margin_level)}%`}</strong></div>
      <div className="kv"><span>Exposure</span><strong>{money(account?.exposure)}</strong></div>
    </section>
  </div>;
}

function Empty({ text }: { text: string }) {
  return <div className="empty">{text}</div>;
}
