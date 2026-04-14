from pathlib import Path

from delta_bot.storage import AuditStore


def test_audit_store_records_events_and_runtime_state(tmp_path: Path):
    store = AuditStore(tmp_path / "system.db")

    store.record_event("system", "started", {"ok": True}, symbol="ETH_USDT")
    store.set_runtime_state("engine", "active_trade:ETH_USDT", {"symbol": "ETH_USDT", "side": "long"})

    events = store.recent_events(limit=10)
    runtime_items = store.list_runtime_states("engine")

    assert len(events) == 1
    assert events[0]["event_type"] == "started"
    assert runtime_items[0]["state_key"] == "active_trade:ETH_USDT"
    assert runtime_items[0]["value"]["side"] == "long"

