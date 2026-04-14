from datetime import datetime, timedelta, timezone
from pathlib import Path

from delta_bot.dashboard_snapshot import control_plane_summary
from delta_bot.monitoring import RuntimeMonitor, runtime_health_summary
from delta_bot.storage import AuditStore


def test_runtime_monitor_records_slow_loop_event(tmp_path: Path):
    store = AuditStore(tmp_path / "system.db")
    monitor = RuntimeMonitor(store, symbol="ETH_USDT")

    monitor.loop_timing("signal_loop", 3_100.0)

    runtime_items = store.list_runtime_states("monitoring")
    events = store.recent_events(limit=10, category="monitoring")

    assert runtime_items[0]["value"]["status"] == "degraded"
    assert events[0]["event_type"] == "slow_loop_detected"


def test_runtime_health_summary_marks_stale_components():
    now = datetime.now(timezone.utc)
    items = [
        {
            "namespace": "monitoring",
            "state_key": "ETH_USDT:signal_loop",
            "updated_at": (now - timedelta(seconds=400)).isoformat(),
            "value": {
                "component": "signal_loop",
                "symbol": "ETH_USDT",
                "kind": "loop_timing",
                "status": "ok",
                "last_seen_at": (now - timedelta(seconds=400)).isoformat(),
            },
        }
    ]

    summary = runtime_health_summary(items, stale_after_seconds=180.0, now=now)

    assert summary["status"] == "degraded"
    assert summary["stale_count"] == 1
    assert summary["components"][0]["status"] == "stale"


def test_control_plane_summary_includes_runtime_status(tmp_path: Path):
    store = AuditStore(tmp_path / "system.db")
    monitor = RuntimeMonitor(store, symbol="BTC_USDT")
    monitor.error("balance_refresh", "timeout")

    summary = control_plane_summary(store.list_runtime_states(), store.recent_events(limit=20))

    assert summary["status"] == "error"
    assert summary["runtime"]["error_count"] == 1
