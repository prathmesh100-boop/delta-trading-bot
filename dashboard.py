"""
dashboard.py - V8 Trading Dashboard

Live dashboard with:
  - performance stats and equity curve
  - live coin cards with current values
  - multi-coin price graph
  - open positions with long/short and unrealized PnL
  - setup/grade analytics
  - recent trades table
"""
import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

logger = logging.getLogger("dashboard")
logging.basicConfig(level=logging.INFO)

ROOT = Path(__file__).parent
TRADE_FILE = ROOT / "trade_history.csv"
EQUITY_FILE = ROOT / "equity_curve.csv"

_file_lock = threading.Lock()

app = FastAPI(title="Trading Dashboard V8")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _dashboard_symbols() -> List[str]:
    raw = os.getenv("DASHBOARD_SYMBOLS", "BTCUSD,ETHUSD,SOLUSD")
    seen = set()
    symbols: List[str] = []
    for item in raw.split(","):
        symbol = item.strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    return symbols


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
        if len(df) > 500:
            step = len(df) // 500
            df = df.iloc[::step]
        return df
    except Exception:
        return pd.DataFrame()


def _compute_stats(df: pd.DataFrame) -> dict:
    if df.empty:
        return {
            "total_pnl": 0.0,
            "total_trades": 0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "profit_factor": 0.0,
            "setup_stats": {},
            "grade_stats": {},
        }

    pnl_col = df["pnl"] if "pnl" in df.columns else pd.Series(dtype=float)
    wins = pnl_col[pnl_col > 0]
    losses = pnl_col[pnl_col <= 0]
    gross_profit = wins.sum()
    gross_loss = abs(losses.sum())

    setup_stats = {}
    if "setup_type" in df.columns:
        for setup, group in df.groupby("setup_type"):
            n = len(group)
            w = (group["pnl"] > 0).sum() if "pnl" in group.columns else 0
            tot = group["pnl"].sum() if "pnl" in group.columns else 0.0
            setup_stats[setup] = {
                "trades": n,
                "win_rate": round(w / n * 100, 1) if n else 0,
                "total_pnl": round(float(tot), 4),
                "avg_pnl": round(float(tot / n), 4) if n else 0,
            }

    grade_stats = {}
    if "entry_grade" in df.columns:
        for grade, group in df.groupby("entry_grade"):
            n = len(group)
            w = (group["pnl"] > 0).sum() if "pnl" in group.columns else 0
            tot = group["pnl"].sum() if "pnl" in group.columns else 0.0
            grade_stats[str(grade)] = {
                "trades": n,
                "win_rate": round(w / n * 100, 1) if n else 0,
                "total_pnl": round(float(tot), 4),
            }

    return {
        "total_pnl": round(float(pnl_col.sum()), 4),
        "total_trades": int(len(df)),
        "win_rate": round(float((pnl_col > 0).mean() * 100), 1) if len(df) else 0.0,
        "avg_win": round(float(wins.mean()), 4) if len(wins) else 0.0,
        "avg_loss": round(float(losses.mean()), 4) if len(losses) else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else 0.0,
        "setup_stats": setup_stats,
        "grade_stats": grade_stats,
    }


def _require_token(request: Request):
    token = os.getenv("DASHBOARD_TOKEN")
    if not token:
        return True
    provided = request.headers.get("x-dashboard-token") or request.query_params.get("token")
    if provided != token:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True


@app.get("/")
def root(_ok: bool = Depends(_require_token)):
    return JSONResponse(
        {
            "status": "ok",
            "version": "V8",
            "endpoints": [
                "/dashboard",
                "/api/stats",
                "/api/trades",
                "/api/equity",
                "/api/market-overview",
                "/api/market-chart",
                "/api/positions",
                "/stream",
            ],
        }
    )


@app.get("/api/stats")
def api_stats(_ok: bool = Depends(_require_token)):
    return JSONResponse(_compute_stats(_load_trades()))


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


@app.get("/api/market-overview")
async def api_market_overview(_ok: bool = Depends(_require_token)):
    key = os.getenv("DELTA_API_KEY", "").strip()
    secret = os.getenv("DELTA_API_SECRET", "").strip()
    symbols = _dashboard_symbols()
    if not key or not secret:
        return JSONResponse({"symbols": symbols, "tickers": [], "positions": [], "error": "API keys not configured"})

    from api import DeltaRESTClient

    async with DeltaRESTClient(key, secret) as client:
        tickers, positions = await asyncio.gather(
            asyncio.gather(*(client.get_ticker(symbol) for symbol in symbols)),
            client.get_positions(),
        )
    return JSONResponse(
        {
            "symbols": symbols,
            "tickers": [ticker.__dict__ for ticker in tickers],
            "positions": [position.__dict__ for position in positions],
            "updated_at": datetime.utcnow().isoformat(),
        }
    )


