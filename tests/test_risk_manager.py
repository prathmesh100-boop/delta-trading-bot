from datetime import datetime

from risk import RiskConfig, RiskManager, TradeRecord
from strategy import Signal, SignalType


def make_signal(long=True, price=100.0, sl=99.0, conf=0.9):
    return Signal(SignalType.LONG if long else SignalType.SHORT, "SYM", price, stop_loss=sl, take_profit=price * 1.02, confidence=conf)


def test_calculate_position_size_and_caps():
    cfg = RiskConfig()
    rm = RiskManager(cfg, initial_capital=10_000.0)
    sig = make_signal()
    notional = rm.calculate_position_size(sig, current_price=100.0, symbol="BTCUSD")
    # Notional should be positive and less than max position cap
    assert notional > 0
    assert notional <= rm.current_capital * cfg.max_position_size_pct * cfg.leverage_by_symbol.get("BTCUSD", cfg.leverage)


def test_check_signal_halts_and_limits():
    cfg = RiskConfig()
    rm = RiskManager(cfg, initial_capital=1000.0)
    # Artificially reduce capital to exceed daily loss
    rm.current_capital = 100.0
    sig = make_signal()
    allowed = rm.check_signal(sig)
    # With large drawdown/daily loss, trading should be halted or limited
    assert allowed is False


def test_partial_close_and_recording():
    cfg = RiskConfig()
    rm = RiskManager(cfg, initial_capital=5000.0)
    tr = TradeRecord(symbol="SYM", side="long", entry_price=100.0, size=10, stop_loss=95.0, take_profit=110.0, entry_time=datetime.utcnow())
    tr.tp1 = 105.0
    rm.register_trade(tr)
    # simulate TP1 hit
    trade = rm.should_partial_close("SYM", current_price=106.0)
    assert trade is not None
    rm.record_partial_close(trade, exit_price=106.0, partial_lots=trade.partial_size)
    assert trade.partial_closed is True
    assert rm.current_capital > 0
