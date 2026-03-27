"""
strategy.py — Strategy framework + sample strategies
Plug in any Strategy subclass; the engine calls generate_signal() on each bar.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Signal
# ─────────────────────────────────────────────

class SignalType(str, Enum):
    LONG = "long"
    SHORT = "short"
    CLOSE_LONG = "close_long"
    CLOSE_SHORT = "close_short"
    HOLD = "hold"


@dataclass
class Signal:
    type: SignalType
    symbol: str
    price: float                           # entry / reference price
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    confidence: float = 1.0               # 0–1, used for position sizing
    metadata: Dict = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


# ─────────────────────────────────────────────
# Base Strategy
# ─────────────────────────────────────────────

class BaseStrategy(ABC):
    """
    All strategies inherit from here.
    Implement generate_signal() to return a Signal given a DataFrame of OHLCV.
    """

    name: str = "base"

    def __init__(self, params: Dict = None):
        self.params = params or {}

    @abstractmethod
    def generate_signal(self, df: pd.DataFrame, symbol: str) -> Signal:
        """
        :param df: OHLCV DataFrame with columns [open, high, low, close, volume].
                   Indexed by datetime, sorted ascending. At least 200 rows recommended.
        :param symbol: trading symbol string
        :return: Signal
        """

    # ── Common indicator helpers ──────────────

    @staticmethod
    def ema(series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=period, adjust=False).mean()

    @staticmethod
    def rsi(series: pd.Series, period: int = 14) -> pd.Series:
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    @staticmethod
    def bollinger_bands(
        series: pd.Series, period: int = 20, std_dev: float = 2.0
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """Returns (upper, middle, lower)."""
        middle = series.rolling(period).mean()
        std = series.rolling(period).std()
        return middle + std_dev * std, middle, middle - std_dev * std

    @staticmethod
    def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        high_low = df["high"] - df["low"]
        high_close = (df["high"] - df["close"].shift()).abs()
        low_close = (df["low"] - df["close"].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        return tr.ewm(alpha=1 / period, adjust=False).mean()


# ─────────────────────────────────────────────
# Strategy 1 — Trend Following
# EMA Crossover (fast/slow) filtered by RSI + multi-timeframe confirmation
# ─────────────────────────────────────────────

class EMACrossoverStrategy(BaseStrategy):
    """
    Entry rules
    -----------
    LONG  : fast EMA crosses above slow EMA AND RSI is between 50–70 (bullish, not overbought)
    SHORT : fast EMA crosses below slow EMA AND RSI is between 30–50 (bearish, not oversold)

    Exit rules
    ----------
    Stop-loss  : ATR-based (atr_multiplier × ATR below/above entry)
    Take-profit: risk_reward × risk distance

    Optional multi-timeframe filter: higher-TF trend must agree with signal.
    """

    name = "ema_crossover"

    DEFAULT_PARAMS = {
        "fast_ema": 9,
        "slow_ema": 21,
        "rsi_period": 14,
        "rsi_long_min": 50,
        "rsi_long_max": 70,
        "rsi_short_min": 30,
        "rsi_short_max": 50,
        "atr_period": 14,
        "atr_sl_multiplier": 1.5,
        "risk_reward": 2.0,
    }

    def __init__(self, params: Dict = None):
        merged = {**self.DEFAULT_PARAMS, **(params or {})}
        super().__init__(merged)

    def generate_signal(self, df: pd.DataFrame, symbol: str) -> Signal:
        if len(df) < self.params["slow_ema"] + 10:
            return Signal(SignalType.HOLD, symbol, df["close"].iloc[-1])

        close = df["close"]
        fast = self.ema(close, self.params["fast_ema"])
        slow = self.ema(close, self.params["slow_ema"])
        rsi = self.rsi(close, self.params["rsi_period"])
        atr = self.atr(df, self.params["atr_period"])

        # Current and previous bar values
        fast_now, fast_prev = fast.iloc[-1], fast.iloc[-2]
        slow_now, slow_prev = slow.iloc[-1], slow.iloc[-2]
        rsi_now = rsi.iloc[-1]
        atr_now = atr.iloc[-1]
        price = close.iloc[-1]

        # Detect crossover (previous bar was opposite side)
        bullish_cross = fast_prev <= slow_prev and fast_now > slow_now
        bearish_cross = fast_prev >= slow_prev and fast_now < slow_now

        sl_dist = self.params["atr_sl_multiplier"] * atr_now
        tp_dist = sl_dist * self.params["risk_reward"]

        if bullish_cross and self.params["rsi_long_min"] <= rsi_now <= self.params["rsi_long_max"]:
            logger.debug("%s: EMA bullish cross, RSI=%.1f → LONG", symbol, rsi_now)
            return Signal(
                type=SignalType.LONG,
                symbol=symbol,
                price=price,
                stop_loss=price - sl_dist,
                take_profit=price + tp_dist,
                confidence=min(1.0, (rsi_now - 50) / 20 + 0.5),
                metadata={"fast_ema": fast_now, "slow_ema": slow_now, "rsi": rsi_now},
            )

        if bearish_cross and self.params["rsi_short_min"] <= rsi_now <= self.params["rsi_short_max"]:
            logger.debug("%s: EMA bearish cross, RSI=%.1f → SHORT", symbol, rsi_now)
            return Signal(
                type=SignalType.SHORT,
                symbol=symbol,
                price=price,
                stop_loss=price + sl_dist,
                take_profit=price - tp_dist,
                confidence=min(1.0, (50 - rsi_now) / 20 + 0.5),
                metadata={"fast_ema": fast_now, "slow_ema": slow_now, "rsi": rsi_now},
            )

        return Signal(SignalType.HOLD, symbol, price)


# ─────────────────────────────────────────────
# Strategy 2 — Mean Reversion
# Bollinger Bands + Volume confirmation + RSI extreme filter
# ─────────────────────────────────────────────

class BollingerMeanReversionStrategy(BaseStrategy):
    """
    Entry rules
    -----------
    LONG  : close < lower band AND volume spike AND RSI < oversold threshold
    SHORT : close > upper band AND volume spike AND RSI > overbought threshold

    Exits at middle band (mean reversion target) with ATR-based stop.

    Notes
    -----
    Mean reversion works best in ranging/consolidating markets.
    Always combine with a market-regime filter (ADX < 25 = ranging).
    """

    name = "bollinger_mean_reversion"

    DEFAULT_PARAMS = {
        "bb_period": 20,
        "bb_std": 2.0,
        "rsi_period": 14,
        "rsi_oversold": 35,
        "rsi_overbought": 65,
        "volume_lookback": 20,
        "volume_spike_factor": 1.5,       # current volume > 1.5× 20-bar avg
        "atr_period": 14,
        "atr_sl_multiplier": 1.5,
        "adx_period": 14,
        "adx_max": 30,                    # skip signal in strong trends
    }

    def __init__(self, params: Dict = None):
        merged = {**self.DEFAULT_PARAMS, **(params or {})}
        super().__init__(merged)

    @staticmethod
    def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
        high, low, close = df["high"], df["low"], df["close"]
        plus_dm = high.diff().clip(lower=0)
        minus_dm = (-low.diff()).clip(lower=0)
        # If plus_dm > minus_dm: keep plus_dm, else 0
        plus_dm = plus_dm.where(plus_dm > minus_dm, 0)
        minus_dm = minus_dm.where(minus_dm > plus_dm, 0)

        tr = pd.concat(
            [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1
        ).max(axis=1)
        atr = tr.ewm(alpha=1 / period, adjust=False).mean()
        plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr
        minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr
        dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
        return dx.ewm(alpha=1 / period, adjust=False).mean()

    def generate_signal(self, df: pd.DataFrame, symbol: str) -> Signal:
        p = self.params
        if len(df) < p["bb_period"] + p["adx_period"] + 5:
            return Signal(SignalType.HOLD, symbol, df["close"].iloc[-1])

        close = df["close"]
        upper, middle, lower = self.bollinger_bands(close, p["bb_period"], p["bb_std"])
        rsi = self.rsi(close, p["rsi_period"])
        atr_vals = self.atr(df, p["atr_period"])
        adx_vals = self.adx(df, p["adx_period"])

        price = close.iloc[-1]
        vol_avg = df["volume"].rolling(p["volume_lookback"]).mean().iloc[-1]
        vol_now = df["volume"].iloc[-1]
        vol_spike = vol_now >= p["volume_spike_factor"] * vol_avg

        rsi_now = rsi.iloc[-1]
        adx_now = adx_vals.iloc[-1]
        atr_now = atr_vals.iloc[-1]
        sl_dist = p["atr_sl_multiplier"] * atr_now
        tp = middle.iloc[-1]         # target = reversion to mean

        # Skip if market is strongly trending
        if adx_now > p["adx_max"]:
            logger.debug("%s: ADX=%.1f > %.1f, skipping mean-reversion signal", symbol, adx_now, p["adx_max"])
            return Signal(SignalType.HOLD, symbol, price)

        if price < lower.iloc[-1] and rsi_now < p["rsi_oversold"] and vol_spike:
            logger.debug("%s: Below lower BB, RSI=%.1f, vol spike → LONG", symbol, rsi_now)
            return Signal(
                type=SignalType.LONG,
                symbol=symbol,
                price=price,
                stop_loss=price - sl_dist,
                take_profit=tp,
                confidence=min(1.0, (p["rsi_oversold"] - rsi_now) / p["rsi_oversold"]),
                metadata={"bb_lower": lower.iloc[-1], "adx": adx_now, "rsi": rsi_now},
            )

        if price > upper.iloc[-1] and rsi_now > p["rsi_overbought"] and vol_spike:
            logger.debug("%s: Above upper BB, RSI=%.1f, vol spike → SHORT", symbol, rsi_now)
            return Signal(
                type=SignalType.SHORT,
                symbol=symbol,
                price=price,
                stop_loss=price + sl_dist,
                take_profit=tp,
                confidence=min(1.0, (rsi_now - p["rsi_overbought"]) / (100 - p["rsi_overbought"])),
                metadata={"bb_upper": upper.iloc[-1], "adx": adx_now, "rsi": rsi_now},
            )

        return Signal(SignalType.HOLD, symbol, price)


# ─────────────────────────────────────────────
# Strategy Registry
# ─────────────────────────────────────────────

STRATEGY_REGISTRY: Dict[str, type] = {
    "ema_crossover": EMACrossoverStrategy,
    "bollinger_mean_reversion": BollingerMeanReversionStrategy,
}


def load_strategy(name: str, params: Dict = None) -> BaseStrategy:
    """Factory: instantiate a strategy by name."""
    if name not in STRATEGY_REGISTRY:
        raise ValueError(f"Unknown strategy '{name}'. Available: {list(STRATEGY_REGISTRY)}")
    return STRATEGY_REGISTRY[name](params)
