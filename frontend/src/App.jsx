import { useEffect, useRef, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis
} from "recharts";

const REFRESH_MS = 3000;
const TOKEN = new URLSearchParams(window.location.search).get("token") || "";
const PALETTE = ["#62c7ff", "#3ce6b0", "#ffbd5c", "#8f88ff", "#ff6b7a", "#7cf3d7"];

function apiUrl(path) {
  if (!TOKEN) return path;
  return `${path}${path.includes("?") ? "&" : "?"}token=${encodeURIComponent(TOKEN)}`;
}

function num(value, digits = 4) {
  const parsed = Number.parseFloat(value ?? 0);
  return Number.isFinite(parsed) ? parsed.toFixed(digits) : "-";
}

function signed(value, digits = 4) {
  const parsed = Number.parseFloat(value ?? 0);
  if (!Number.isFinite(parsed)) return "-";
  return `${parsed >= 0 ? "+" : ""}${parsed.toFixed(digits)}`;
}

function pretty(value) {
  return String(value || "-").replace(/_/g, " ").replace(/\b\w/g, (char) => char.toUpperCase());
}

function toneForState(item) {
  if (item.active_trade) return "good";
  const eventType = String(item.decision?.event_type || "").toLowerCase();
  if (eventType === "signal") return "good";
  if (eventType === "hold") return "bad";
  return "warn";
}

function toneForTrend(value) {
  const text = String(value || "").toLowerCase();
  if (["bull", "ok", "long", "live"].includes(text)) return "good";
  if (["bear", "error", "short", "blocked"].includes(text)) return "bad";
  return "warn";
}

function useDashboardData() {
  const [stats, setStats] = useState({
    total_pnl: 0,
    total_trades: 0,
    win_rate: 0,
    avg_win: 0,
    avg_loss: 0,
    profit_factor: 0,
    setup_stats: {},
    grade_stats: {}
  });
  const [trades, setTrades] = useState([]);
  const [equity, setEquity] = useState({ labels: [], values: [] });
  const [marketChart, setMarketChart] = useState({ labels: [], datasets: [] });
  const [market, setMarket] = useState({ watchlist: [], positions: [], account: {}, error: "" });
  const [status, setStatus] = useState("Connecting...");
  const [heroStatus, setHeroStatus] = useState("Syncing stream");
  const [footer, setFooter] = useState("-");
  const streamRef = useRef(null);

  useEffect(() => {
    let active = true;

    async function loadInitial() {
      try {
        const [statsRes, tradesRes, equityRes] = await Promise.all([
          fetch(apiUrl("/api/stats")),
          fetch(apiUrl("/api/trades?limit=100")),
          fetch(apiUrl("/api/equity"))
        ]);
        const [statsPayload, tradesPayload, equityPayload] = await Promise.all([
          statsRes.json(),
          tradesRes.json(),
          equityRes.json()
        ]);
        if (!active) return;
        setStats(statsPayload);
        setTrades(tradesPayload.trades || []);
        setEquity(equityPayload);
      } catch {
        if (active) setHeroStatus("Initial sync failed");
      }
    }

    async function loadLive() {
      try {
        const [marketRes, chartRes] = await Promise.all([
          fetch(apiUrl("/api/market-overview")),
          fetch(apiUrl("/api/market-chart"))
        ]);
        const [marketPayload, chartPayload] = await Promise.all([marketRes.json(), chartRes.json()]);
        if (!active) return;
        setMarket(marketPayload);
        setMarketChart(chartPayload);
        setStatus(marketPayload.error ? "Decision feed only" : "Live market synced");
        const actionable = (marketPayload.watchlist || []).filter((item) => {
          const state = String(item.display_state || "").toUpperCase();
          return state.includes("IN ") || state.includes("READY") || state.includes("SETUP");
        }).length;
        setHeroStatus(
          marketPayload.error
            ? "Decision feed active, market feed unavailable"
            : `${actionable} coins active or setup-ready`
        );
      } catch {
        if (!active) return;
        setStatus("Live data unavailable");
        setHeroStatus("Reconnect pending");
      }
    }

    function connectStream() {
      const stream = new EventSource(apiUrl("/stream"));
      streamRef.current = stream;

      stream.onopen = () => {
        if (!active) return;
        setStatus("Stream connected");
        setHeroStatus("Streaming fills and equity");
      };

      stream.onmessage = (event) => {
        if (!active) return;
        const payload = JSON.parse(event.data);
        if (payload.stats) setStats(payload.stats);
        if (payload.recent) setTrades(payload.recent);
        if (payload.equity) setEquity(payload.equity);
        setFooter(new Date().toLocaleTimeString());
      };

      stream.onerror = () => {
        stream.close();
        if (!active) return;
        setStatus("Reconnecting stream...");
        setHeroStatus("Stream reconnecting");
        setTimeout(() => {
          if (active) connectStream();
        }, 5000);
      };
    }

    loadInitial();
    loadLive();
    connectStream();
    const intervalId = setInterval(loadLive, REFRESH_MS);

    return () => {
      active = false;
      clearInterval(intervalId);
      streamRef.current?.close();
    };
  }, []);

  return { stats, trades, equity, marketChart, market, status, heroStatus, footer };
}

