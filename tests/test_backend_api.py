from pathlib import Path

from delta_bot.dashboard_snapshot import monitoring_summary, trade_summary
from delta_bot.storage import AuditStore


def test_trade_summary_calculates_win_rate_and_best_setup():
    summary = trade_summary(
        [
            {"status": "closed", "pnl": 10.0, "setup_type": "trend_pullback"},
            {"status": "closed", "pnl": -5.0, "setup_type": "trend_pullback"},
            {"status": "closed", "pnl": 8.0, "setup_type": "range_mean_rev"},
            {"status": "open", "pnl": 0.0, "setup_type": "range_mean_rev"},
        ]
    )

    assert summary["closed_trades"] == 3
    assert summary["open_trades"] == 1
    assert summary["realized_pnl"] == 13.0
    assert summary["win_rate"] == 66.67
    assert summary["best_setup"] == "range_mean_rev"


def test_monitoring_summary_counts_errors_and_risk():
    summary = monitoring_summary(
        [
            {"category": "risk", "severity": "warning", "event_type": "signal_rejected"},
            {"category": "execution", "severity": "error", "event_type": "entry_failed"},
            {"category": "system", "severity": "info", "event_type": "started"},
        ]
    )

    assert summary["risk_count"] == 1
    assert summary["execution_count"] == 2
    assert summary["error_count"] == 1
    assert summary["latest_error_event"]["event_type"] == "entry_failed"


def test_recent_portfolio_snapshots_returns_ordered_series(tmp_path: Path):
    store = AuditStore(tmp_path / "system.db")
    store.record_portfolio_snapshot({"timestamp": "2026-01-01T00:00:00+00:00", "current_equity": 100, "current_capital": 100, "peak_equity": 100, "daily_start_equity": 100, "drawdown_pct": 0, "daily_loss_pct": 0, "open_positions": 0, "open_notional_usd": 0, "open_risk_usd": 0, "kill_switch": False})
    store.record_portfolio_snapshot({"timestamp": "2026-01-01T00:05:00+00:00", "current_equity": 110, "current_capital": 110, "peak_equity": 110, "daily_start_equity": 100, "drawdown_pct": 0, "daily_loss_pct": 0, "open_positions": 1, "open_notional_usd": 50, "open_risk_usd": 5, "kill_switch": False})

    items = store.recent_portfolio_snapshots(limit=10)

    assert len(items) == 2
    assert items[0]["current_equity"] == 100
    assert items[1]["current_equity"] == 110
