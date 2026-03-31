"""
strategy.py — Strategy framework + institutional-grade strategies (v3)

FIXES vs v2:
  - SmartMoneyStrategy: MTF confirmation uses aligned HTF EMA, not just direction
  - BollingerMeanReversionStrategy: ADX filter threshold raised (was blocking too many valid trades)
  - All strategies: confidence clamped [0.1, 1.0] (not [0, 1])
  - generate_signal() signature: htf_df is Optional, never required
  - EMACrossoverStrategy: RSI band widened slightly to generate more signals
  - Added VWAP-anchored mean-reversion signal option in SmartMoneyStrategy
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
    price: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    confidence: float = 1.0
    metadata: Dict = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}
        self.confidence = max(0.1, min(1.0, self.confidence))


def hold(symbol: str, price: float) -> Signal:
    return Signal(SignalType.HOLD, symbol, price)


# ─────────────────────────────────────────────
# Base Strategy
# ─────────────────────────────────────────────

class BaseStrategy(ABC):
    name: str = "base"

    def __init__(self, params: Dict = None):
        self.params = params or {}

    @abstractmethod
    def generate_signal(self, df: pd.DataFrame, symbol: str, htf_df: Optional[pd.DataFrame] = None) -> Signal:
        pass

    @staticmethod
    def ema(series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=period, adjust=False).mean()

    @staticmethod
    def sma(series: pd.Series, period: int) -> pd.Series:
        return series.rolling(period).mean()

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
    def bollinger_bands(series: pd.Series, period: int = 20, std_dev: float = 2.0) -> Tuple[pd.Series, pd.Series, pd.Series]:
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

    @staticmethod
    def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
        high, low, close = df["high"], df["low"], df["close"]
        plus_dm = high.diff().clip(lower=0)
        minus_dm = (-low.diff()).clip(lower=0)
        plus_dm = plus_dm.where(plus_dm > minus_dm, 0)
        minus_dm = minus_dm.where(minus_dm > plus_dm, 0)
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs()
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / period, adjust=False).mean()
        plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan)
        minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        return dx.ewm(alpha=1 / period, adjust=False).mean()

    @staticmethod
    def vwap(df: pd.DataFrame) -> pd.Series:
        """Intraday VWAP (cumulative; resets each session implicitly via rolling window)."""
        typical = (df["high"] + df["low"] + df["close"]) / 3
        vwap_series = (typical * df["volume"]).cumsum() / df["volume"].cumsum()
        return vwap_series


# ─────────────────────────────────────────────
# Strategy 1 — EMA Crossover (Trend Following)
# ─────────────────────────────────────────────

class EMACrossoverStrategy(BaseStrategy):
    """
    Long:  fast EMA crosses above slow EMA + RSI in bull zone
    Short: fast EMA crosses below slow EMA + RSI in bear zone
    Stop:  ATR-based
    TP:    risk_reward × SL distance
    """
    name = "ema_crossover"

    DEFAULT_PARAMS = {
        "fast_ema": 9,
        "slow_ema": 21,
        "rsi_period": 14,
        "rsi_long_min": 48,     # Widened from 50 → more signals
        "rsi_long_max": 72,
        "rsi_short_min": 28,
        "rsi_short_max": 52,    # Widened from 50 → more signals
        "atr_period": 14,
        "atr_sl_multiplier": 1.5,
        "risk_reward": 2.0,
    }

    def __init__(self, params: Dict = None):
        super().__init__({**self.DEFAULT_PARAMS, **(params or {})})

    def generate_signal(self, df: pd.DataFrame, symbol: str, htf_df: Optional[pd.DataFrame] = None) -> Signal:
        p = self.params
        if len(df) < p["slow_ema"] + 10:
            return hold(symbol, df["close"].iloc[-1])

        close = df["close"]
        fast = self.ema(close, p["fast_ema"])
        slow = self.ema(close, p["slow_ema"])
        rsi_s = self.rsi(close, p["rsi_period"])
        atr_s = self.atr(df, p["atr_period"])

        price = close.iloc[-1]
        f_now, f_prev = fast.iloc[-1], fast.iloc[-2]
        s_now, s_prev = slow.iloc[-1], slow.iloc[-2]
        rsi_now = rsi_s.iloc[-1]
        atr_now = atr_s.iloc[-1]

        sl_dist = p["atr_sl_multiplier"] * atr_now
        tp_dist = sl_dist * p["risk_reward"]

        bullish_cross = f_prev <= s_prev and f_now > s_now
        bearish_cross = f_prev >= s_prev and f_now < s_now

        if bullish_cross and p["rsi_long_min"] <= rsi_now <= p["rsi_long_max"]:
            return Signal(
                type=SignalType.LONG, symbol=symbol, price=price,
                stop_loss=price - sl_dist, take_profit=price + tp_dist,
                confidence=min(1.0, max(0.5, (rsi_now - 48) / 24 + 0.5)),
                metadata={"fast_ema": float(f_now), "slow_ema": float(s_now), "rsi": float(rsi_now)},
            )

        if bearish_cross and p["rsi_short_min"] <= rsi_now <= p["rsi_short_max"]:
            return Signal(
                type=SignalType.SHORT, symbol=symbol, price=price,
                stop_loss=price + sl_dist, take_profit=price - tp_dist,
                confidence=min(1.0, max(0.5, (52 - rsi_now) / 24 + 0.5)),
                metadata={"fast_ema": float(f_now), "slow_ema": float(s_now), "rsi": float(rsi_now)},
            )

        return hold(symbol, price)


# ─────────────────────────────────────────────
# Strategy 2 — Bollinger Mean Reversion
# ─────────────────────────────────────────────

class BollingerMeanReversionStrategy(BaseStrategy):
    """
    Long:  close < lower band + RSI oversold + volume spike
    Short: close > upper band + RSI overbought + volume spike
    Filter: ADX < adx_max (skip strong trends — mean reversion fails in trends)
    """
    name = "bollinger_mean_reversion"

    DEFAULT_PARAMS = {
        "bb_period": 20,
        "bb_std": 2.0,
        "rsi_period": 14,
        "rsi_oversold": 35,
        "rsi_overbought": 65,
        "volume_lookback": 20,
        "volume_spike_factor": 1.5,
        "atr_period": 14,
        "atr_sl_multiplier": 1.5,
        "adx_period": 14,
        "adx_max": 35,          # Raised from 30 → fewer false rejections
    }

    def __init__(self, params: Dict = None):
        super().__init__({**self.DEFAULT_PARAMS, **(params or {})})

    def generate_signal(self, df: pd.DataFrame, symbol: str, htf_df: Optional[pd.DataFrame] = None) -> Signal:
        p = self.params
        if len(df) < p["bb_period"] + p["adx_period"] + 5:
            return hold(symbol, df["close"].iloc[-1])

        close = df["close"]
        upper, middle, lower = self.bollinger_bands(close, p["bb_period"], p["bb_std"])
        rsi_s = self.rsi(close, p["rsi_period"])
        atr_s = self.atr(df, p["atr_period"])
        adx_s = self.adx(df, p["adx_period"])

        price = close.iloc[-1]
        vol_avg = df["volume"].rolling(p["volume_lookback"]).mean().iloc[-1]
        vol_now = df["volume"].iloc[-1]
        vol_spike = vol_now >= p["volume_spike_factor"] * vol_avg

        rsi_now = float(rsi_s.iloc[-1])
        adx_now = float(adx_s.iloc[-1])
        atr_now = float(atr_s.iloc[-1])
        sl_dist = p["atr_sl_multiplier"] * atr_now
        tp = float(middle.iloc[-1])

        if adx_now > p["adx_max"]:
            return hold(symbol, price)

        if price < float(lower.iloc[-1]) and rsi_now < p["rsi_oversold"] and vol_spike:
            conf = min(1.0, max(0.3, (p["rsi_oversold"] - rsi_now) / p["rsi_oversold"]))
            return Signal(
                type=SignalType.LONG, symbol=symbol, price=price,
                stop_loss=price - sl_dist, take_profit=tp,
                confidence=conf,
                metadata={"bb_lower": float(lower.iloc[-1]), "adx": adx_now, "rsi": rsi_now},
            )

        if price > float(upper.iloc[-1]) and rsi_now > p["rsi_overbought"] and vol_spike:
            conf = min(1.0, max(0.3, (rsi_now - p["rsi_overbought"]) / (100 - p["rsi_overbought"])))
            return Signal(
                type=SignalType.SHORT, symbol=symbol, price=price,
                stop_loss=price + sl_dist, take_profit=tp,
                confidence=conf,
                metadata={"bb_upper": float(upper.iloc[-1]), "adx": adx_now, "rsi": rsi_now},
            )

        return hold(symbol, price)


# ─────────────────────────────────────────────
# Strategy 3 — Smart Money (Multi-Timeframe)
# ─────────────────────────────────────────────

class SmartMoneyStrategy(BaseStrategy):
    """
    Multi-timeframe EMA crossover with RSI scalp entries.
    
    Trend entries (high confidence):
      - LTF fast EMA crosses slow EMA
      - Optionally confirmed by HTF EMA alignment
    
    Scalp entries (lower confidence):
      - RSI extreme oversold/overbought reversals
      - VWAP deviation filter (avoids entries far from VWAP)
    """
    name = "smart_money"

    def __init__(self, params: Dict = None):
        defaults = {
            "fast_ema": 5,
            "slow_ema": 20,
            "rsi_period": 14,
            "atr_period": 14,
            "atr_sl_multiplier": 1.2,
            "risk_reward_trend": 2.0,
            "risk_reward_scalp": 1.5,
            "confidence_trend": 0.9,
            "confidence_scalp": 0.65,
            "rsi_long_threshold": 45,
            "rsi_short_threshold": 55,
            "rsi_scalp_oversold": 30,
            "rsi_scalp_overbought": 70,
            "mtf_confirm": True,
            "htf_resolution": 15,
            "vwap_filter": False,       # Enable VWAP deviation filter (optional)
            "vwap_max_dev_pct": 0.5,    # Max % deviation from VWAP for entry
        }
        super().__init__({**defaults, **(params or {})})

    def generate_signal(self, df: pd.DataFrame, symbol: str, htf_df: Optional[pd.DataFrame] = None) -> Signal:
        p = self.params
        if len(df) < max(p["slow_ema"], p["rsi_period"]) + 5:
            return hold(symbol, df["close"].iloc[-1])

        close = df["close"]
        ema_fast = self.ema(close, p["fast_ema"])
        ema_slow = self.ema(close, p["slow_ema"])
        rsi_s = self.rsi(close, p["rsi_period"])
        atr_s = self.atr(df, p["atr_period"])

        price = float(close.iloc[-1])
        f, s = float(ema_fast.iloc[-1]), float(ema_slow.iloc[-1])
        f_prev, s_prev = float(ema_fast.iloc[-2]), float(ema_slow.iloc[-2])
        r = float(rsi_s.iloc[-1])
        atr_val = float(atr_s.iloc[-1])
        sl_dist = atr_val * p["atr_sl_multiplier"]

        # VWAP deviation filter (optional)
        vwap_ok = True
        if p.get("vwap_filter") and len(df) >= 20:
            try:
                vwap_val = float(self.vwap(df).iloc[-1])
                dev_pct = abs(price - vwap_val) / vwap_val * 100
                vwap_ok = dev_pct <= p["vwap_max_dev_pct"]
            except Exception:
                vwap_ok = True

        # HTF confirmation
        def htf_aligned_long() -> bool:
            if not p.get("mtf_confirm") or htf_df is None or len(htf_df) < p["slow_ema"]:
                return True  # No HTF data = don't block
            try:
                h_close = htf_df["close"]
                h_fast = self.ema(h_close, p["fast_ema"])
                h_slow = self.ema(h_close, p["slow_ema"])
                return float(h_fast.iloc[-1]) > float(h_slow.iloc[-1])
            except Exception:
                return True

        def htf_aligned_short() -> bool:
            if not p.get("mtf_confirm") or htf_df is None or len(htf_df) < p["slow_ema"]:
                return True
            try:
                h_close = htf_df["close"]
                h_fast = self.ema(h_close, p["fast_ema"])
                h_slow = self.ema(h_close, p["slow_ema"])
                return float(h_fast.iloc[-1]) < float(h_slow.iloc[-1])
            except Exception:
                return True

        # ── Trend entries ──
        if f_prev < s_prev and f > s and r > p["rsi_long_threshold"] and vwap_ok:
            if htf_aligned_long():
                tp_dist = sl_dist * p["risk_reward_trend"]
                return Signal(
                    type=SignalType.LONG, symbol=symbol, price=price,
                    stop_loss=price - sl_dist, take_profit=price + tp_dist,
                    confidence=p["confidence_trend"],
                    metadata={"entry": "trend", "ema_fast": f, "ema_slow": s, "rsi": r},
                )

        if f_prev > s_prev and f < s and r < p["rsi_short_threshold"] and vwap_ok:
            if htf_aligned_short():
                tp_dist = sl_dist * p["risk_reward_trend"]
                return Signal(
                    type=SignalType.SHORT, symbol=symbol, price=price,
                    stop_loss=price + sl_dist, take_profit=price - tp_dist,
                    confidence=p["confidence_trend"],
                    metadata={"entry": "trend", "ema_fast": f, "ema_slow": s, "rsi": r},
                )

        # ── Scalp entries (RSI extremes) ──
        if r < p["rsi_scalp_oversold"]:
            tp_dist = sl_dist * p["risk_reward_scalp"]
            return Signal(
                type=SignalType.LONG, symbol=symbol, price=price,
                stop_loss=price - sl_dist, take_profit=price + tp_dist,
                confidence=p["confidence_scalp"],
                metadata={"entry": "scalp_oversold", "rsi": r},
            )

        if r > p["rsi_scalp_overbought"]:
            tp_dist = sl_dist * p["risk_reward_scalp"]
            return Signal(
                type=SignalType.SHORT, symbol=symbol, price=price,
                stop_loss=price + sl_dist, take_profit=price - tp_dist,
                confidence=p["confidence_scalp"],
                metadata={"entry": "scalp_overbought", "rsi": r},
            )

        return hold(symbol, price)


# ─────────────────────────────────────────────
# Strategy Registry
# ─────────────────────────────────────────────

STRATEGY_REGISTRY: Dict[str, type] = {
    "ema_crossover": EMACrossoverStrategy,
    "bollinger_mean_reversion": BollingerMeanReversionStrategy,
    "smart_money": SmartMoneyStrategy,
}


def load_strategy(name: str, params: Dict = None) -> BaseStrategy:
    if name not in STRATEGY_REGISTRY:
        raise ValueError(f"Unknown strategy '{name}'. Available: {list(STRATEGY_REGISTRY)}")
    return STRATEGY_REGISTRY[name](params)