@app.get("/api/market-chart")
async def api_market_chart(_ok: bool = Depends(_require_token)):
    key = os.getenv("DELTA_API_KEY", "").strip()
    secret = os.getenv("DELTA_API_SECRET", "").strip()
    symbols = _dashboard_symbols()
    if not key or not secret:
        return JSONResponse({"labels": [], "datasets": [], "error": "API keys not configured"})

    from api import DeltaRESTClient

    end = int(time.time())
    start = end - 15 * 60 * 60
    palette = ["#38bdf8", "#22c55e", "#f97316", "#a78bfa", "#fbbf24", "#ef4444"]

    async with DeltaRESTClient(key, secret) as client:
        candles_by_symbol = await asyncio.gather(
            *(client.get_ohlcv(symbol, 15, start, end) for symbol in symbols)
        )

    labels = []
    datasets = []
    for idx, (symbol, candles) in enumerate(zip(symbols, candles_by_symbol)):
        if not candles:
            continue
        if not labels:
            labels = [datetime.utcfromtimestamp(c.timestamp).strftime("%H:%M") for c in candles]
        datasets.append(
            {
                "label": symbol,
                "data": [round(float(c.close), 4) for c in candles],
                "borderColor": palette[idx % len(palette)],
            }
        )
    return JSONResponse({"labels": labels, "datasets": datasets, "updated_at": datetime.utcnow().isoformat()})


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


DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Delta Trading Terminal V8</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0a0e1a; --bg2: #111827; --border: #2d3748;
    --text: #e2e8f0; --muted: #94a3b8; --green: #22c55e;
    --red: #ef4444; --blue: #38bdf8; --yellow: #fbbf24;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); font-size: 13px; }
  header { background: var(--bg2); border-bottom: 1px solid var(--border); padding: 12px 20px; display: flex; align-items: center; gap: 16px; }
  header h1 { font-size: 15px; font-weight: 700; color: var(--blue); }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--green); animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; padding: 14px; }
  .market-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; }
  .card { background: var(--bg2); border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
  .card .label { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: .5px; margin-bottom: 5px; }
  .card .value { font-size: 20px; font-weight: 700; }
  .coin-symbol { font-size: 15px; font-weight: 700; color: var(--text); }
  .coin-sub { color: var(--muted); font-size: 11px; margin-top: 2px; }
  .coin-price { font-size: 22px; font-weight: 700; margin-top: 10px; }
  .coin-meta { display: flex; justify-content: space-between; gap: 8px; margin-top: 10px; color: var(--muted); font-size: 11px; }
  .positive { color: var(--green); } .negative { color: var(--red); } .neutral { color: var(--blue); }
  .section { padding: 0 14px 14px; }
  .section h2 { font-size: 11px; font-weight: 600; color: var(--muted); margin-bottom: 8px; text-transform: uppercase; letter-spacing: .5px; }
  .chart-wrap { background: var(--bg2); border: 1px solid var(--border); border-radius: 8px; padding: 14px; height: 220px; position: relative; }
  .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 14px; }
  .analytics-panel { background: var(--bg2); border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
  .analytics-panel h3 { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; margin-bottom: 8px; }
  .analytics-row { display: flex; justify-content: space-between; align-items: center; padding: 5px 0; border-bottom: 1px solid rgba(45,55,72,.5); font-size: 12px; }
  .analytics-row:last-child { border-bottom: none; }
  .badge { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: 700; }
  .badge-A { background: rgba(34,197,94,.2); color: var(--green); }
  .badge-B { background: rgba(56,189,248,.2); color: var(--blue); }
  .badge-C { background: rgba(251,191,36,.2); color: var(--yellow); }
  .badge-D { background: rgba(239,68,68,.2); color: var(--red); }
  .table-wrap { overflow-x:auto; background:var(--bg2); border:1px solid var(--border); border-radius:8px; padding:4px; }
  table { width: 100%; border-collapse: collapse; }
  thead th { padding: 7px 8px; text-align: left; color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: .5px; border-bottom: 1px solid var(--border); }
  tbody tr { border-bottom: 1px solid rgba(45,55,72,.4); }
  tbody tr:hover { background: rgba(56,189,248,.04); }
  tbody td { padding: 6px 8px; }
  .tag { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: 600; }
  .tag-long { background: rgba(34,197,94,.15); color: var(--green); }
  .tag-short { background: rgba(239,68,68,.15); color: var(--red); }
  .tag-tp { background: rgba(34,197,94,.15); color: var(--green); }
  .tag-sl { background: rgba(239,68,68,.15); color: var(--red); }
  .progress-bar { height: 4px; border-radius: 2px; background: var(--border); overflow: hidden; margin-top: 4px; }
  .progress-fill { height: 100%; border-radius: 2px; transition: width .3s; }
  @media(max-width:600px) { .two-col { grid-template-columns: 1fr; } .grid { grid-template-columns: 1fr 1fr; } }
</style>
</head>
<body>
<header>
  <div class="status-dot" id="dot"></div>
  <h1>Delta Trading Terminal V8</h1>
  <span style="color:var(--muted);font-size:11px;margin-left:auto" id="last-update">Connecting...</span>
</header>

<div class="grid">
  <div class="card"><div class="label">Total PnL</div><div class="value neutral" id="s-pnl">-</div></div>
  <div class="card"><div class="label">Trades</div><div class="value neutral" id="s-trades">-</div></div>
  <div class="card"><div class="label">Win Rate</div><div class="value" id="s-wr">-</div><div class="progress-bar"><div class="progress-fill" id="wr-bar" style="background:var(--green);width:0%"></div></div></div>
  <div class="card"><div class="label">Profit Factor</div><div class="value" id="s-pf">-</div></div>
  <div class="card"><div class="label">Avg Win</div><div class="value positive" id="s-aw">-</div></div>
  <div class="card"><div class="label">Avg Loss</div><div class="value negative" id="s-al">-</div></div>
</div>

<div class="section">
  <h2>Market Overview</h2>
  <div class="market-grid" id="market-cards">
    <div class="card"><div style="color:var(--muted);font-size:11px">Loading live prices...</div></div>
  </div>
</div>

<div class="section">
  <h2>Coin Price Graph</h2>
  <div class="chart-wrap"><canvas id="market-chart"></canvas></div>
</div>

<div class="section">
  <h2>Equity Curve</h2>
  <div class="chart-wrap"><canvas id="equity-chart"></canvas></div>
</div>

<div class="section">
  <h2>Open Positions</h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Symbol</th><th>Side</th><th>Size</th><th>Entry</th><th>Mark</th><th>uPnL</th><th>Margin</th></tr></thead>
      <tbody id="positions-body">
        <tr><td colspan="7" style="text-align:center;padding:20px;color:var(--muted)">No open positions</td></tr>
      </tbody>
    </table>
  </div>
</div>

<div class="section">
  <div class="two-col">
    <div class="analytics-panel">
      <h3>By Setup Type</h3>
      <div id="setup-stats"><div style="color:var(--muted);font-size:11px">No data yet</div></div>
    </div>
    <div class="analytics-panel">
      <h3>By Entry Grade</h3>
      <div id="grade-stats"><div style="color:var(--muted);font-size:11px">No data yet</div></div>
    </div>
  </div>
</div>

<div class="section">
  <h2>Recent Trades</h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Setup</th><th>Grade</th><th>Entry</th><th>Exit</th><th>PnL</th><th>Reason</th></tr></thead>
      <tbody id="trades-body">
        <tr><td colspan="9" style="text-align:center;padding:20px;color:var(--muted)">Loading...</td></tr>
      </tbody>
    </table>
  </div>
</div>

<div style="color:var(--muted);font-size:11px;padding:4px 14px 10px" id="footer">Last updated: -</div>

<script>
const TOKEN = new URLSearchParams(location.search).get('token') || '';

const equityCtx = document.getElementById('equity-chart').getContext('2d');
const equityChart = new Chart(equityCtx, {
  type: 'line',
  data: { labels: [], datasets: [{ label: 'Equity', data: [], borderColor: '#38bdf8', backgroundColor: 'rgba(56,189,248,.06)', borderWidth: 1.5, pointRadius: 0, tension: 0.2, fill: true }]},
  options: { responsive: true, maintainAspectRatio: false, animation: false, plugins: { legend: { display: false } }, scales: { x: { display: false }, y: { grid: { color: 'rgba(255,255,255,.05)' }, ticks: { color: '#94a3b8', font: { size: 10 } } } } }
});

const marketCtx = document.getElementById('market-chart').getContext('2d');
const marketChart = new Chart(marketCtx, {
  type: 'line',
  data: { labels: [], datasets: [] },
  options: {
    responsive: true, maintainAspectRatio: false, animation: false,
    plugins: { legend: { labels: { color: '#94a3b8', boxWidth: 10 } } },
    scales: {
      x: { ticks: { color: '#94a3b8', font: { size: 10 }, maxTicksLimit: 8 }, grid: { color: 'rgba(255,255,255,.04)' } },
      y: { ticks: { color: '#94a3b8', font: { size: 10 } }, grid: { color: 'rgba(255,255,255,.05)' } }
    }
  }
});

function updateEquity(labels, values) {
  const n = Math.max(1, Math.floor(labels.length / 6));
  equityChart.data.labels = labels.map((l, i) => i % n === 0 ? String(l).substring(0, 10) : '');
  equityChart.data.datasets[0].data = values;
  const start = values[0] || 0;
  const end = values[values.length - 1] || 0;
  const color = end >= start ? '#22c55e' : '#ef4444';
  equityChart.data.datasets[0].borderColor = color;
  equityChart.data.datasets[0].backgroundColor = end >= start ? 'rgba(34,197,94,.06)' : 'rgba(239,68,68,.06)';
  equityChart.update('none');
}

function updateMarketCards(items) {
  const el = document.getElementById('market-cards');
  if (!items || !items.length) {
    el.innerHTML = '<div class="card"><div style="color:var(--muted);font-size:11px">No live market data</div></div>';
    return;
  }
  el.innerHTML = items.map(t => {
    const funding = parseFloat(t.funding_rate || 0);
    const fundingClass = funding >= 0 ? 'positive' : 'negative';
    return `<div class="card"><div class="coin-symbol">${t.symbol}</div><div class="coin-sub">Mark ${parseFloat(t.mark_price || 0).toFixed(2)}</div><div class="coin-price">${parseFloat(t.last_price || 0).toFixed(2)}</div><div class="coin-meta"><span>Funding <span class="${fundingClass}">${funding.toFixed(5)}</span></span><span>OI ${Math.round(parseFloat(t.open_interest || 0)).toLocaleString()}</span></div></div>`;
  }).join('');
}

function updateMarketChart(payload) {
  marketChart.data.labels = payload.labels || [];
  marketChart.data.datasets = (payload.datasets || []).map(ds => ({
    label: ds.label,
    data: ds.data,
    borderColor: ds.borderColor,
    backgroundColor: ds.borderColor + '22',
    borderWidth: 1.7,
    pointRadius: 0,
    tension: 0.2,
    fill: false
  }));
  marketChart.update('none');
}

function updatePositions(positions) {
  const tbody = document.getElementById('positions-body');
  if (!positions || !positions.length) {
    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:20px;color:var(--muted)">No open positions</td></tr>';
    return;
  }
  tbody.innerHTML = positions.map(p => {
    const pnl = parseFloat(p.unrealized_pnl || 0);
    const pnlClass = pnl >= 0 ? 'positive' : 'negative';
    return `<tr><td><b>${p.symbol || ''}</b></td><td><span class="tag tag-${p.side}">${(p.side || '').toUpperCase()}</span></td><td>${parseFloat(p.size || 0).toFixed(4)}</td><td>${parseFloat(p.entry_price || 0).toFixed(2)}</td><td>${parseFloat(p.mark_price || 0).toFixed(2)}</td><td class="${pnlClass}">${pnl >= 0 ? '+' : ''}${pnl.toFixed(4)}</td><td>${parseFloat(p.margin || 0).toFixed(2)}</td></tr>`;
  }).join('');
}

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

  const ss = s.setup_stats || {};
  const ssEl = document.getElementById('setup-stats');
  if (Object.keys(ss).length === 0) {
    ssEl.innerHTML = '<div style="color:var(--muted);font-size:11px">No data yet</div>';
  } else {
    ssEl.innerHTML = Object.entries(ss).map(([setup, d]) => {
      const pnlClass = d.total_pnl >= 0 ? 'positive' : 'negative';
      return `<div class="analytics-row"><span style="color:var(--text)">${setup.replace('_', ' ')}</span><span style="color:var(--muted);font-size:11px">${d.trades}T ${d.win_rate}%wr</span><span class="${pnlClass}">${d.total_pnl >= 0 ? '+' : ''}${d.total_pnl.toFixed(3)}</span></div>`;
    }).join('');
  }

  const gs = s.grade_stats || {};
  const gsEl = document.getElementById('grade-stats');
  if (Object.keys(gs).length === 0) {
    gsEl.innerHTML = '<div style="color:var(--muted);font-size:11px">No data yet</div>';
  } else {
    const gradeOrder = ['A', 'B', 'C', 'D'];
    const sorted = gradeOrder.filter(g => gs[g]).map(g => [g, gs[g]]);
    gsEl.innerHTML = sorted.map(([grade, d]) => {
      const pnlClass = d.total_pnl >= 0 ? 'positive' : 'negative';
      return `<div class="analytics-row"><span class="badge badge-${grade}">${grade}</span><span style="color:var(--muted);font-size:11px">${d.trades} trades ${d.win_rate}% wr</span><span class="${pnlClass}">${d.total_pnl >= 0 ? '+' : ''}${d.total_pnl.toFixed(3)}</span></div>`;
    }).join('');
  }
}

