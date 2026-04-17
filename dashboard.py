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
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from delta_bot.config import StorageConfig
from delta_bot.storage import AuditStore

logger = logging.getLogger("dashboard")
logging.basicConfig(level=logging.INFO)

ROOT = Path(__file__).parent
TRADE_FILE = ROOT / "trade_history.csv"
EQUITY_FILE = ROOT / "equity_curve.csv"

_file_lock = threading.Lock()

app = FastAPI(title="Trading Dashboard V8")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _dashboard_symbols() -> List[str]:
    raw = os.getenv("DASHBOARD_SYMBOLS", "BTCUSD,ETHUSD,SOLUSD,BNBUSD,XRPUSD,AVAXUSD")
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


def _candidate_audit_db_paths() -> List[Path]:
    cfg = StorageConfig(root=ROOT / ".bot_data")
    candidates = [cfg.database_path]
    bot_data = cfg.root
    if bot_data.exists():
        candidates.extend(sorted(bot_data.glob("system-recovery-*.db"), key=lambda p: p.stat().st_mtime, reverse=True))
        candidates.extend(sorted(bot_data.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True))

    seen = set()
    ordered: List[Path] = []
    for path in candidates:
        resolved = str(path.resolve())
        if resolved in seen or not path.exists():
            continue
        seen.add(resolved)
        ordered.append(path)
    return ordered


def _audit_db_score(path: Path) -> tuple:
    try:
        conn = sqlite3.connect(path, timeout=2)
        cur = conn.cursor()
        tables = {row[0] for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"audit_events", "runtime_state", "trade_audit"}
        has_schema = int(required.issubset(tables))
        if not has_schema:
            return (0, 0, 0.0)
        event_count = cur.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        runtime_count = cur.execute("SELECT COUNT(*) FROM runtime_state").fetchone()[0]
        trade_count = cur.execute("SELECT COUNT(*) FROM trade_audit").fetchone()[0]
        return (1, int(event_count + runtime_count + trade_count), path.stat().st_mtime)
    except Exception:
        return (0, 0, 0.0)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _load_audit_store() -> Optional[AuditStore]:
    best_path: Optional[Path] = None
    best_score = (-1, -1, -1.0)
    for path in _candidate_audit_db_paths():
        score = _audit_db_score(path)
        if score > best_score:
            best_score = score
            best_path = path
    if not best_path or best_score[0] <= 0:
        return None
    return AuditStore(best_path, config=StorageConfig(root=best_path.parent, database_name=best_path.name))


def _compact_timestamp(value: str) -> str:
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%H:%M:%S")
    except Exception:
        return str(value)[:19]


