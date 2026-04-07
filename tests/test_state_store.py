from datetime import datetime
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
