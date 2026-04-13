from datetime import datetime
from pathlib import Path

from dashboard import load_recent_decisions, load_runtime_state, summarize_decisions
from risk import TradeRecord
from state_store import StateStore


def test_load_runtime_state_returns_serialized_trade(tmp_path: Path):
    store = StateStore(root=tmp_path / ".bot_state")
    trade = TradeRecord(
        id="trade-1",
        symbol="ETH_USDT",
        side="long",
        entry_price=1800.0,
        size=2,
        contract_value=0.01,
        stop_loss=1750.0,
        take_profit=1900.0,
        entry_time=datetime.utcnow(),
        order_id="order-1",
    )

    store.save_trade(trade)
    rows = load_runtime_state(root=store.root)

    assert len(rows) == 1
    assert rows[0]["symbol"] == "ETH_USDT"
    assert rows[0]["entry_time"]
    assert rows[0]["net_pnl"] is None


def test_load_recent_decisions_and_summary(tmp_path: Path):
    csv_path = tmp_path / "decisions.csv"
    csv_path.write_text(
        "timestamp,symbol,event,side,price,confidence,pnl\n"
        "2026-04-13T10:00:00,ETH_USDT,SIGNAL,long,1800,0.81,\n"
        "2026-04-13T10:05:00,ETH_USDT,ENTRY,long,1801,0.81,\n"
        "2026-04-13T12:00:00,ETH_USDT,EXIT,long,1820,0.81,12.5\n"
        "2026-04-13T13:00:00,ETH_USDT,EXIT,long,1790,0.40,-5.0\n",
        encoding="utf-8",
    )

    rows = load_recent_decisions(csv_path=csv_path, limit=10)
    summary = summarize_decisions(rows)

    assert rows[0]["event"] == "EXIT"
    assert summary["signals"] == 1
    assert summary["entries"] == 1
    assert summary["exits"] == 2
    assert summary["wins"] == 1
    assert summary["losses"] == 1
    assert summary["realized_pnl"] == 7.5