function updateTrades(trades) {
  const tbody = document.getElementById('trades-body');
  if (!trades || !trades.length) {
    tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;padding:20px;color:var(--muted)">No trades yet</td></tr>';
    return;
  }
  tbody.innerHTML = [...trades].reverse().map(t => {
    const pnl = parseFloat(t.pnl) || 0;
    const pnlClass = pnl >= 0 ? 'positive' : 'negative';
    let reasonTag = t.exit_reason || '';
    if (reasonTag.includes('sl')) reasonTag = '<span class="tag tag-sl">SL</span>';
    else if (reasonTag.includes('tp')) reasonTag = '<span class="tag tag-tp">TP</span>';
    const exitTime = t.exit_time ? String(t.exit_time).substring(0, 16) : '-';
    const grade = t.entry_grade || '?';
    const sideTag = `<span class="tag tag-${t.side}">${(t.side || '').toUpperCase()}</span>`;
    return `<tr><td style="color:var(--muted)">${exitTime}</td><td><b>${t.symbol || ''}</b></td><td>${sideTag}</td><td style="color:var(--muted);font-size:11px">${String(t.setup_type || '').replace('_', ' ')}</td><td><span class="badge badge-${grade}">${grade}</span></td><td>${parseFloat(t.entry_price || 0).toFixed(2)}</td><td>${t.exit_price ? parseFloat(t.exit_price).toFixed(2) : '-'}</td><td class="${pnlClass}">${pnl >= 0 ? '+' : ''}${pnl.toFixed(4)}</td><td>${reasonTag}</td></tr>`;
  }).join('');
}

