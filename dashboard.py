"""
dashboard.py - FastAPI dashboard for the Delta trading bot.
"""

from __future__ import annotations

import asyncio
import csv
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

from api import DeltaAPIError, DeltaRESTClient
from risk import TradeRecord
from state_store import StateStore


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def serialize_trade(trade: TradeRecord) -> Dict[str, Any]:
    payload = asdict(trade)
    payload["entry_time"] = trade.entry_time.isoformat()
    payload["exit_time"] = trade.exit_time.isoformat() if trade.exit_time else None
    payload["net_pnl"] = trade.net_pnl
    return payload


def load_runtime_state(root: Optional[Path] = None) -> List[Dict[str, Any]]:
    store = StateStore(root=root)
    if not store.root.exists():
        return []

    trades: List[Dict[str, Any]] = []
    for path in sorted(store.root.glob("*.json")):
        try:
            trade = store.load_trade(path.stem)
            if not trade:
                continue
            payload = serialize_trade(trade)
            payload["state_file"] = str(path)
            trades.append(payload)
        except Exception:
            continue
    return sorted(trades, key=lambda item: item.get("entry_time") or "", reverse=True)


def load_recent_decisions(csv_path: Optional[Path] = None, limit: int = 80) -> List[Dict[str, Any]]:
    path = csv_path or (Path.cwd() / "decisions.csv")
    if not path.exists():
        return []

    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            cleaned: Dict[str, Any] = {}
            for key, value in row.items():
                if value is None:
                    cleaned[key] = value
                    continue
                value = value.strip()
                if value == "":
                    cleaned[key] = value
                elif key in {"price", "confidence", "stop_loss", "take_profit", "pnl", "entry_price", "exit_price"}:
                    cleaned[key] = _safe_float(value, 0.0)
                else:
                    cleaned[key] = value
            rows.append(cleaned)
    if limit <= 0:
        return list(reversed(rows))
    return list(reversed(rows[-limit:]))


def summarize_decisions(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    exits = [row for row in rows if str(row.get("event", "")).upper() == "EXIT"]
    entries = [row for row in rows if str(row.get("event", "")).upper() == "ENTRY"]
    signals = [row for row in rows if str(row.get("event", "")).upper() == "SIGNAL"]
    total_pnl = sum(_safe_float(row.get("pnl")) for row in exits)
    wins = sum(1 for row in exits if _safe_float(row.get("pnl")) > 0)
    losses = sum(1 for row in exits if _safe_float(row.get("pnl")) < 0)
    return {
        "entries": len(entries),
        "signals": len(signals),
        "exits": len(exits),
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / len(exits) * 100.0) if exits else 0.0,
        "realized_pnl": total_pnl,
    }


async def _capture(name: str, awaitable: Any) -> Dict[str, Any]:
    try:
        return {"name": name, "ok": True, "data": await awaitable}
    except Exception as exc:
        return {"name": name, "ok": False, "error": str(exc)}


