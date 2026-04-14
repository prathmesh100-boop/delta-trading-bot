from __future__ import annotations

from typing import Any, Dict, List


def trade_summary(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    closed = [item for item in items if item.get("status") == "closed"]
    pnls = [float(item.get("pnl") or 0.0) for item in closed]
    wins = [p for p in pnls if p > 0]
    by_setup: Dict[str, float] = {}
    setup_counts: Dict[str, int] = {}
    for item in closed:
        setup = item.get("setup_type") or "unknown"
        by_setup[setup] = by_setup.get(setup, 0.0) + float(item.get("pnl") or 0.0)
        setup_counts[setup] = setup_counts.get(setup, 0) + 1
    best_setup = None
    if by_setup:
        best_setup = max(by_setup.keys(), key=lambda key: by_setup[key] / max(setup_counts[key], 1))
    return {
        "closed_trades": len(closed),
        "open_trades": sum(1 for item in items if item.get("status") == "open"),
        "realized_pnl": round(sum(pnls), 4),
        "win_rate": round((len(wins) / len(pnls) * 100.0), 2) if pnls else 0.0,
        "best_setup": best_setup,
    }


def monitoring_summary(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    risk_events = [item for item in events if item.get("category") == "risk"]
    execution_events = [item for item in events if item.get("category") in {"execution", "system"}]
    error_count = sum(1 for item in events if item.get("severity") in {"error", "critical"})
    return {
        "risk_count": len(risk_events),
        "execution_count": len(execution_events),
        "error_count": error_count,
        "latest_risk_event": risk_events[0] if risk_events else None,
        "latest_error_event": next((item for item in events if item.get("severity") in {"error", "critical"}), None),
    }