async function refreshLivePanels() {
  try {
    const [marketRes, chartRes] = await Promise.all([
      fetch('/api/market-overview' + (TOKEN ? '?token=' + TOKEN : '')),
      fetch('/api/market-chart' + (TOKEN ? '?token=' + TOKEN : ''))
    ]);
    const market = await marketRes.json();
    const chart = await chartRes.json();
    updateMarketCards(market.tickers || []);
    updatePositions(market.positions || []);
    updateMarketChart(chart);
  } catch (e) {
    console.error('Live panel refresh error', e);
  }
}

function connectSSE() {
  const url = '/stream' + (TOKEN ? '?token=' + TOKEN : '');
  const es = new EventSource(url);
  es.onopen = () => {
    document.getElementById('dot').style.background = 'var(--green)';
    document.getElementById('last-update').textContent = 'Live';
  };
  es.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      if (data.stats) updateStats(data.stats);
      if (data.recent) updateTrades(data.recent);
      if (data.equity && data.equity.labels) updateEquity(data.equity.labels, data.equity.values);
      document.getElementById('footer').textContent = 'Last updated: ' + new Date().toLocaleTimeString();
    } catch(err) { console.error('SSE parse error', err); }
  };
  es.onerror = () => {
    document.getElementById('dot').style.background = 'var(--red)';
    document.getElementById('last-update').textContent = 'Reconnecting...';
    es.close();
    setTimeout(connectSSE, 5000);
  };
}

async function initialLoad() {
  try {
    const [statsRes, tradesRes, equityRes] = await Promise.all([
      fetch('/api/stats' + (TOKEN ? '?token=' + TOKEN : '')),
      fetch('/api/trades?limit=100' + (TOKEN ? '&token=' + TOKEN : '')),
      fetch('/api/equity' + (TOKEN ? '?token=' + TOKEN : ''))
    ]);
    const stats = await statsRes.json();
    const trades = await tradesRes.json();
    const equity = await equityRes.json();
    updateStats(stats);
    updateTrades(trades.trades || []);
    if (equity.labels) updateEquity(equity.labels, equity.values);
    await refreshLivePanels();
  } catch(e) { console.error('Initial load error', e); }
}

initialLoad();
connectSSE();
setInterval(refreshLivePanels, 15000);
</script>
</body>
</html>
"""


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_ui(request: Request, _ok: bool = Depends(_require_token)):
    return HTMLResponse(content=DASHBOARD_HTML)


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
