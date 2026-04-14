from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

from api import DeltaRESTClient
from delta_bot.config import StorageConfig
from delta_bot.dashboard_view import render_dashboard_html
from delta_bot.dashboard_snapshot import monitoring_summary, trade_summary
from delta_bot.runtime import build_audit_store


def create_backend_app() -> FastAPI:
    app = FastAPI(title="Delta Bot Control API", version="0.1.0")
    store = build_audit_store(StorageConfig())

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> str:
        return render_dashboard_html()

    @app.get("/api/health")
    async def health() -> Dict[str, Any]:
        return {
            "ok": True,
            "api_configured": bool(os.getenv("DELTA_API_KEY", "").strip() and os.getenv("DELTA_API_SECRET", "").strip()),
            "db_path": str(store.db_path),
        }

    @app.get("/api/portfolio")
    async def portfolio() -> Dict[str, Any]:
        return store.latest_portfolio_snapshot() or {}

    @app.get("/api/events")
    async def events(limit: int = Query(default=100, ge=1, le=500), category: str | None = None) -> Dict[str, Any]:
        return {"items": store.recent_events(limit=limit, category=category)}

    @app.get("/api/trades")
    async def trades(limit: int = Query(default=100, ge=1, le=500)) -> Dict[str, Any]:
        return {"items": store.recent_trades(limit=limit)}

    @app.get("/api/runtime")
    async def runtime(namespace: str | None = None) -> Dict[str, Any]:
        return {"items": store.list_runtime_states(namespace=namespace)}

    @app.get("/api/account")
    async def account(symbol: str = Query(default="ETH_USDT")) -> Dict[str, Any]:
        api_key = os.getenv("DELTA_API_KEY", "").strip()
        api_secret = os.getenv("DELTA_API_SECRET", "").strip()
        if not api_key or not api_secret:
            return {"error": "missing_api_credentials"}
        async with DeltaRESTClient(api_key, api_secret) as rest:
            product = await rest.get_product(symbol)
            asset = DeltaRESTClient.infer_account_asset(product, symbol)
            balance = await rest.get_wallet_balance(asset)
            equity = await rest.get_account_equity(asset)
            positions = [vars(position) for position in await rest.get_positions()]
            orders = await rest.get_open_orders(product_id=product.get("id") if product else None)
            ticker = vars(await rest.get_ticker(symbol))
        return {
            "asset": asset,
            "balance": balance,
            "equity": equity,
            "positions": positions,
            "orders": orders,
            "ticker": ticker,
            "product": product or {},
        }

    @app.get("/api/dashboard/snapshot")
    async def dashboard_snapshot(symbol: str = Query(default="ETH_USDT")) -> Dict[str, Any]:
        recent_events = store.recent_events(limit=80)
        recent_trades = store.recent_trades(limit=80)
        runtime_items = store.list_runtime_states()
        portfolio = store.latest_portfolio_snapshot() or {}
        errors: List[str] = []

        api_key = os.getenv("DELTA_API_KEY", "").strip()
        api_secret = os.getenv("DELTA_API_SECRET", "").strip()
        account_payload: Dict[str, Any] = {"positions": [], "orders": [], "ticker": {}, "product": {}}
        if api_key and api_secret:
            try:
                async with DeltaRESTClient(api_key, api_secret) as rest:
                    product = await rest.get_product(symbol)
                    asset = DeltaRESTClient.infer_account_asset(product, symbol)
                    account_payload = {
                        "asset": asset,
                        "balance": await rest.get_wallet_balance(asset),
                        "equity": await rest.get_account_equity(asset),
                        "positions": [vars(position) for position in await rest.get_positions()],
                        "orders": await rest.get_open_orders(product_id=product.get("id") if product else None),
                        "ticker": vars(await rest.get_ticker(symbol)),
                        "product": product or {},
                    }
            except Exception as exc:
                errors.append(str(exc))
        else:
            errors.append("Delta API credentials are missing")

        risk_events = [item for item in recent_events if item.get("category") == "risk"][:20]
        execution_events = [item for item in recent_events if item.get("category") in {"execution", "system"}][:20]
        portfolio_history = store.recent_portfolio_snapshots(limit=120)

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "health": {
                "ok": True,
                "api_configured": bool(api_key and api_secret),
                "db_path": str(store.db_path),
            },
            "portfolio": portfolio,
            "trade_summary": trade_summary(recent_trades),
            "monitoring": monitoring_summary(recent_events),
            "account": account_payload,
            "events": {"risk": risk_events, "execution": execution_events},
            "trades": {"items": recent_trades[:20]},
            "runtime": {"items": runtime_items[:20]},
            "history": {"portfolio": portfolio_history},
            "errors": errors,
        }

    return app