def _watchlist_runtime_snapshot(symbols: List[str]) -> Dict[str, dict]:
    store = _load_audit_store()
    snapshot: Dict[str, dict] = {symbol: {"symbol": symbol} for symbol in symbols}
    if not store:
        return snapshot

    try:
        runtime_items = store.list_runtime_states()
        recent_events = store.recent_events(limit=max(200, len(symbols) * 30))
    except Exception:
        return snapshot

    for item in runtime_items:
        namespace = item.get("namespace")
        state_key = item.get("state_key", "")
        value = item.get("value", {}) or {}
        symbol = str(value.get("symbol") or state_key.split(":")[-1] or "").upper()
        if symbol not in snapshot:
            continue
        if namespace == "engine" and state_key.startswith("active_trade:"):
            snapshot[symbol]["active_trade"] = {
                "side": value.get("side", "-"),
                "entry_price": value.get("entry_price"),
                "stop_loss": value.get("stop_loss"),
                "take_profit": value.get("take_profit"),
                "trade_id": value.get("trade_id"),
                "updated_at": value.get("updated_at") or item.get("updated_at"),
            }
        elif namespace == "monitoring":
            snapshot[symbol]["monitoring"] = {
                "component": value.get("component"),
                "status": value.get("status", "unknown"),
                "kind": value.get("kind"),
                "message": value.get("message"),
                "updated_at": item.get("updated_at"),
            }

    for event in recent_events:
        symbol = str(event.get("symbol") or "").upper()
        if symbol not in snapshot:
            continue
        payload = event.get("payload", {}) or {}
        if event.get("category") == "decision" and "decision" not in snapshot[symbol]:
            blockers = payload.get("blockers") or []
            if isinstance(blockers, str):
                blockers = [part.strip() for part in blockers.split(",") if part.strip()]
            snapshot[symbol]["decision"] = {
                "event_type": event.get("event_type", "-"),
                "candle_time": payload.get("candle_time", "-"),
                "price": payload.get("price"),
                "regime": payload.get("regime", "-"),
                "htf": payload.get("htf", "-"),
                "rsi": payload.get("rsi", "-"),
                "confidence": float(payload.get("confidence", 0.0) or 0.0),
                "setup_type": payload.get("setup_type", "-"),
                "entry_grade": payload.get("entry_grade", "-"),
                "blockers": blockers[:6],
                "updated_at": event.get("event_time"),
            }
        elif event.get("category") == "execution" and "latest_execution" not in snapshot[symbol]:
            snapshot[symbol]["latest_execution"] = {
                "event_type": event.get("event_type", "-"),
                "severity": event.get("severity", "info"),
                "updated_at": event.get("event_time"),
            }

    for symbol, item in snapshot.items():
        decision = item.get("decision", {})
        active_trade = item.get("active_trade")
        monitoring = item.get("monitoring", {})
        if active_trade:
            state = f"IN {str(active_trade.get('side', 'trade')).upper()}"
        elif decision.get("event_type") == "signal":
            state = "READY"
            grade = str(decision.get("entry_grade", "-")).upper()
            if grade and grade != "-":
                state = f"READY {grade}"
        elif decision.get("event_type") == "hold":
            state = "HOLD"
        else:
            state = "WAIT"
        item["display_state"] = state
        item["monitor_status"] = monitoring.get("status", "unknown")
        item["updated_label"] = _compact_timestamp(
            (decision.get("updated_at") or item.get("latest_execution", {}).get("updated_at") or monitoring.get("updated_at") or "")
        )
    return snapshot


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
    watchlist_state = _watchlist_runtime_snapshot(symbols)
    if not key or not secret:
        return JSONResponse(
            {
                "symbols": symbols,
                "tickers": [],
                "positions": [],
                "watchlist": [watchlist_state[symbol] for symbol in symbols],
                "error": "API keys not configured",
            }
        )

    from api import DeltaRESTClient

    async with DeltaRESTClient(key, secret) as client:
        product = await client.get_product(symbols[0]) if symbols else None
        account_asset = DeltaRESTClient.infer_account_asset(product, symbols[0] if symbols else "")
        tickers, positions, wallet_balance, wallet_equity = await asyncio.gather(
            asyncio.gather(*(client.get_ticker(symbol) for symbol in symbols)),
            client.get_positions(),
            client.get_wallet_balance(account_asset),
            client.get_account_equity(account_asset),
        )

    positions_by_symbol = {str(position.symbol).upper(): position.__dict__ for position in positions}
    tickers_by_symbol = {str(ticker.symbol).upper(): ticker.__dict__ for ticker in tickers}
    watchlist = [
        {
            **watchlist_state.get(symbol, {"symbol": symbol}),
            "ticker": tickers_by_symbol.get(symbol, {"symbol": symbol}),
            "position": positions_by_symbol.get(symbol),
        }
        for symbol in symbols
    ]

    return JSONResponse(
        {
            "symbols": symbols,
            "tickers": [ticker.__dict__ for ticker in tickers],
            "positions": [position.__dict__ for position in positions],
            "watchlist": watchlist,
            "account": {
                "asset": account_asset,
                "balance": round(float(wallet_balance or 0.0), 4),
                "equity": round(float(wallet_equity or 0.0), 4),
            },
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


DASHBOARD_HTML = (ROOT / "dashboard_template.html").read_text(encoding="utf-8")


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