async def fetch_dashboard_snapshot(
    api_key: str,
    api_secret: str,
    symbol: str,
    resolution: int,
    candles: int,
    decisions_limit: int,
) -> Dict[str, Any]:
    generated_at = _utc_now_iso()
    runtime_trades = load_runtime_state()
    recent_decisions = load_recent_decisions(limit=decisions_limit)
    decision_summary = summarize_decisions(recent_decisions)

    payload: Dict[str, Any] = {
        "generated_at": generated_at,
        "symbol": symbol,
        "resolution": resolution,
        "status": {
            "api_configured": bool(api_key and api_secret),
            "bot_log_exists": Path("bot.log").exists(),
            "decisions_log_exists": Path("decisions.csv").exists(),
            "active_runtime_trades": len(runtime_trades),
        },
        "runtime": {
            "active_trades": runtime_trades,
            "decision_summary": decision_summary,
            "recent_decisions": recent_decisions,
        },
        "account": {},
        "market": {},
        "positions": [],
        "orders": [],
        "symbols": [],
        "errors": [],
    }

    if not api_key or not api_secret:
        payload["errors"].append("Delta API credentials are missing in .env")
        return payload

    async with DeltaRESTClient(api_key, api_secret) as rest:
        product = await rest.get_product(symbol)
        account_asset = DeltaRESTClient.infer_account_asset(product, symbol)
        end_time = int(datetime.now(timezone.utc).timestamp())
        start_time = end_time - (resolution * 60 * max(candles, 30))
        tasks = await asyncio.gather(
            _capture("ticker", rest.get_ticker(symbol)),
            _capture("orderbook", rest.get_orderbook(symbol, depth=10)),
            _capture("ohlcv", rest.get_ohlcv(symbol, resolution, start_time, end_time)),
            _capture("positions", rest.get_positions()),
            _capture("products", rest.get_products()),
            _capture("orders", rest.get_open_orders(product_id=product.get("id") if product else None)),
            _capture("balance", rest.get_wallet_balance(account_asset)),
            _capture("equity", rest.get_account_equity(account_asset)),
        )

    task_map = {item["name"]: item for item in tasks}
    for item in tasks:
        if not item["ok"]:
            payload["errors"].append(f"{item['name']}: {item['error']}")

    products = task_map.get("products", {}).get("data") or []
    payload["symbols"] = sorted(
        [
            {"id": prod.get("id"), "symbol": prod.get("symbol"), "contract_value": prod.get("contract_value")}
            for prod in products
            if isinstance(prod, dict) and prod.get("symbol") and ("USDT" in prod.get("symbol", "") or "USD" in prod.get("symbol", ""))
        ],
        key=lambda item: item["symbol"],
    )[:120]

    ticker = task_map.get("ticker", {}).get("data")
    orderbook = task_map.get("orderbook", {}).get("data")
    ohlcv = task_map.get("ohlcv", {}).get("data") or []
    positions = task_map.get("positions", {}).get("data") or []
    orders = task_map.get("orders", {}).get("data") or []

    payload["account"] = {
        "asset": account_asset,
        "balance": task_map.get("balance", {}).get("data") or 0.0,
        "equity": task_map.get("equity", {}).get("data") or 0.0,
    }
    payload["market"] = {
        "product": product or {},
        "ticker": asdict(ticker) if ticker else {},
        "orderbook": {
            "best_bid": orderbook.best_bid() if orderbook else None,
            "best_ask": orderbook.best_ask() if orderbook else None,
            "spread": orderbook.spread() if orderbook else None,
            "imbalance": orderbook.imbalance() if orderbook else 0.0,
        },
        "candles": [asdict(candle) for candle in ohlcv[-candles:]],
    }
    payload["positions"] = [asdict(position) for position in positions]
    payload["orders"] = orders[:25]
    return payload


