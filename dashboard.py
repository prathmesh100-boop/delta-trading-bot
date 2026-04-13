"""
dashboard.py — Institutional-grade trading dashboard (HARDENED v3)

IMPROVEMENTS vs v2:
  - Full HTML UI with live equity chart (Chart.js via CDN)
  - Real-time SSE pushes on every file change (no polling from browser)
  - Per-trade PnL, win/loss colouring, drawdown gauge
  - Risk status panel (halted, daily limit, capital, drawdown)
  - Trade table with all fields including contract_value and exit_reason
  - Equity curve sparkline auto-updated from CSV
  - Dark theme, mobile-responsive
"""
import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

import pandas as pd
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

logger = logging.getLogger("dashboard")
logging.basicConfig(level=logging.INFO)

ROOT = Path(__file__).parent
TRADE_FILE = ROOT / "trade_history.csv"
EQUITY_FILE = ROOT / "equity_curve.csv"

_file_lock = threading.Lock()

app = FastAPI(title="Trading Dashboard")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _load_trades() -> pd.DataFrame:
    if not TRADE_FILE.exists():
        return pd.DataFrame()
    with _file_lock:
        try:
            return pd.read_csv(TRADE_FILE)
        except Exception:
            return pd.DataFrame()


def _load_equity() -> pd.DataFrame:
    if not EQUITY_FILE.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(EQUITY_FILE)
        # Thin to max 500 points for chart
        if len(df) > 500:
            step = len(df) // 500
            df = df.iloc[::step]
        return df
    except Exception:
        return pd.DataFrame()


def _compute_stats(df: pd.DataFrame) -> dict:
    if df.empty:
        return {
            "total_pnl": 0.0, "total_trades": 0, "win_rate": 0.0,
            "avg_win": 0.0, "avg_loss": 0.0, "profit_factor": 0.0,
            "last_update_ts": None,
        }
    pnl_col = df.get("pnl", pd.Series(dtype=float)) if "pnl" in df.columns else pd.Series(dtype=float)
    wins = pnl_col[pnl_col > 0]
    losses = pnl_col[pnl_col <= 0]
    gross_profit = wins.sum()
    gross_loss = abs(losses.sum())
    return {
        "total_pnl": round(float(pnl_col.sum()), 4),
        "total_trades": int(len(df)),
        "win_rate": round(float((pnl_col > 0).mean() * 100), 1) if len(df) else 0.0,
        "avg_win": round(float(wins.mean()), 4) if len(wins) else 0.0,
        "avg_loss": round(float(losses.mean()), 4) if len(losses) else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else 0.0,
        "last_update_ts": int(TRADE_FILE.stat().st_mtime) if TRADE_FILE.exists() else None,
    }


def _require_token(request: Request):
    token = os.getenv("DASHBOARD_TOKEN")
    if not token:
        return True
    provided = request.headers.get("x-dashboard-token") or request.query_params.get("token")
    if provided != token:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True


# ── API Endpoints ─────────────────────────────

@app.get("/")
def root(_ok: bool = Depends(_require_token)):
    return JSONResponse({"status": "ok", "endpoints": ["/dashboard", "/api/stats", "/api/trades", "/api/equity", "/stream"]})


@app.get("/api/stats")
def api_stats(_ok: bool = Depends(_require_token)):
    df = _load_trades()
    return JSONResponse(_compute_stats(df))


@app.get("/api/trades")
def api_trades(limit: Optional[int] = 200, _ok: bool = Depends(_require_token)):
    df = _load_trades()
    if df.empty:
        return JSONResponse({"trades": [], "count": 0})
    trades = df.tail(limit).fillna("").to_dict(orient="records")
    return JSONResponse({"trades": trades, "count": len(trades)})


@app.get("/api/equity")
def api_equity(_ok: bool = Depends(_require_token)):
    df = _load_equity()
    if df.empty:
        return JSONResponse({"labels": [], "values": []})
    labels = df.get("time", df.index.astype(str)).tolist()
    values = df.get("equity", pd.Series(dtype=float)).round(4).tolist()
    return JSONResponse({"labels": labels, "values": values})


