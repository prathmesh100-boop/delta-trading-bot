"""
dashboard.py - Live analytics dashboard for the trading bot.
"""

import asyncio
import json
import logging
import math
import os
import threading
from collections import Counter
from pathlib import Path

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

logger = logging.getLogger("dashboard")
logging.basicConfig(level=logging.INFO)

ROOT = Path(__file__).parent
DECISIONS_FILE = ROOT / "decisions.csv"
EQUITY_FILE = ROOT / "equity_curve.csv"

_file_lock = threading.Lock()

app = FastAPI(title="Trading Analytics Dashboard")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _require_token(request: Request):
    token = os.getenv("DASHBOARD_TOKEN")
    if not token:
        return True
    provided = request.headers.get("x-dashboard-token") or request.query_params.get("token")
    if provided != token:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    with _file_lock:
        try:
            return pd.read_csv(path)
        except Exception:
            logger.exception("Failed to read %s", path)
            return pd.DataFrame()


def _load_decisions() -> pd.DataFrame:
    df = _read_csv(DECISIONS_FILE)
    if df.empty:
        return df
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    return df.sort_values("timestamp", na_position="last")


def _load_equity() -> dict:
    live = _load_decisions()
    exit_df = live[live.get("event", pd.Series(dtype=str)).eq("EXIT")].copy() if not live.empty else pd.DataFrame()
    if not exit_df.empty and "equity" in exit_df.columns:
        exit_df["equity"] = pd.to_numeric(exit_df["equity"], errors="coerce")
        exit_df = exit_df.dropna(subset=["equity"])
        if not exit_df.empty:
            return {
                "labels": exit_df["timestamp"].dt.strftime("%m-%d %H:%M").tolist(),
                "values": exit_df["equity"].round(4).tolist(),
            }

    backtest = _read_csv(EQUITY_FILE)
    if backtest.empty:
        return {"labels": [], "values": []}
    labels = backtest.iloc[:, 0].astype(str).tolist()
    values = pd.to_numeric(backtest.iloc[:, -1], errors="coerce").fillna(0).round(4).tolist()
    if len(labels) > 400:
        step = max(1, math.ceil(len(labels) / 400))
        labels = labels[::step]
        values = values[::step]
    return {"labels": labels, "values": values}


def _summarize_exits(exit_df: pd.DataFrame) -> dict:
    if exit_df.empty:
        return {
            "total_pnl": 0.0,
            "total_trades": 0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "profit_factor": 0.0,
            "best_trade": 0.0,
            "worst_trade": 0.0,
            "current_equity": 0.0,
        }

    # Safely extract `pnl` as a Series even if the source is a scalar or missing.
    if exit_df is None or exit_df.empty or "pnl" not in exit_df.columns:
        pnl = pd.Series(dtype=float)
    else:
        pnl = pd.to_numeric(exit_df["pnl"], errors="coerce").fillna(0.0)

    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    gross_profit = wins.sum()
    gross_loss = abs(losses.sum())

    # Safely extract current equity (may be scalar or Series)
    if exit_df is None or exit_df.empty or "equity" not in exit_df.columns:
        current_equity = pd.Series(dtype=float)
    else:
        current_equity = pd.to_numeric(exit_df["equity"], errors="coerce").dropna()

    return {
        "total_pnl": round(float(pnl.sum()), 4),
        "total_trades": int(len(exit_df)),
        "win_rate": round(float((pnl > 0).mean() * 100), 1),
        "avg_win": round(float(wins.mean()), 4) if not wins.empty else 0.0,
        "avg_loss": round(float(losses.mean()), 4) if not losses.empty else 0.0,
        "profit_factor": round(float(gross_profit / gross_loss), 2) if gross_loss > 0 else 0.0,
        "best_trade": round(float(pnl.max()), 4),
        "worst_trade": round(float(pnl.min()), 4),
        "current_equity": round(float(current_equity.iloc[-1]), 4) if not current_equity.empty else 0.0,
    }


