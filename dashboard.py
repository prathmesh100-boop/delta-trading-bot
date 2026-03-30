"""
dashboard.py — FastAPI dashboard for trading bot

Production-minded features:
- Token-based access if `DASHBOARD_TOKEN` set in env
- JSON API endpoints for stats and trades
- Simple HTML dashboard with auto-refresh and basic styling
- Safe CSV reads with minimal locking and error handling
- Uvicorn run block for straightforward deployment
"""
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path
from typing import Optional
import pandas as pd
import os
import time
import logging

logger = logging.getLogger("dashboard")
logging.basicConfig(level=logging.INFO)

ROOT = Path(__file__).parent
TRADE_FILE = ROOT / "trade_history.csv"

# Simple file read lock to avoid concurrent reads/writes races
_file_lock = None
try:
    import threading
    _file_lock = threading.Lock()
except Exception:
    _file_lock = None

app = FastAPI(title="Trading Dashboard")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _load_trades() -> pd.DataFrame:
    if not TRADE_FILE.exists():
        return pd.DataFrame()

    if _file_lock:
        _file_lock.acquire()
    try:
        df = pd.read_csv(TRADE_FILE)
        return df
    except Exception as exc:
        logger.exception("Failed to read trades: %s", exc)
        return pd.DataFrame()
    finally:
        if _file_lock:
            _file_lock.release()


def _compute_stats(df: pd.DataFrame) -> dict:
    if df.empty:
        return {
            "total_pnl": 0.0,
            "total_trades": 0,
            "win_rate": 0.0,
            "last_update_ts": None,
        }

    total_pnl = float(df.get("pnl", pd.Series(dtype=float)).sum())
    total_trades = int(len(df))
    win_rate = float((df.get("pnl", 0) > 0).mean() * 100)
    mtime = TRADE_FILE.stat().st_mtime
    return {
        "total_pnl": round(total_pnl, 2),
        "total_trades": total_trades,
        "win_rate": round(win_rate, 2),
        "last_update_ts": int(mtime),
    }


def _require_token(request: Request):
    token = os.getenv("DASHBOARD_TOKEN")
    if not token:
        return True
    # Accept either header `x-dashboard-token` or query `token`
    provided = request.headers.get("x-dashboard-token") or request.query_params.get("token")
    if provided != token:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True


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


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, _ok: bool = Depends(_require_token)):
    df = _load_trades()

    stats = _compute_stats(df)

    if df.empty:
        body = "<h2>No trades yet...</h2>"
        return HTMLResponse(content=body)

    # Keep the page simple and server-rendered; client auto-refreshes every 5s
    recent_html = df.tail(50).to_html(index=False, classes="trade-table")
    last_update = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stats['last_update_ts'])) if stats["last_update_ts"] else "-"

    html = f"""
    <html>
    <head>
        <title>Trading Dashboard</title>
        <meta http-equiv="refresh" content="5">
        <style>
            body {{ font-family: Arial, sans-serif; padding: 20px; background:#fafafa; color:#222 }}
            h1 {{ color: #111 }}
            .card {{ display:inline-block; padding:14px 18px; margin:8px; border-radius:8px; background:#fff; box-shadow:0 1px 4px rgba(0,0,0,0.06) }}
            .trade-table {{ border-collapse: collapse; width:100%; max-width:1200px }}
            .trade-table th, .trade-table td {{ border:1px solid #eee; padding:6px 8px; text-align:left }}
        </style>
    </head>
    <body>
        <h1>📊 Trading Dashboard</h1>
        <div>
            <div class="card"><strong>Total PnL</strong><div>${stats['total_pnl']:.2f}</div></div>
            <div class="card"><strong>Total Trades</strong><div>{stats['total_trades']}</div></div>
            <div class="card"><strong>Win Rate</strong><div>{stats['win_rate']:.2f}%</div></div>
            <div class="card"><strong>Last Update</strong><div>{last_update}</div></div>
        </div>

        <h2>Recent Trades</h2>
        {recent_html}
    </body>
    </html>
    """
    return HTMLResponse(content=html)


if __name__ == "__main__":
    import uvicorn
    host = os.getenv("DASHBOARD_HOST", "16.171.148.214")
    port = int(os.getenv("DASHBOARD_PORT", "8000"))
    workers = int(os.getenv("DASHBOARD_WORKERS", "1"))
    uvicorn.run("dashboard:app", host=host, port=port, workers=workers)