function MetricCard({ label, value, tone = "neutral" }) {
  return (
    <div className="metric-card">
      <span>{label}</span>
      <strong className={`tone-${tone}`}>{value}</strong>
    </div>
  );
}

function Panel({ title, subtitle, children }) {
  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <h3>{title}</h3>
          <p>{subtitle}</p>
        </div>
      </div>
      {children}
    </section>
  );
}

function ChartPanel({ title, subtitle, data, mode }) {
  const chartData =
    mode === "equity"
      ? (data.labels || []).map((label, index) => ({ label, Equity: Number(data.values?.[index] || 0) }))
      : (data.labels || []).map((label, index) => {
          const row = { label };
          (data.datasets || []).forEach((dataset) => {
            row[dataset.label] = Number(dataset.data?.[index] || 0);
          });
          return row;
        });

  const lines =
    mode === "equity"
      ? [{ key: "Equity", color: chartData.length > 1 && chartData.at(-1)?.Equity >= chartData[0]?.Equity ? "#3ce6b0" : "#ff6b7a" }]
      : (data.datasets || []).map((dataset, index) => ({ key: dataset.label, color: dataset.borderColor || PALETTE[index % PALETTE.length] }));

  return (
    <Panel title={title} subtitle={subtitle}>
      <div className="chart-shell">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={chartData}>
            <CartesianGrid stroke="rgba(141, 162, 188, 0.12)" vertical={false} />
            <XAxis dataKey="label" tick={{ fill: "#8da2bc", fontSize: 11 }} minTickGap={24} />
            <YAxis tick={{ fill: "#8da2bc", fontSize: 11 }} width={72} />
            <Tooltip
              contentStyle={{
                background: "rgba(7,14,24,0.96)",
                border: "1px solid rgba(141,162,188,0.12)",
                borderRadius: "16px",
                color: "#f4f7fb"
              }}
            />
            {lines.map((line) => (
              <Line key={line.key} type="monotone" dataKey={line.key} stroke={line.color} strokeWidth={2.5} dot={false} />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </Panel>
  );
}

export default function App() {
  const { stats, trades, equity, marketChart, market, status, heroStatus, footer } = useDashboardData();
  const watchlist = market.watchlist || [];
  const positions = market.positions || [];
  const setupStats = Object.entries(stats.setup_stats || {});
  const gradeStats = ["A", "B", "C", "D"].filter((grade) => stats.grade_stats?.[grade]).map((grade) => [grade, stats.grade_stats[grade]]);
  const bestGrade = ["A", "B", "C", "D"].find((grade) => Number(stats.grade_stats?.[grade]?.trades || 0) > 0) || "No data";

  return (
    <div className="app-shell">
      <div className="app-frame">
        <div className="topbar">
          <div className="brand">
            <div className="brand-mark" />
            <div>
              <div className="eyebrow">Delta Engine</div>
              <p className="brand-subtitle">PRATHMESH SONAWANE</p>
              <p>Execution-aware dashboard with live wallet, HTF bias, blockers, and rapid pricing.</p>
            </div>
          </div>
          <div className="status-strip">
            <div className="status-pill">{status}</div>
            <div className="status-pill">Refresh 3s</div>
            <div className="status-pill">Mode Live Monitor</div>
          </div>
        </div>

        <section className="hero-grid">
          <div className="hero-card hero-main">
            <div className="section-label">Execution cockpit</div>
            <h2>Sharper signal, faster reads, cleaner live risk.</h2>
            <p>React now drives the dashboard, so watchlist cards, wallet balance, HTF state, blockers, and live positions update with a cleaner component-based UI.</p>
            <div className="hero-rail">
              <div className="rail-chip"><span>Total Trades</span><strong>{stats.total_trades || 0}</strong></div>
              <div className="rail-chip"><span>Best Grade</span><strong>{bestGrade}</strong></div>
              <div className="rail-chip"><span>Active Positions</span><strong>{positions.length} open</strong></div>
              <div className="rail-chip"><span>Live Status</span><strong>{heroStatus}</strong></div>
              <div className="rail-chip"><span>Wallet Balance</span><strong>{market.account?.asset ? `${num(market.account.balance)} ${market.account.asset}` : "-"}</strong></div>
              <div className="rail-chip"><span>Wallet Equity</span><strong>{market.account?.asset ? `${num(market.account.equity)} ${market.account.asset}` : "-"}</strong></div>
            </div>
          </div>

          <div className="hero-card hero-metrics">
            <MetricCard label="Total PnL" value={signed(stats.total_pnl)} tone={stats.total_pnl >= 0 ? "good" : "bad"} />
            <MetricCard label="Win Rate" value={`${num(stats.win_rate, 1)}%`} tone={stats.win_rate >= 50 ? "good" : "bad"} />
            <MetricCard label="Profit Factor" value={num(stats.profit_factor, 2)} tone={stats.profit_factor >= 1 ? "good" : "bad"} />
            <MetricCard label="Avg Win" value={signed(stats.avg_win)} tone="good" />
            <MetricCard label="Avg Loss" value={num(stats.avg_loss)} tone="bad" />
            <MetricCard label="Wallet Balance" value={market.account?.asset ? `${num(market.account.balance)} ${market.account.asset}` : "-"} />
          </div>
        </section>

        <Panel title="Watchlist" subtitle="Execution-aware coin cards with HTF, confidence, blockers, and live trade context.">
          {market.error && !watchlist.length ? (
            <div className="empty-state">{market.error}</div>
          ) : (
            <div className="watchlist-grid">
              {watchlist.map((item) => {
                const ticker = item.ticker || {};
                const decision = item.decision || {};
                const blockers = Array.isArray(decision.blockers) ? decision.blockers.slice(0, 4) : [];
                const confidence = Math.max(0, Math.min(100, Number.parseFloat(decision.confidence || 0) * 100));
                const pnl = Number.parseFloat(item.position?.unrealized_pnl || 0);

                return (
                  <article className="watch-card" key={item.symbol}>
                    <div className="watch-top">
                      <div>
                        <div className="coin-symbol">{item.symbol}</div>
                        <div className="coin-sub">Updated {item.updated_label || "-"} | Candle {decision.candle_time || "-"}</div>
                      </div>
                      <div className="coin-tag">{item.active_trade ? "Active trade" : "Watchlist"}</div>
                    </div>

                    <div className="watch-price-row">
                      <div className="coin-price">{ticker.last_price ? num(ticker.last_price) : "--"}</div>
                      <div className="pill-wrap">
                        <span className={`pill tone-${toneForState(item)}`}>{item.active_trade ? `IN ${String(item.active_trade.side || "").toUpperCase()}` : item.display_state || "WAIT"}</span>
                        <span className={`pill tone-${toneForTrend(decision.htf)}`}>HTF {pretty(decision.htf)}</span>
                        <span className={`pill tone-${toneForTrend(decision.regime)}`}>{pretty(decision.regime)}</span>
                      </div>
                    </div>

                    <div className="decision-grid">
                      <MetricCard label="Confidence" value={`${confidence.toFixed(0)}%`} />
                      <MetricCard label="RSI" value={decision.rsi === undefined || decision.rsi === "-" ? "-" : num(decision.rsi, 1)} />
                      <MetricCard label="Exec State" value={pretty(decision.event_type || item.latest_execution?.event_type || "waiting")} />
                      <MetricCard label="Live PnL" value={item.position ? signed(pnl) : "-"} tone={pnl >= 0 ? "good" : "bad"} />
                    </div>

                    <div className="confidence-track"><div className="confidence-fill" style={{ width: `${confidence}%` }} /></div>

                    <div className="blocker-list">
                      {blockers.length
                        ? blockers.map((blocker) => <span className="blocker-chip" key={blocker}>{pretty(blocker)}</span>)
                        : <span className="blocker-chip tone-good">No active blockers</span>}
                    </div>

                    <div className="decision-grid compact-gap">
                      <MetricCard label="Funding" value={num(ticker.funding_rate)} tone={Number(ticker.funding_rate || 0) >= 0 ? "good" : "bad"} />
                      <MetricCard label="Open Interest" value={Math.round(Number.parseFloat(ticker.open_interest || 0)).toLocaleString()} />
                      <MetricCard label="Mark" value={ticker.mark_price ? num(ticker.mark_price) : "-"} />
                      <MetricCard label="Spread" value={ticker.last_price ? num(Number.parseFloat(ticker.last_price) - Number.parseFloat(ticker.mark_price || 0)) : "-"} />
                    </div>
                  </article>
                );
              })}
            </div>
          )}
        </Panel>

        <section className="two-col">
          <Panel title="Decision Queue" subtitle="Per-coin scan of HTF alignment, action state, and confidence.">
            <div className="board-list">
              {watchlist.map((item) => {
                const decision = item.decision || {};
                const confidence = Math.max(0, Math.min(100, Number.parseFloat(decision.confidence || 0) * 100));
                return (
                  <div className="board-row" key={`queue-${item.symbol}`}>
                    <div className="board-symbol">{item.symbol}</div>
                    <div className="board-meta">
                      <div>HTF {pretty(decision.htf)} | Regime {pretty(decision.regime)} | Confidence {confidence.toFixed(0)}%</div>
                      <div>{decision.candle_time || "No decision candle"} | Last update {item.updated_label || "-"}</div>
                    </div>
                    <div className={`pill tone-${toneForState(item)}`}>{item.display_state || "WAIT"}</div>
                  </div>
                );
              })}
            </div>
          </Panel>

          <Panel title="Attention Board" subtitle="Fast view of what is blocking entries across the watchlist.">
            <div className="board-list">
              {watchlist.map((item) => {
                const blockers = Array.isArray(item.decision?.blockers) ? item.decision.blockers : [];
                return (
                  <div className="board-row" key={`blockers-${item.symbol}`}>
                    <div className="board-symbol">{item.symbol}</div>
                    <div className="board-meta">
                      <div>{blockers.length ? blockers.slice(0, 3).map(pretty).join(" | ") : "No blockers"}</div>
                      <div>{pretty(item.decision?.event_type || "waiting")} | HTF {pretty(item.decision?.htf || "-")}</div>
                    </div>
                    <div className={`pill tone-${blockers.length ? "bad" : "good"}`}>{blockers.length ? `${blockers.length} blockers` : "clear"}</div>
                  </div>
                );
              })}
            </div>
          </Panel>
        </section>

        <section className="two-col chart-row">
          <ChartPanel title="Multi-Asset Market Tape" subtitle="Cross-asset intraday movement for tracked symbols." data={marketChart} mode="market" />
          <ChartPanel title="Equity Curve" subtitle="Equity trend with live streaming updates." data={equity} mode="equity" />
        </section>

        <Panel title="Open Positions" subtitle="Size, direction, current mark, and unrealized PnL.">
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>Symbol</th><th>Side</th><th>Size</th><th>Entry</th><th>Mark</th><th>uPnL</th><th>Margin</th></tr>
              </thead>
              <tbody>
                {positions.length ? positions.map((position) => {
                  const pnl = Number.parseFloat(position.unrealized_pnl || 0);
                  return (
                    <tr key={`${position.symbol}-${position.side}`}>
                      <td><strong>{position.symbol}</strong></td>
                      <td><span className={`pill tone-${String(position.side || "").toLowerCase() === "long" ? "good" : "bad"}`}>{String(position.side || "").toUpperCase()}</span></td>
                      <td>{num(position.size)}</td>
                      <td>{num(position.entry_price)}</td>
                      <td>{num(position.mark_price)}</td>
                      <td className={`tone-${pnl >= 0 ? "good" : "bad"}`}>{signed(pnl)}</td>
                      <td>{num(position.margin)}</td>
                    </tr>
                  );
                }) : <tr><td colSpan="7" className="empty-state">No open positions</td></tr>}
              </tbody>
            </table>
          </div>
        </Panel>

        <section className="two-col">
          <Panel title="Setup Performance" subtitle="Which setups are doing the work.">
            <div className="analytics-list">
              {setupStats.length ? setupStats.map(([setup, value]) => (
                <div className="analytics-row" key={setup}>
                  <div>{pretty(setup)}</div>
                  <div className="muted">{value.trades} trades | {value.win_rate}% win rate</div>
                  <div className={`tone-${Number(value.total_pnl) >= 0 ? "good" : "bad"}`}>{signed(value.total_pnl, 3)}</div>
                </div>
              )) : <div className="empty-state">No setup data yet</div>}
            </div>
          </Panel>

          <Panel title="Entry Grade Breakdown" subtitle="Grade quality and realized results.">
            <div className="analytics-list">
              {gradeStats.length ? gradeStats.map(([grade, value]) => (
                <div className="analytics-row" key={grade}>
                  <div>Grade {grade}</div>
                  <div className="muted">{value.trades} trades | {value.win_rate}% win rate</div>
                  <div className={`tone-${Number(value.total_pnl) >= 0 ? "good" : "bad"}`}>{signed(value.total_pnl, 3)}</div>
                </div>
              )) : <div className="empty-state">No grade data yet</div>}
            </div>
          </Panel>
        </section>

        <Panel title="Recent Trades" subtitle="Latest fills with side, setup, grade, and exit quality.">
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>Time</th><th>Symbol</th><th>Side</th><th>Setup</th><th>Grade</th><th>Entry</th><th>Exit</th><th>PnL</th><th>Reason</th></tr>
              </thead>
              <tbody>
                {[...trades].reverse().length ? [...trades].reverse().map((trade, index) => {
                  const pnl = Number.parseFloat(trade.pnl || 0);
                  return (
                    <tr key={`${trade.symbol}-${trade.exit_time || index}`}>
                      <td>{trade.exit_time ? String(trade.exit_time).slice(0, 16) : "-"}</td>
                      <td><strong>{trade.symbol}</strong></td>
                      <td><span className={`pill tone-${String(trade.side || "").toLowerCase() === "long" ? "good" : "bad"}`}>{String(trade.side || "").toUpperCase()}</span></td>
                      <td>{pretty(trade.setup_type)}</td>
                      <td>{String(trade.entry_grade || "U").toUpperCase()}</td>
                      <td>{num(trade.entry_price)}</td>
                      <td>{trade.exit_price ? num(trade.exit_price) : "-"}</td>
                      <td className={`tone-${pnl >= 0 ? "good" : "bad"}`}>{signed(pnl)}</td>
                      <td>{trade.exit_reason || "-"}</td>
                    </tr>
                  );
                }) : <tr><td colSpan="9" className="empty-state">No trades yet</td></tr>}
              </tbody>
            </table>
          </div>
        </Panel>

        <footer className="footer">
          <span>Last updated: {footer}</span>
          <span>React dashboard upgrade</span>
        </footer>
      </div>
    </div>
  );
}