def _group_trade_stats(exit_df: pd.DataFrame, column: str) -> list:
  if exit_df.empty or column not in exit_df.columns:
    return []

  work = exit_df.copy()
  work[column] = work[column].fillna("").replace("", "unknown")

  # Ensure `pnl` is always a Series before using Series methods
  if work is None or work.empty or "pnl" not in work.columns:
    work["pnl"] = pd.Series(dtype=float)
  else:
    work["pnl"] = pd.to_numeric(work["pnl"], errors="coerce").fillna(0.0)

  rows = []
  for key, grp in work.groupby(column):
    pnl = grp["pnl"]
    rows.append({
      "name": str(key),
      "trades": int(len(grp)),
      "win_rate": round(float((pnl > 0).mean() * 100), 1),
      "total_pnl": round(float(pnl.sum()), 4),
      "avg_pnl": round(float(pnl.mean()), 4),
      "best": round(float(pnl.max()), 4),
      "worst": round(float(pnl.min()), 4),
    })

  rows.sort(key=lambda item: (item["total_pnl"], item["win_rate"]), reverse=True)
  return rows[:12]


def _blocker_stats(hold_df: pd.DataFrame) -> list:
    if hold_df.empty or "blockers" not in hold_df.columns:
        return []
    counts = Counter()
    for raw in hold_df["blockers"].fillna(""):
        for part in str(raw).split(","):
            blocker = part.strip()
            if blocker and blocker != "-":
                counts[blocker] += 1
    return [{"name": name, "count": count} for name, count in counts.most_common(12)]


def _hourly_stats(exit_df: pd.DataFrame) -> list:
    if exit_df.empty or "timestamp" not in exit_df.columns:
        return []
    work = exit_df.dropna(subset=["timestamp"]).copy()
    if work.empty:
        return []
    # Ensure `pnl` is a Series before using Series methods (avoid scalar numpy.float64)
    if work is None or work.empty or "pnl" not in work.columns:
      work["pnl"] = pd.Series(dtype=float)
    else:
      work["pnl"] = pd.to_numeric(work["pnl"], errors="coerce").fillna(0.0)
    work["hour"] = work["timestamp"].dt.hour
    rows = []
    for hour, grp in work.groupby("hour"):
        pnl = grp["pnl"]
        rows.append({
            "hour": f"{int(hour):02d}:00",
            "trades": int(len(grp)),
            "pnl": round(float(pnl.sum()), 4),
            "win_rate": round(float((pnl > 0).mean() * 100), 1),
        })
    rows.sort(key=lambda item: item["hour"])
    return rows