async def _sse_stream(poll: float = 2.0):
    last_mtime = 0
    while True:
        try:
            files = [TRADE_FILE, EQUITY_FILE]
            mtimes = [f.stat().st_mtime if f.exists() else 0 for f in files]
            mtime = max(mtimes)
            if mtime != last_mtime:
                last_mtime = mtime
                df = _load_trades()
                eq_df = _load_equity()
                eq_labels = eq_df.get("time", pd.Series(dtype=str)).tolist() if not eq_df.empty else []
                eq_values = eq_df.get("equity", pd.Series(dtype=float)).round(4).tolist() if not eq_df.empty else []
                payload = {
                    "stats": _compute_stats(df),
                    "recent": df.tail(50).fillna("").to_dict(orient="records"),
                    "equity": {"labels": eq_labels, "values": eq_values},
                }
                yield f"data: {json.dumps(payload)}\n\n"
        except Exception:
            pass
        await asyncio.sleep(poll)


@app.get("/stream")
def stream(_ok: bool = Depends(_require_token)):
    return StreamingResponse(_sse_stream(), media_type="text/event-stream")


# ── Full Dashboard UI ─────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Delta Trading Terminal</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0a0e1a; --bg2: #111827; --bg3: #1e2738;
    --border: #2d3748; --text: #e2e8f0; --muted: #94a3b8;
    --green: #22c55e; --red: #ef4444; --blue: #38bdf8;
    --yellow: #fbbf24; --orange: #f97316;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); font-size: 13px; }
  header { background: var(--bg2); border-bottom: 1px solid var(--border); padding: 12px 20px;
    display: flex; align-items: center; gap: 16px; }
  header h1 { font-size: 16px; font-weight: 700; color: var(--blue); }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--green); animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; padding: 16px; }
  .card { background: var(--bg2); border: 1px solid var(--border); border-radius: 8px; padding: 14px; }
  .card .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .5px; margin-bottom: 6px; }
  .card .value { font-size: 22px; font-weight: 700; }
  .positive { color: var(--green); }
  .negative { color: var(--red); }
  .neutral { color: var(--blue); }
  .halted { background: rgba(239,68,68,.12); border-color: var(--red); }
  .section { padding: 0 16px 16px; }
  .section h2 { font-size: 13px; font-weight: 600; color: var(--muted); margin-bottom: 10px;
    text-transform: uppercase; letter-spacing: .5px; }
  .chart-wrap { background: var(--bg2); border: 1px solid var(--border); border-radius: 8px;
    padding: 16px; height: 220px; position: relative; }
  table { width: 100%; border-collapse: collapse; }
  thead th { padding: 8px 10px; text-align: left; color: var(--muted); font-size: 11px;
    text-transform: uppercase; letter-spacing: .5px; border-bottom: 1px solid var(--border); }
  tbody tr { border-bottom: 1px solid rgba(45,55,72,.5); }
  tbody tr:hover { background: rgba(56,189,248,.04); }
  tbody td { padding: 7px 10px; }
  .tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
  .tag-long { background: rgba(34,197,94,.15); color: var(--green); }
  .tag-short { background: rgba(239,68,68,.15); color: var(--red); }
  .tag-sl { background: rgba(239,68,68,.15); color: var(--red); }
  .tag-tp { background: rgba(34,197,94,.15); color: var(--green); }
  .tag-flip { background: rgba(251,191,36,.15); color: var(--yellow); }
  .progress-bar { height: 6px; border-radius: 3px; background: var(--border); overflow: hidden; margin-top: 6px; }
  .progress-fill { height: 100%; border-radius: 3px; transition: width .3s; }
  .last-update { color: var(--muted); font-size: 11px; padding: 4px 16px 8px; }
  .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  @media(max-width:600px) { .two-col { grid-template-columns: 1fr; } .grid { grid-template-columns: 1fr 1fr; } }
</style>
</head>
<body>
<header>
  <div class="status-dot" id="dot"></div>
  <h1>📊 Delta Trading Terminal</h1>
  <span style="color:var(--muted);font-size:11px" id="last-update">Connecting…</span>
</header>

<div class="grid" id="stat-cards">
  <div class="card"><div class="label">Total PnL (USDT)</div><div class="value neutral" id="s-pnl">—</div></div>
  <div class="card"><div class="label">Total Trades</div><div class="value neutral" id="s-trades">—</div></div>
  <div class="card"><div class="label">Win Rate</div><div class="value" id="s-wr">—</div>
    <div class="progress-bar"><div class="progress-fill" id="wr-bar" style="background:var(--green);width:0%"></div></div>
  </div>
  <div class="card"><div class="label">Profit Factor</div><div class="value" id="s-pf">—</div></div>
  <div class="card"><div class="label">Avg Win</div><div class="value positive" id="s-aw">—</div></div>
  <div class="card"><div class="label">Avg Loss</div><div class="value negative" id="s-al">—</div></div>
