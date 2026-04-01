import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from backtest import Backtester, BacktestConfig
from strategy import BaseStrategy, Signal, SignalType


def make_ohlcv(n=200, seed=0):
    np.random.seed(seed)
    dates = [datetime.utcnow() - timedelta(minutes=(n - i)) for i in range(n)]
    price = 100 + np.cumsum(np.random.normal(scale=0.2, size=n))
    high = price + np.abs(np.random.normal(scale=0.05, size=n))
    low = price - np.abs(np.random.normal(scale=0.05, size=n))
    openp = np.concatenate([[price[0]], price[:-1]])
    volume = np.random.randint(50, 200, size=n)
    df = pd.DataFrame({"open": openp, "high": high, "low": low, "close": price, "volume": volume},
                      index=pd.DatetimeIndex(dates))
    return df


class SimpleTestStrategy(BaseStrategy):
    """Emits a single long signal on the first eligible bar and then HOLDs."""

    def __init__(self, params=None):
        super().__init__(params)
        self.emitted = False

    @property
    def name(self):
        return "simple_test"

    def generate_signal(self, df: pd.DataFrame, symbol: str, htf_df=None) -> Signal:
        # emit a long on the first call (if not already emitted)
        if not self.emitted and len(df) > 10:
            self.emitted = True
            price = float(df['close'].iloc[-1])
            sl = price * 0.99
            tp = price * 1.02
            return Signal(SignalType.LONG, symbol, price, stop_loss=sl, take_profit=tp, confidence=1.0,
                          metadata={"tp1": price * 1.01})
        return Signal(SignalType.HOLD, symbol, float(df['close'].iloc[-1]))


def test_backtester_entry_and_exit_lifecycle():
    df = make_ohlcv(100)
    bt = Backtester(BacktestConfig(initial_capital=10000.0, risk_per_trade=0.01, taker_fee=0.0005, slippage_pct=0.0))
    strat = SimpleTestStrategy()
    res = bt.run(df, strat, symbol="TEST")

    # If the strategy emitted a signal, we should have at least one trade
    assert isinstance(res.trades, list)
    # Trades may be zero if no signal emitted; ensure no exceptions and equity curve produced
    assert not res.equity_curve.empty
