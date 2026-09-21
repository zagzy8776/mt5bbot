import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  api,
  getToken,
  setToken,
  type Account,
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
  const [orders, setOrders] = useState<Order[]>([]);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [risk, setRisk] = useState<Risk | null>(null);
  const [status, setStatus] = useState<Record<string, unknown> | null>(null);
  const [symbol, setSymbol] = useState("XAUUSD");
  const [timeframe, setTimeframe] = useState("M15");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setError("");
    try {
      const [rt, ac, pos, ord, sig, rk, st] = await Promise.all([
        api.runtime(),
        api.account().catch(() => null),
        api.positions().catch(() => []),
        api.orders().catch(() => []),
        api.signals().catch(() => []),
        api.risk(),
        api.status()
      ]);
      setRuntime(rt);
      setAccount(ac);
      setPositions(pos);
      setOrders(ord);
      setSignals(sig);
      setRisk(rk);
      setStatus(st);
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
            <Badge tone={risk?.kill_switch ? "bad" : "good"}>{risk?.kill_switch ? "KILL SWITCH" : "TRADING ENABLED"}</Badge>
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
                </div>
                <div className="button-row">
                  <button className="primary" disabled={busy || running} onClick={() => void doAction(() => api.start(symbol, timeframe))}>Start bot</button>
                  <button disabled={busy || !running} onClick={() => void doAction(api.stop)}>Stop bot</button>
                  <button disabled={busy} onClick={() => void doAction(api.restart)}>Restart</button>
                </div>
              </section>

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

        {view === "positions" && <section className="card"><div className="section-head"><h2>Open positions</h2><Badge tone="neutral">{positions.length}</Badge></div><PositionTable positions={positions} /></section>}
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
            <pre>{JSON.stringify(status, null, 2)}</pre>
          </section>
        )}
      </main>
    </div>
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
  if (!positions.length) return <Empty text="No bot-managed positions." />;
  return <div className="table-wrap"><table><thead><tr><th>Symbol</th><th>Side</th><th>Volume</th><th>Entry</th><th>Current</th><th>P/L</th><th>SL</th><th>TP</th></tr></thead><tbody>
    {positions.map(p => <tr key={p.ticket}><td>{p.symbol}</td><td><Badge tone={p.side === "buy" ? "good" : "bad"}>{p.side.toUpperCase()}</Badge></td><td>{p.volume}</td><td>{fmt(p.entry_price)}</td><td>{fmt(p.current_price)}</td><td className={p.floating_pnl >= 0 ? "positive" : "negative"}>{money(p.floating_pnl)}</td><td>{fmt(p.stop_loss)}</td><td>{fmt(p.take_profit)}</td></tr>)}
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