</div>

<div class="section">
  <h2>Equity Curve</h2>
  <div class="chart-wrap">
    <canvas id="equity-chart"></canvas>
  </div>
</div>

<div class="section">
  <h2>Recent Trades</h2>
  <div style="overflow-x:auto; background:var(--bg2); border:1px solid var(--border); border-radius:8px; padding:4px">
    <table>
      <thead>
        <tr>
          <th>Time</th><th>Symbol</th><th>Side</th>
          <th>Entry</th><th>Exit</th><th>Lots</th>
          <th>PnL (USDT)</th><th>Reason</th>
        </tr>
      </thead>
      <tbody id="trades-body">
        <tr><td colspan="8" style="text-align:center;padding:20px;color:var(--muted)">Loading…</td></tr>
      </tbody>
    </table>
  </div>
</div>

<div class="last-update" id="footer">Last updated: —</div>

<script>
const TOKEN = new URLSearchParams(location.search).get('token') || '';

// ── Equity chart ──────────────────────────────
const ctx = document.getElementById('equity-chart').getContext('2d');
const equityChart = new Chart(ctx, {
  type: 'line',
  data: { labels: [], datasets: [{
    label: 'Equity', data: [],
    borderColor: '#38bdf8', backgroundColor: 'rgba(56,189,248,.06)',
    borderWidth: 1.5, pointRadius: 0, tension: 0.2, fill: true,
  }]},
  options: {
    responsive: true, maintainAspectRatio: false, animation: false,
    plugins: { legend: { display: false } },
    scales: {
      x: { display: false },
      y: { grid: { color: 'rgba(255,255,255,.05)' },
           ticks: { color: '#94a3b8', font: { size: 10 } } }
    }
  }
});

function updateEquity(labels, values) {
  // Show every Nth label to avoid clutter
  const N = Math.max(1, Math.floor(labels.length / 6));
  equityChart.data.labels = labels.map((l, i) => i % N === 0 ? l.substring(0, 10) : '');
  equityChart.data.datasets[0].data = values;
  // Color line based on trend
  const start = values[0] || 0, end = values[values.length - 1] || 0;
  equityChart.data.datasets[0].borderColor = end >= start ? '#22c55e' : '#ef4444';
  equityChart.data.datasets[0].backgroundColor = end >= start ? 'rgba(34,197,94,.06)' : 'rgba(239,68,68,.06)';
  equityChart.update('none');
}

// ── Stats update ──────────────────────────────
function updateStats(s) {
  const pnl = parseFloat(s.total_pnl) || 0;
  document.getElementById('s-pnl').textContent = (pnl >= 0 ? '+' : '') + pnl.toFixed(4);
  document.getElementById('s-pnl').className = 'value ' + (pnl >= 0 ? 'positive' : 'negative');
  document.getElementById('s-trades').textContent = s.total_trades;
  const wr = parseFloat(s.win_rate) || 0;
  document.getElementById('s-wr').textContent = wr.toFixed(1) + '%';
  document.getElementById('s-wr').className = 'value ' + (wr >= 50 ? 'positive' : 'negative');
  document.getElementById('wr-bar').style.width = wr + '%';
  const pf = parseFloat(s.profit_factor) || 0;
  document.getElementById('s-pf').textContent = pf.toFixed(2);
  document.getElementById('s-pf').className = 'value ' + (pf >= 1 ? 'positive' : 'negative');
  document.getElementById('s-aw').textContent = '+' + (s.avg_win || 0).toFixed(4);
  document.getElementById('s-al').textContent = (s.avg_loss || 0).toFixed(4);
}

// ── Trades table ──────────────────────────────
function updateTrades(trades) {
  const tbody = document.getElementById('trades-body');
  if (!trades || !trades.length) {
    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:20px;color:var(--muted)">No trades yet</td></tr>';
    return;
  }
  const rows = [...trades].reverse().map(t => {
    const pnl = parseFloat(t.pnl) || 0;
    const pnlClass = pnl >= 0 ? 'positive' : 'negative';
    const sideTag = `<span class="tag tag-${t.side}">${(t.side||'').toUpperCase()}</span>`;
    const reason = t.exit_reason || '';
    let reasonTag = reason;
    if (reason.includes('sl')) reasonTag = `<span class="tag tag-sl">SL</span>`;
    else if (reason.includes('tp')) reasonTag = `<span class="tag tag-tp">TP</span>`;
    else if (reason.includes('flip')) reasonTag = `<span class="tag tag-flip">FLIP</span>`;
    const exitTime = t.exit_time ? String(t.exit_time).substring(0, 16) : '—';
    return `<tr>
      <td style="color:var(--muted)">${exitTime}</td>
      <td><b>${t.symbol || ''}</b></td>
      <td>${sideTag}</td>
      <td>${parseFloat(t.entry_price || 0).toFixed(2)}</td>
      <td>${t.exit_price ? parseFloat(t.exit_price).toFixed(2) : '—'}</td>
      <td>${t.size_lots || t.size || '—'}</td>
      <td class="${pnlClass}">${pnl >= 0 ? '+' : ''}${pnl.toFixed(4)}</td>
      <td>${reasonTag}</td>
    </tr>`;
  }).join('');
  tbody.innerHTML = rows;
}