def _recent_trades(exit_df: pd.DataFrame, limit: int = 50) -> list:
    if exit_df.empty:
        return []
    view = exit_df.tail(limit).copy()
    keep = ["timestamp", "symbol", "side", "price", "pnl", "reason", "setup", "regime", "confidence", "equity", "lots"]
    keep = [col for col in keep if col in view.columns]
    view = view[keep]
    for col in ("pnl", "price", "confidence", "equity"):
        if col in view.columns:
            view[col] = pd.to_numeric(view[col], errors="coerce").fillna(0.0)
    if "timestamp" in view.columns:
        view["timestamp"] = view["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
    return view.fillna("").to_dict(orient="records")


def _analytics_payload() -> dict:
    df = _load_decisions()
    if df.empty:
        return {
            "stats": _summarize_exits(pd.DataFrame()),
            "setup_stats": [],
            "symbol_stats": [],
            "side_stats": [],
            "hourly_stats": [],
            "blocker_stats": [],
            "recent_trades": [],
            "equity": {"labels": [], "values": []},
            "last_update_ts": None,
        }

    exit_df = df[df.get("event", pd.Series(dtype=str)).eq("EXIT")].copy()
    hold_df = df[df.get("event", pd.Series(dtype=str)).eq("HOLD")].copy()
    return {
        "stats": _summarize_exits(exit_df),
        "setup_stats": _group_trade_stats(exit_df, "setup"),
        "symbol_stats": _group_trade_stats(exit_df, "symbol"),
        "side_stats": _group_trade_stats(exit_df, "side"),
        "hourly_stats": _hourly_stats(exit_df),
        "blocker_stats": _blocker_stats(hold_df),
        "recent_trades": _recent_trades(exit_df),
        "equity": _load_equity(),
        "last_update_ts": int(DECISIONS_FILE.stat().st_mtime) if DECISIONS_FILE.exists() else None,
    }


@app.get("/")
def root(_ok: bool = Depends(_require_token)):
    return JSONResponse({"status": "ok", "endpoints": ["/dashboard", "/api/analytics", "/stream"]})


@app.get("/api/analytics")
def api_analytics(_ok: bool = Depends(_require_token)):
    return JSONResponse(_analytics_payload())


async def _sse_stream(poll: float = 2.0):
    last_mtime = 0
    while True:
        try:
            mtimes = [p.stat().st_mtime if p.exists() else 0 for p in (DECISIONS_FILE, EQUITY_FILE)]
            current = max(mtimes)
            if current != last_mtime:
                last_mtime = current
                yield f"data: {json.dumps(_analytics_payload())}\n\n"
        except Exception:
            logger.exception("Dashboard stream update failed")
        await asyncio.sleep(poll)


@app.get("/stream")
def stream(_ok: bool = Depends(_require_token)):
    return StreamingResponse(_sse_stream(), media_type="text/event-stream")


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang=\"en\">
<head>
<meta charset=\"UTF-8\">
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
<title>Trading Analytics Dashboard</title>
<script src=\"https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js\"></script>
<style>
  :root {
    --bg: #081018; --panel: #101a26; --panel2: #152232; --border: #223448;
    --text: #e8f0f7; --muted: #8fa4b8; --green: #22c55e; --red: #ef4444;
    --amber: #f59e0b; --cyan: #22d3ee;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: linear-gradient(180deg, #081018, #0d1623 40%, #081018); color: var(--text); font: 13px/1.45 \"Segoe UI\", sans-serif; }
  header { padding: 18px 20px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; background: rgba(8,16,24,0.9); position: sticky; top: 0; backdrop-filter: blur(8px); z-index: 10; }
  h1 { margin: 0; font-size: 18px; color: var(--cyan); }
  .sub { color: var(--muted); font-size: 12px; }
  .wrap { padding: 18px; display: grid; gap: 16px; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
  .card, .panel { background: linear-gradient(180deg, rgba(21,34,50,0.95), rgba(16,26,38,0.95)); border: 1px solid var(--border); border-radius: 14px; box-shadow: 0 16px 40px rgba(0,0,0,0.22); }
  .card { padding: 14px; }
  .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; margin-bottom: 8px; }
  .value { font-size: 24px; font-weight: 700; }
  .positive { color: var(--green); } .negative { color: var(--red); } .neutral { color: var(--cyan); }
  .grid-2 { display: grid; grid-template-columns: 1.3fr 1fr; gap: 16px; }
  .grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; }
  .panel { padding: 14px; }
  .panel h2 { margin: 0 0 12px; font-size: 13px; color: var(--muted); text-transform: uppercase; letter-spacing: .08em; }
  .chart-box { height: 280px; }
  table { width: 100%; border-collapse: collapse; }
  th, td { padding: 8px 10px; border-bottom: 1px solid rgba(34,52,72,0.7); text-align: left; }
  th { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }
  tr:hover td { background: rgba(34,211,238,0.03); }
  .mini { max-height: 280px; overflow: auto; }
  .tag { display: inline-block; border-radius: 999px; padding: 2px 8px; font-size: 11px; font-weight: 700; }
  .long { background: rgba(34,197,94,.12); color: var(--green); }
  .short { background: rgba(239,68,68,.12); color: var(--red); }
  @media (max-width: 980px) { .grid-2, .grid-3 { grid-template-columns: 1fr; } }
</style>
</head>
<body>
  <header>
    <div>
      <h1>Trade Analytics Dashboard</h1>
      <div class=\"sub\">Setup performance, blocker counts, and live decision tracking</div>
    </div>
    <div class=\"sub\" id=\"status\">Connecting...</div>
  </header>
  <div class=\"wrap\">
    <section class=\"cards\">
      <div class=\"card\"><div class=\"label\">Net PnL</div><div class=\"value neutral\" id=\"total-pnl\">0.00</div></div>
      <div class=\"card\"><div class=\"label\">Trades</div><div class=\"value\" id=\"total-trades\">0</div></div>
      <div class=\"card\"><div class=\"label\">Win Rate</div><div class=\"value\" id=\"win-rate\">0%</div></div>
      <div class=\"card\"><div class=\"label\">Profit Factor</div><div class=\"value\" id=\"profit-factor\">0.00</div></div>
      <div class=\"card\"><div class=\"label\">Best Trade</div><div class=\"value positive\" id=\"best-trade\">0.00</div></div>
      <div class=\"card\"><div class=\"label\">Worst Trade</div><div class=\"value negative\" id=\"worst-trade\">0.00</div></div>
      <div class=\"card\"><div class=\"label\">Current Equity</div><div class=\"value\" id=\"current-equity\">0.00</div></div>
    </section>

    <section class=\"grid-2\">
      <div class=\"panel\">
        <h2>Equity Curve</h2>
        <div class=\"chart-box\"><canvas id=\"equityChart\"></canvas></div>
      </div>
      <div class=\"panel mini\">
        <h2>Top Blockers</h2>
        <table><thead><tr><th>Blocker</th><th>Count</th></tr></thead><tbody id=\"blockers-body\"></tbody></table>
      </div>
    </section>

    <section class=\"grid-3\">
      <div class=\"panel mini\">
        <h2>By Setup</h2>
        <table><thead><tr><th>Setup</th><th>Trades</th><th>PnL</th><th>WR</th></tr></thead><tbody id=\"setup-body\"></tbody></table>
      </div>
      <div class=\"panel mini\">
        <h2>By Symbol</h2>
        <table><thead><tr><th>Symbol</th><th>Trades</th><th>PnL</th><th>WR</th></tr></thead><tbody id=\"symbol-body\"></tbody></table>
      </div>
      <div class=\"panel mini\">
        <h2>By Hour</h2>
        <table><thead><tr><th>Hour</th><th>Trades</th><th>PnL</th><th>WR</th></tr></thead><tbody id=\"hour-body\"></tbody></table>
      </div>
    </section>

    <section class=\"panel mini\">
      <h2>Recent Exits</h2>
      <table>
        <thead>
          <tr><th>Time</th><th>Symbol</th><th>Side</th><th>Setup</th><th>Exit Price</th><th>PnL</th><th>Reason</th><th>Equity</th></tr>
        </thead>
        <tbody id=\"trades-body\"></tbody>
      </table>
    </section>
  </div>

<script>
const TOKEN = new URLSearchParams(location.search).get('token') || '';
const qs = TOKEN ? '?token=' + encodeURIComponent(TOKEN) : '';

const ctx = document.getElementById('equityChart').getContext('2d');
const chart = new Chart(ctx, {
  type: 'line',
  data: { labels: [], datasets: [{ data: [], borderColor: '#22d3ee', pointRadius: 0, fill: true, backgroundColor: 'rgba(34,211,238,0.08)', tension: 0.25 }] },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    plugins: { legend: { display: false } },
    scales: {
      x: { ticks: { color: '#8fa4b8', maxTicksLimit: 8 }, grid: { color: 'rgba(34,52,72,0.3)' } },
      y: { ticks: { color: '#8fa4b8' }, grid: { color: 'rgba(34,52,72,0.3)' } }
    }
  }
});

