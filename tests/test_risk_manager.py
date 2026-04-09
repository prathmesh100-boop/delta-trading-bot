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


def test_trade_record_net_pnl_uses_contract_value():
    trade = TradeRecord(
        id="1",
        symbol="ETH_USDT",
        side="long",
        entry_price=100.0,
        size=10,
        contract_value=0.01,
        stop_loss=95.0,
        take_profit=110.0,
        entry_time=datetime.utcnow(),
        order_id="abc",
    )
    trade.exit_price = 110.0
    assert trade.net_pnl == 1.0


def test_update_equity_resets_baseline_on_fresh_session_outlier():
    cfg = RiskConfig()
    rm = RiskManager(cfg, initial_capital=50.0)

    rm.update_equity(525.0)
    assert rm.current_equity == 525.0
    assert rm._peak_equity == 525.0

    rm.update_equity(50.0)

    assert rm.current_equity == 50.0
    assert rm.current_capital == 50.0
    assert rm._peak_equity == 50.0
    assert rm.can_trade() is True