def dashboard_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Delta Bot Dashboard</title>
  <style>
    :root{--bg:#09111f;--panel:rgba(7,20,39,.92);--panel2:rgba(9,24,48,.96);--line:rgba(143,191,255,.16);--text:#eef5ff;--muted:#9eb0ca;--accent:#4fd1c5;--accent2:#f6ad55;--good:#4ade80;--bad:#fb7185;--warn:#facc15}
    *{box-sizing:border-box} body{margin:0;font-family:"Segoe UI","Trebuchet MS",sans-serif;background:radial-gradient(circle at top left,rgba(79,209,197,.14),transparent 32%),radial-gradient(circle at top right,rgba(246,173,85,.16),transparent 28%),linear-gradient(160deg,#07101d 0%,#0a1529 48%,#060d19 100%);color:var(--text);min-height:100vh}
    .shell{width:min(1440px,calc(100vw - 24px));margin:0 auto;padding:18px 0 28px}.hero,.metrics,.grid,.status,.tables{display:grid;gap:16px}.hero{grid-template-columns:1.6fr 1fr}.grid{grid-template-columns:1.4fr 1fr}.status,.metrics{grid-template-columns:repeat(4,1fr)}.tables{grid-template-columns:repeat(2,1fr);margin-top:16px}
    .card,.panel{background:var(--panel);border:1px solid var(--line);border-radius:20px;backdrop-filter:blur(12px);box-shadow:0 24px 70px rgba(0,0,0,.35)}.card,.panel{padding:18px}.title{display:flex;justify-content:space-between;gap:12px;align-items:start}
    h1,h2,p{margin:0} h1{font-size:clamp(1.7rem,2vw,2.4rem)} h2{font-size:1rem;margin-bottom:14px;letter-spacing:.05em;text-transform:uppercase}.subtle,.muted{color:var(--muted)}
    .controls{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px} select,button{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);color:var(--text);border-radius:12px;padding:10px 14px;font:inherit} button{cursor:pointer;background:linear-gradient(135deg,rgba(79,209,197,.22),rgba(246,173,85,.24))}
    .metric{padding:16px;background:var(--panel2);border:1px solid var(--line);border-radius:18px}.label{color:var(--muted);font-size:.9rem;margin-bottom:8px}.value{font-size:clamp(1.25rem,1.7vw,2rem);font-weight:700}
    .good{color:var(--good)} .bad{color:var(--bad)} .warn{color:var(--warn)} .pill{display:inline-flex;align-items:center;padding:6px 10px;border-radius:999px;background:rgba(255,255,255,.08)}
    .chart{height:320px;border-radius:16px;overflow:hidden;background:linear-gradient(180deg,rgba(255,255,255,.03),rgba(255,255,255,.01));border:1px solid rgba(255,255,255,.07);padding:12px} canvas{width:100%;height:100%;display:block}
    table{width:100%;border-collapse:collapse;font-size:.93rem} th,td{padding:10px 8px;border-bottom:1px solid rgba(255,255,255,.08);text-align:left;vertical-align:top} th{color:var(--muted);font-size:.82rem;text-transform:uppercase}
    .stack{display:grid;gap:16px}.runtime-card,.log-item,.error{padding:12px;border-radius:14px;border:1px solid rgba(255,255,255,.08);background:rgba(255,255,255,.03)} .log-list{display:grid;gap:10px;max-height:520px;overflow:auto}
    .log-head{display:flex;justify-content:space-between;gap:8px;margin-bottom:6px;font-size:.88rem;color:var(--muted)} .error{color:#ffe5e5;background:rgba(251,113,133,.12);border-color:rgba(251,113,133,.3)}
    @media (max-width:1080px){.hero,.grid,.status,.metrics,.tables{grid-template-columns:1fr}.shell{width:min(100vw - 16px,1440px)}}
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <div class="card">
        <div class="title">
          <div>
            <p class="subtle">Live control surface</p>
            <h1>Delta Trading Bot Dashboard</h1>
            <p class="subtle" id="headline">Connecting to live account and market data...</p>
          </div>
          <div class="pill" id="refreshState">Refreshing</div>
        </div>
        <div class="controls">
          <select id="symbolSelect"></select>
          <select id="resolutionSelect"><option value="5">5m</option><option value="15" selected>15m</option><option value="30">30m</option><option value="60">1h</option><option value="240">4h</option><option value="1440">1d</option></select>
          <button id="refreshBtn" type="button">Refresh Now</button>
        </div>
        <div id="errors" class="stack" style="margin-top:12px"></div>
      </div>
      <div class="card">
        <p class="subtle">Account pulse</p>
        <div class="metrics">
          <div class="metric"><div class="label">Wallet Balance</div><div class="value" id="walletValue">--</div></div>
          <div class="metric"><div class="label">Account Equity</div><div class="value" id="equityValue">--</div></div>
          <div class="metric"><div class="label">Open Positions</div><div class="value" id="positionsValue">--</div></div>
          <div class="metric"><div class="label">Open Orders</div><div class="value" id="ordersValue">--</div></div>
        </div>
      </div>
    </section>
    <section class="status" id="statusRow"></section>
    <section class="grid">
      <div>
        <div class="panel">
          <h2>Price Action</h2>
          <div class="chart"><canvas id="priceChart" width="1000" height="340"></canvas></div>
          <p class="muted" id="chartMeta" style="margin-top:10px"></p>
        </div>
        <div class="tables">
          <div class="panel"><h2>Positions</h2><div id="positionsTable"></div></div>
          <div class="panel"><h2>Open Orders</h2><div id="ordersTable"></div></div>
          <div class="panel"><h2>Order Book</h2><div id="orderbookTable"></div></div>
          <div class="panel"><h2>Runtime Trades</h2><div id="runtimeTrades"></div></div>
        </div>
      </div>
      <div class="stack">
        <div class="panel">
          <h2>Decision Stream</h2>
          <div class="metrics">
            <div class="metric"><div class="label">Signals</div><div class="value" id="signalsValue">--</div></div>
            <div class="metric"><div class="label">Entries</div><div class="value" id="entriesValue">--</div></div>
            <div class="metric"><div class="label">Win Rate</div><div class="value" id="winRateValue">--</div></div>
            <div class="metric"><div class="label">Realized PnL</div><div class="value" id="pnlValue">--</div></div>
          </div>
        </div>
        <div class="panel"><h2>Recent Decisions</h2><div class="log-list" id="decisionList"></div></div>
      </div>
    </section>
  </div>
  <script>
    const state={symbol:new URLSearchParams(location.search).get("symbol")||"ETH_USDT",resolution:Number(new URLSearchParams(location.search).get("resolution")||15),firstLoad:true};
    const $=(id)=>document.getElementById(id);
    const fmt=(v,d=2)=>Number.isFinite(Number(v))?new Intl.NumberFormat("en-US",{minimumFractionDigits:d,maximumFractionDigits:d}).format(Number(v)):"--";
    const signed=(v,d=2)=>Number.isFinite(Number(v))?`${Number(v)>=0?"+":""}${fmt(v,d)}`:"--";
    const table=(headers,rows)=>rows.length?`<table><thead><tr>${headers.map(h=>`<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.map(r=>`<tr>${r.map(c=>`<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`:`<p class="muted">No data available.</p>`;
    function cards(data){const items=[["API",data.status.api_configured?"Connected":"Missing keys",data.status.api_configured?"good":"bad"],["Bot Log",data.status.bot_log_exists?"Present":"Missing",data.status.bot_log_exists?"good":"warn"],["Decisions",data.status.decisions_log_exists?"Recording":"No file",data.status.decisions_log_exists?"good":"warn"],["Runtime Trades",String(data.status.active_runtime_trades),data.status.active_runtime_trades?"good":"muted"]];$("statusRow").innerHTML=items.map(([l,v,c])=>`<div class="metric"><div class="label">${l}</div><div class="value ${c}">${v}</div></div>`).join("")}
    function chart(candles){const canvas=$("priceChart"),ctx=canvas.getContext("2d"),w=canvas.width,h=canvas.height;ctx.clearRect(0,0,w,h);if(!candles.length){ctx.fillStyle="#9eb0ca";ctx.font="18px Segoe UI";ctx.fillText("No candle data available",24,48);return}const highs=candles.map(c=>Number(c.high)),lows=candles.map(c=>Number(c.low)),min=Math.min(...lows),max=Math.max(...highs),range=max-min||1,padX=18,padY=16,innerW=w-padX*2,innerH=h-padY*2,candleW=Math.max(4,innerW/candles.length*.66);ctx.strokeStyle="rgba(255,255,255,.08)";for(let i=0;i<5;i+=1){const y=padY+innerH*(i/4);ctx.beginPath();ctx.moveTo(padX,y);ctx.lineTo(w-padX,y);ctx.stroke()}candles.forEach((c,i)=>{const x=padX+(innerW*i/Math.max(candles.length-1,1)),openY=padY+innerH*(1-((Number(c.open)-min)/range)),closeY=padY+innerH*(1-((Number(c.close)-min)/range)),highY=padY+innerH*(1-((Number(c.high)-min)/range)),lowY=padY+innerH*(1-((Number(c.low)-min)/range)),up=Number(c.close)>=Number(c.open);ctx.strokeStyle=up?"#4ade80":"#fb7185";ctx.fillStyle=up?"rgba(74,222,128,.75)":"rgba(251,113,133,.75)";ctx.beginPath();ctx.moveTo(x,highY);ctx.lineTo(x,lowY);ctx.stroke();ctx.fillRect(x-candleW/2,Math.min(openY,closeY),candleW,Math.max(3,Math.abs(closeY-openY)))})}
    function runtime(trades){$("runtimeTrades").innerHTML=trades.length?trades.map(t=>`<div class="runtime-card"><div class="log-head"><strong>${t.symbol}</strong><span class="${t.side==="long"?"good":"bad"}">${t.side.toUpperCase()}</span></div><div>Entry: ${fmt(t.entry_price,4)} | Stop: ${fmt(t.stop_loss,4)}</div><div>TP: ${t.take_profit?fmt(t.take_profit,4):"--"} | Size: ${t.size}</div><div class="muted">Opened ${new Date(t.entry_time).toLocaleString()}</div></div>`).join(""):`<p class="muted">No persisted active trades found in .bot_state.</p>`}
    function decisions(rows){$("decisionList").innerHTML=rows.length?rows.map(r=>`<div class="log-item"><div class="log-head"><span>${r.event||"EVENT"} ${r.side?`· ${r.side}`:""}</span><span>${r.timestamp?new Date(r.timestamp).toLocaleString():"--"}</span></div><div>${r.symbol||"--"} @ ${fmt(r.price,4)}</div><div class="muted">${r.confidence!==undefined&&r.confidence!==""?`Confidence ${fmt(r.confidence,2)} · `:""}${r.pnl!==undefined&&r.pnl!==""?`PnL ${signed(r.pnl,2)}`:r.reason||"No extra metadata"}</div></div>`).join(""):`<p class="muted">No decision log entries yet.</p>`}
    function symbolOptions(symbols){const current=state.symbol,options=symbols.length?symbols:[{symbol:current}];$("symbolSelect").innerHTML=options.map(item=>`<option value="${item.symbol}" ${item.symbol===current?"selected":""}>${item.symbol}</option>`).join("")}
    async function refresh(){ $("refreshState").textContent="Refreshing"; const query=new URLSearchParams({symbol:state.symbol,resolution:String(state.resolution)}); const data=await (await fetch(`/api/dashboard?${query.toString()}`)).json(); $("headline").textContent=`${data.symbol} · ${data.resolution}m · last update ${new Date(data.generated_at).toLocaleTimeString()}`; $("walletValue").textContent=`${fmt(data.account.balance,2)} ${data.account.asset||""}`; $("equityValue").textContent=`${fmt(data.account.equity,2)} ${data.account.asset||""}`; $("positionsValue").textContent=String(data.positions.length); $("ordersValue").textContent=String(data.orders.length); $("signalsValue").textContent=String(data.runtime.decision_summary.signals); $("entriesValue").textContent=String(data.runtime.decision_summary.entries); $("winRateValue").textContent=`${fmt(data.runtime.decision_summary.win_rate,1)}%`; $("pnlValue").textContent=signed(data.runtime.decision_summary.realized_pnl,2); $("chartMeta").textContent=`Last ${data.market.candles.length} candles · Mark ${fmt(data.market.ticker.mark_price,4)} · Funding ${fmt(data.market.ticker.funding_rate,4)}`; chart(data.market.candles); cards(data); symbolOptions(data.symbols); runtime(data.runtime.active_trades); decisions(data.runtime.recent_decisions); $("positionsTable").innerHTML=table(["Symbol","Side","Size","Entry","UPnL"],data.positions.map(p=>[p.symbol,`<span class="${p.side==="long"?"good":"bad"}">${p.side}</span>`,fmt(p.size,4),fmt(p.entry_price,4),`<span class="${Number(p.unrealized_pnl)>=0?"good":"bad"}">${signed(p.unrealized_pnl,2)}</span>`])); $("ordersTable").innerHTML=table(["ID","Side","Type","Size","Price"],data.orders.map(o=>[String(o.id||"").slice(0,10),o.side||"--",o.order_type||"--",fmt(o.size,4),fmt(o.limit_price||o.stop_price||0,4)])); $("orderbookTable").innerHTML=table(["Metric","Value"],[["Best Bid",fmt(data.market.orderbook.best_bid,4)],["Best Ask",fmt(data.market.orderbook.best_ask,4)],["Spread",fmt(data.market.orderbook.spread,4)],["Imbalance",fmt(data.market.orderbook.imbalance,3)]]); $("errors").innerHTML=data.errors.length?data.errors.map(msg=>`<div class="error">${msg}</div>`).join(""):""; $("refreshState").textContent="Live"; if(state.firstLoad){$("symbolSelect").value=state.symbol;$("resolutionSelect").value=String(state.resolution);state.firstLoad=false}}
    $("refreshBtn").addEventListener("click",refresh); $("symbolSelect").addEventListener("change",e=>{state.symbol=e.target.value;refresh()}); $("resolutionSelect").addEventListener("change",e=>{state.resolution=Number(e.target.value);refresh()}); refresh(); setInterval(refresh,15000);
  </script>
</body>
</html>"""


def create_dashboard_app(default_symbol: str = "ETH_USDT", default_resolution: int = 15) -> FastAPI:
    app = FastAPI(title="Delta Trading Bot Dashboard", version="1.0.0")

    @app.get("/", response_class=HTMLResponse)
    async def home() -> str:
        return dashboard_html()

    @app.get("/api/health")
    async def health() -> Dict[str, Any]:
        return {
            "ok": True,
            "generated_at": _utc_now_iso(),
            "api_configured": bool(os.getenv("DELTA_API_KEY", "").strip() and os.getenv("DELTA_API_SECRET", "").strip()),
        }

    @app.get("/api/dashboard")
    async def api_dashboard(
        symbol: str = Query(default=default_symbol),
        resolution: int = Query(default=default_resolution),
        candles: int = Query(default=80, ge=30, le=300),
        decisions_limit: int = Query(default=80, ge=10, le=300),
    ) -> Dict[str, Any]:
        api_key = os.getenv("DELTA_API_KEY", "").strip()
        api_secret = os.getenv("DELTA_API_SECRET", "").strip()
        try:
            return await fetch_dashboard_snapshot(api_key, api_secret, symbol, resolution, candles, decisions_limit)
        except DeltaAPIError as exc:
            recent_decisions = load_recent_decisions(limit=decisions_limit)
            return {
                "generated_at": _utc_now_iso(),
                "symbol": symbol,
                "resolution": resolution,
                "status": {
                    "api_configured": True,
                    "bot_log_exists": Path("bot.log").exists(),
                    "decisions_log_exists": Path("decisions.csv").exists(),
                    "active_runtime_trades": len(load_runtime_state()),
                },
                "runtime": {
                    "active_trades": load_runtime_state(),
                    "decision_summary": summarize_decisions(recent_decisions),
                    "recent_decisions": recent_decisions,
                },
                "account": {},
                "market": {"ticker": {}, "orderbook": {}, "candles": []},
                "positions": [],
                "orders": [],
                "symbols": [],
                "errors": [f"Delta API error {exc.status}: {exc}"],
            }

    return app