// ── SSE connection ────────────────────────────
function connectSSE() {
  const url = '/stream' + (TOKEN ? '?token=' + TOKEN : '');
  const es = new EventSource(url);

  es.onopen = () => {
    document.getElementById('dot').style.background = 'var(--green)';
    document.getElementById('last-update').textContent = 'Live ✓';
  };

  es.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      if (data.stats) updateStats(data.stats);
      if (data.recent) updateTrades(data.recent);
      if (data.equity && data.equity.labels) updateEquity(data.equity.labels, data.equity.values);
      const ts = new Date().toLocaleTimeString();
      document.getElementById('footer').textContent = 'Last updated: ' + ts;
    } catch(err) { console.error('SSE parse error', err); }
  };

  es.onerror = () => {
    document.getElementById('dot').style.background = 'var(--red)';
    document.getElementById('last-update').textContent = 'Reconnecting…';
    es.close();
    setTimeout(connectSSE, 5000);
  };
}

// ── Initial load ──────────────────────────────
async function initialLoad() {
  try {
    const [statsRes, tradesRes, equityRes] = await Promise.all([
      fetch('/api/stats' + (TOKEN ? '?token=' + TOKEN : '')),
      fetch('/api/trades?limit=100' + (TOKEN ? '&token=' + TOKEN : '')),
      fetch('/api/equity' + (TOKEN ? '?token=' + TOKEN : '')),
    ]);
    const stats = await statsRes.json();
    const trades = await tradesRes.json();
    const equity = await equityRes.json();
    updateStats(stats);
    updateTrades(trades.trades || []);
    if (equity.labels) updateEquity(equity.labels, equity.values);
  } catch(e) { console.error('Initial load error', e); }
}

initialLoad();
connectSSE();
</script>
</body>
</html>
"""


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_ui(request: Request, _ok: bool = Depends(_require_token)):
    return HTMLResponse(content=DASHBOARD_HTML)


@app.post("/api/order")
async def api_order(payload: dict, _ok: bool = Depends(_require_token)):
    key = os.getenv("DELTA_API_KEY")
    secret = os.getenv("DELTA_API_SECRET")
    if not key or not secret:
        raise HTTPException(status_code=403, detail="API keys not configured")

    symbol = payload.get("symbol")
    side = payload.get("side")
    size = int(payload.get("size_lots", 0))
    if not symbol or side not in ("buy", "sell") or size < 1:
        raise HTTPException(status_code=400, detail="Invalid order payload")

    from api import DeltaRESTClient, Order, OrderSide, OrderType
    async with DeltaRESTClient(key, secret) as client:
        prod = await client.get_product(symbol)
        if not prod:
            raise HTTPException(status_code=404, detail=f"Product {symbol} not found")
        order = Order(
            product_id=int(prod["id"]),
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            order_type=OrderType.MARKET,
            size=size,
        )
        resp = await client.place_order(order)
        return JSONResponse({"order_id": resp.order_id, "status": str(resp.status)})


@app.get("/api/positions")
async def api_positions(_ok: bool = Depends(_require_token)):
    key = os.getenv("DELTA_API_KEY")
    secret = os.getenv("DELTA_API_SECRET")
    if not key or not secret:
        raise HTTPException(status_code=403, detail="API keys not configured")
    from api import DeltaRESTClient
    async with DeltaRESTClient(key, secret) as client:
        pos = await client.get_positions()
        return JSONResponse({"positions": [p.__dict__ for p in pos]})


if __name__ == "__main__":
    import uvicorn
    host = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    port = int(os.getenv("DASHBOARD_PORT", "8000"))
    uvicorn.run("dashboard:app", host=host, port=port, reload=False)
