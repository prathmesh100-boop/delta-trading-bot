from datetime import datetime
import json
from pathlib import Path

from risk import TradeRecord
from state_store import StateStore


def test_state_store_round_trip(tmp_path: Path):
    store = StateStore(root=tmp_path / "state")
    trade = TradeRecord(
        id="t1",
        symbol="BTC_USDT",
        side="long",
        entry_price=100.0,
        size=5,
        contract_value=0.001,
        stop_loss=95.0,
        take_profit=110.0,
        entry_time=datetime.utcnow(),
        order_id="ord1",
        entry_client_order_id="client1",
        notional_usd=0.5,
        entry_filled=True,
        filled_size=5,
        stop_order_id="sl1",
        take_profit_order_id="tp1",
    )

    store.save_trade(trade)
    loaded = store.load_trade("BTC_USDT")

    assert loaded is not None
    assert loaded.symbol == trade.symbol
    assert loaded.order_id == trade.order_id
    assert loaded.contract_value == trade.contract_value
    assert loaded.filled_size == trade.filled_size

    store.clear_trade("BTC_USDT")
    assert store.load_trade("BTC_USDT") is None


def test_state_store_quarantines_corrupt_file(tmp_path: Path):
    store = StateStore(root=tmp_path / "state")
    broken_path = store._path_for_symbol("ETH_USDT")
    broken_path.write_text("{not-json", encoding="utf-8")

    loaded = store.load_trade("ETH_USDT")

    assert loaded is None
    assert not broken_path.exists()
    assert broken_path.with_suffix(".corrupt").exists()


def test_state_store_load_trade_accepts_legacy_analytics_aliases(tmp_path: Path):
    store = StateStore(root=tmp_path / "state")
    trade_path = store._path_for_symbol("BTCUSD")
    trade_path.write_text(
        json.dumps(
            {
                "id": "t-legacy",
                "symbol": "BTCUSD",
                "side": "long",
                "entry_price": 100.0,
                "size": 2,
                "contract_value": 0.001,
                "stop_loss": 95.0,
                "take_profit": 110.0,
                "entry_time": "2026-04-15T04:45:07+00:00",
                "order_id": "ord-1",
                "entry_client_order_id": "client-1",
                "notional_usd": 0.2,
                "entry_filled": True,
                "filled_size": 2,
                "setup_type": "dip_buy",
                "entry_grade": "A",
                "quality_score": 0.87,
                "ema_depth_pct": 0.31,
                "regime": "trend",
                "htf": "bullish",
                "rsi": 43.5,
                "adx": 28.1,
                "updated_at": "2026-04-15T04:45:08+00:00",
                "unexpected_field": "ignore-me",
            }
        ),
        encoding="utf-8",
    )

    loaded = store.load_trade("BTCUSD")

    assert loaded is not None
    assert loaded.entry_quality_score == 0.87
    assert loaded.regime_at_entry == "trend"
    assert loaded.htf_at_entry == "bullish"
    assert loaded.rsi_at_entry == 43.5
    assert loaded.adx_at_entry == 28.1