function setText(id, value, cls='') {
  const el = document.getElementById(id);
  el.textContent = value;
  if (cls) el.className = 'value ' + cls;
}

function pnlClass(v) {
  return v >= 0 ? 'positive' : 'negative';
}

function fillRows(bodyId, rows, render, emptyText='No data') {
  const body = document.getElementById(bodyId);
  if (!rows || !rows.length) {
    body.innerHTML = `<tr><td colspan=\"8\" style=\"color:#8fa4b8;padding:16px\">${emptyText}</td></tr>`;
    return;
  }
  body.innerHTML = rows.map(render).join('');
}

function updateDashboard(data) {
  const s = data.stats || {};
  setText('total-pnl', (s.total_pnl || 0).toFixed(4), pnlClass(s.total_pnl || 0));
  setText('total-trades', String(s.total_trades || 0));
  setText('win-rate', ((s.win_rate || 0).toFixed(1)) + '%', pnlClass((s.win_rate || 0) - 50));
  setText('profit-factor', (s.profit_factor || 0).toFixed(2), pnlClass((s.profit_factor || 0) - 1));
  setText('best-trade', (s.best_trade || 0).toFixed(4), 'positive');
  setText('worst-trade', (s.worst_trade || 0).toFixed(4), 'negative');
  setText('current-equity', (s.current_equity || 0).toFixed(4));

  chart.data.labels = (data.equity?.labels || []).map(v => String(v).slice(5, 16));
  chart.data.datasets[0].data = data.equity?.values || [];
  chart.update('none');

  fillRows('setup-body', data.setup_stats, row => `<tr><td>${row.name}</td><td>${row.trades}</td><td class=\"${pnlClass(row.total_pnl)}\">${row.total_pnl.toFixed(4)}</td><td>${row.win_rate.toFixed(1)}%</td></tr>`, 'No setup stats yet');
  fillRows('symbol-body', data.symbol_stats, row => `<tr><td>${row.name}</td><td>${row.trades}</td><td class=\"${pnlClass(row.total_pnl)}\">${row.total_pnl.toFixed(4)}</td><td>${row.win_rate.toFixed(1)}%</td></tr>`, 'No symbol stats yet');
  fillRows('hour-body', data.hourly_stats, row => `<tr><td>${row.hour}</td><td>${row.trades}</td><td class=\"${pnlClass(row.pnl)}\">${row.pnl.toFixed(4)}</td><td>${row.win_rate.toFixed(1)}%</td></tr>`, 'No hourly stats yet');
  fillRows('blockers-body', data.blocker_stats, row => `<tr><td>${row.name}</td><td>${row.count}</td></tr>`, 'No blockers recorded yet');
  fillRows('trades-body', data.recent_trades, row => `<tr><td>${row.timestamp || ''}</td><td>${row.symbol || ''}</td><td><span class=\"tag ${row.side === 'long' ? 'long' : 'short'}\">${(row.side || '').toUpperCase()}</span></td><td>${row.setup || ''}</td><td>${Number(row.price || 0).toFixed(4)}</td><td class=\"${pnlClass(Number(row.pnl || 0))}\">${Number(row.pnl || 0).toFixed(4)}</td><td>${row.reason || ''}</td><td>${Number(row.equity || 0).toFixed(4)}</td></tr>`, 'No exits recorded yet');
}

async function initialLoad() {
  const res = await fetch('/api/analytics' + qs);
  const data = await res.json();
  updateDashboard(data);
}

function connect() {
  const stream = new EventSource('/stream' + qs);
  stream.onopen = () => { document.getElementById('status').textContent = 'Live'; };
  stream.onmessage = (event) => {
    updateDashboard(JSON.parse(event.data));
    document.getElementById('status').textContent = 'Live update ' + new Date().toLocaleTimeString();
  };
  stream.onerror = () => {
    document.getElementById('status').textContent = 'Reconnecting...';
    stream.close();
    setTimeout(connect, 3000);
  };
}

initialLoad().then(connect);
</script>
</body>
</html>
"""


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_ui(_ok: bool = Depends(_require_token)):
    return HTMLResponse(content=DASHBOARD_HTML)


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    port = int(os.getenv("DASHBOARD_PORT", "8000"))
    uvicorn.run("dashboard:app", host=host, port=port, reload=False)
