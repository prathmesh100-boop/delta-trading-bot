"""
dashboard.py — Backend-only FastAPI endpoints for trading bot

Provides JSON APIs and an SSE stream for programmatic consumption.
No frontend/UI code included.
"""
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path
from typing import Optional
import pandas as pd
import os
import time
import logging
import asyncio

logger = logging.getLogger("dashboard")
logging.basicConfig(level=logging.INFO)

ROOT = Path(__file__).parent
TRADE_FILE = ROOT / "trade_history.csv"

# Lightweight file lock
try:
    import threading
    _file_lock = threading.Lock()
except Exception:
    _file_lock = None

app = FastAPI(title="Trading Dashboard API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def _load_trades() -> pd.DataFrame:
    if not TRADE_FILE.exists():
        return pd.DataFrame()

    if _file_lock:
        _file_lock.acquire()
    try:
        return pd.read_csv(TRADE_FILE)
    except Exception:
        logger.exception("Failed to read trade file")
        return pd.DataFrame()
    finally:
        if _file_lock:
            _file_lock.release()


def _compute_stats(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"total_pnl": 0.0, "total_trades": 0, "win_rate": 0.0, "last_update_ts": None}
    total_pnl = float(df.get("pnl", pd.Series(dtype=float)).sum())
    total_trades = int(len(df))
    win_rate = float((df.get("pnl", 0) > 0).mean() * 100)
    mtime = TRADE_FILE.stat().st_mtime
    return {"total_pnl": round(total_pnl, 2), "total_trades": total_trades, "win_rate": round(win_rate, 2), "last_update_ts": int(mtime)}


def _tail_lines(path: Path, n: int = 200):
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            from collections import deque
            return list(deque(fh, maxlen=n))
    except Exception:
        return []


async def _stream_trades(poll_interval: float = 2.0):
    """SSE generator that yields JSON payload when trade file changes."""
    import json
    last_mtime = 0
    while True:
        try:
            if TRADE_FILE.exists():
                mtime = TRADE_FILE.stat().st_mtime
                if mtime != last_mtime:
                    last_mtime = mtime
                    df = _load_trades()
                    payload = {"stats": _compute_stats(df), "recent": df.tail(50).to_dict(orient="records")}
                    yield f"data: {json.dumps(payload)}\n\n"
        except Exception:
            pass
        await asyncio.sleep(poll_interval)


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
    return JSONResponse(content={"status": "ok", "endpoints": ["/api/stats", "/api/trades", "/api/logs", "/stream", "/api/order", "/api/positions"]})


@app.get("/api/stats")
def api_stats(_ok: bool = Depends(_require_token)):
    df = _load_trades()
    return JSONResponse(content=_compute_stats(df))


@app.get("/api/trades")
def api_trades(limit: Optional[int] = 100, _ok: bool = Depends(_require_token)):
    df = _load_trades()
    if df.empty:
        return JSONResponse(content={"trades": [], "count": 0})
    df = df.tail(limit)
    payload = df.fillna("").to_dict(orient="records")
    return JSONResponse(content={"trades": payload, "count": len(payload)})


@app.get("/api/logs")
def api_logs(lines: int = 200, _ok: bool = Depends(_require_token)):
    logs = _tail_lines(TRADE_FILE, n=lines)
    return JSONResponse(content={"lines": logs, "count": len(logs)})


@app.get("/stream")
def stream(request: Request, _ok: bool = Depends(_require_token)):
    return StreamingResponse(_stream_trades(), media_type="text/event-stream")


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_ui(request: Request, _ok: bool = Depends(_require_token)):
    return HTMLResponse(content="""
<!DOCTYPE html>
<html>
<head>
<title>Trading Terminal</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body { font-family: Arial; background:#0f172a; color:#e2e8f0; padding:20px }
h1 { color:#38bdf8 }
.card { display:inline-block; background:#1e293b; padding:15px; margin:10px; border-radius:10px }
table { width:100%; border-collapse:collapse; margin-top:20px }
th, td { padding:8px; border-bottom:1px solid #334155 }
.green { color:#22c55e }
.red { color:#ef4444 }
button { padding:8px 12px; margin:5px; border:none; border-radius:5px; cursor:pointer }
.buy { background:#22c55e }
.sell { background:#ef4444 }
</style>
</head>
<body>

<h1>📊 Trading Terminal</h1>

<div id="stats"></div>

<h2>📈 Trades</h2>
<table id="trades"></table>

<h2>⚡ Quick Order</h2>
<button class="buy" onclick="placeOrder('buy')">BUY</button>
<button class="sell" onclick="placeOrder('sell')">SELL</button>

<script>
const TOKEN = new URLSearchParams(window.location.search).get("token");

async function loadStats() {
    const res = await fetch('/api/stats?token=' + TOKEN);
    const data = await res.json();

    document.getElementById('stats').innerHTML = `
        <div class="card">PnL: $${data.total_pnl}</div>
        <div class="card">Trades: ${data.total_trades}</div>
        <div class="card">Win Rate: ${data.win_rate}%</div>
    `;
}

async function loadTrades() {
    const res = await fetch('/api/trades?token=' + TOKEN);
    const data = await res.json();

    let html = "<tr><th>Symbol</th><th>Side</th><th>PnL</th></tr>";

    data.trades.forEach(t => {
        html += `
        <tr>
            <td>${t.symbol}</td>
            <td>${t.side}</td>
            <td class="${t.pnl >= 0 ? 'green' : 'red'}">${t.pnl}</td>
        </tr>`;
    });

    document.getElementById('trades').innerHTML = html;
}

async function placeOrder(side) {
    await fetch('/api/order?token=' + TOKEN, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            symbol: "BTC_USDT",
            side: side,
            size_lots: 1
        })
    });
    alert("Order sent");
}

setInterval(() => {
    loadStats();
    loadTrades();
}, 3000);

loadStats();
loadTrades();

</script>

</body>
</html>
""")


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
            raise HTTPException(status_code=404, detail="Product not found")
        product_id = int(prod.get("id"))

        order = Order(
            product_id=product_id,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            order_type=OrderType.MARKET,
            size=size,
        )
        resp = await client.place_order(order)
        return JSONResponse(content={"order_id": resp.order_id, "status": str(resp.status)})


@app.get("/api/positions")
async def api_positions(_ok: bool = Depends(_require_token)):
    key = os.getenv("DELTA_API_KEY")
    secret = os.getenv("DELTA_API_SECRET")
    if not key or not secret:
        raise HTTPException(status_code=403, detail="API keys not configured")
    from api import DeltaRESTClient
    async with DeltaRESTClient(key, secret) as client:
        pos = await client.get_positions()
        return JSONResponse(content={"positions": [p.__dict__ for p in pos]})


if __name__ == "__main__":
    import uvicorn
    host = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    port = int(os.getenv("DASHBOARD_PORT", "8000"))
    workers = int(os.getenv("DASHBOARD_WORKERS", "1"))
    uvicorn.run("dashboard:app", host=host, port=port, workers=workers)
