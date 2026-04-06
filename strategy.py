"""
strategy.py — Trading Strategies v6

Strategies:
  1. EMACrossoverStrategy    — EMA9/21 crossover + RSI filter + ATR SL/TP
  2. BollingerMeanReversion  — Bollinger band extremes + RSI + volume
  3. SmartMoneyStrategy      — Multi-timeframe + CHoCH + OB zones + FVG
  4. BreakoutStrategy        — N-bar high/low breakout + volume confirmation
  5. VWAPMeanReversionStrategy — VWAP deviation entry with ATR exit
  6. AIFilteredStrategy      — Wraps any strategy with ML-style signal scoring

All strategies:
  - Return Signal(type, stop_loss, take_profit, confidence, metadata)
  - Use causal indicators only (no look-ahead)
  - Calculate ATR-based SL/TP
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Type

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class SignalType(str, Enum):
    LONG    = "long"
    SHORT   = "short"
    NEUTRAL = "neutral"


@dataclass
class Signal:
    # Backwards-compatible constructor: (type, symbol, price, stop_loss=..., take_profit=..., confidence=...)
    type:       SignalType
    symbol:     Optional[str] = None
    price:      Optional[float] = None
    stop_loss:  Optional[float] = None
    take_profit: Optional[float] = None
    confidence: float = 1.0
    metadata:   Dict  = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# Base Strategy
# ─────────────────────────────────────────────────────────────────────────────

class BaseStrategy(ABC):
    def __init__(self, params: Optional[Dict] = None):
        self.params = params or {}

    def p(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)

    @abstractmethod
    def generate_signal(self, df: pd.DataFrame, symbol: str = "") -> Optional[Signal]:
        """
        df: OHLCV DataFrame with columns open/high/low/close/volume.
            Only df.iloc[:-1] (past bars) should inform the signal.
        Returns Signal or None.
        """

    # ── Common Indicators ──────────────────────────────────────────────────

    @staticmethod
    def ema(series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=period, adjust=False).mean()

    @staticmethod
    def rsi(series: pd.Series, period: int = 14) -> pd.Series:
        delta = series.diff()
        gain  = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
        loss  = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
        rs    = gain / loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    @staticmethod
    def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        hi, lo, cl = df["high"], df["low"], df["close"]
        tr = pd.concat([
            hi - lo,
            (hi - cl.shift()).abs(),
            (lo - cl.shift()).abs(),
        ], axis=1).max(axis=1)
        return tr.ewm(alpha=1/period, adjust=False).mean()

    @staticmethod
    def vwap(df: pd.DataFrame) -> pd.Series:
        tp  = (df["high"] + df["low"] + df["close"]) / 3
        cum_vol = df["volume"].cumsum()
        cum_tpv = (tp * df["volume"]).cumsum()
        return cum_tpv / cum_vol.replace(0, np.nan)

    @staticmethod
    def bollinger(series: pd.Series, period: int = 20, std: float = 2.0):
        mid  = series.rolling(period).mean()
        s    = series.rolling(period).std()
        return mid - std * s, mid, mid + std * s


# ─────────────────────────────────────────────────────────────────────────────
# 1. EMA Crossover Strategy
# ─────────────────────────────────────────────────────────────────────────────

class EMACrossoverStrategy(BaseStrategy):
    """
    EMA 9/21 crossover with RSI confirmation and ATR-based SL/TP.

    Entry:
      LONG:  EMA9 crosses above EMA21, RSI in (30, 65)
      SHORT: EMA9 crosses below EMA21, RSI in (35, 70)

    Exit:
      SL: ATR × sl_mult below/above entry
      TP: ATR × tp_mult above/below entry (RR ≥ 2)
    """

    DEFAULTS = {
        "fast_ema":         9,
        "slow_ema":         21,
        "rsi_period":       14,
        "rsi_long_max":     65,
        "rsi_short_min":    35,
        "atr_period":       14,
        "atr_sl_multiplier": 1.5,
        "atr_tp_multiplier": 3.0,
    }

    def __init__(self, params: Optional[Dict] = None):
        merged = {**self.DEFAULTS, **(params or {})}
        super().__init__(merged)

    def generate_signal(self, df: pd.DataFrame, symbol: str = "") -> Optional[Signal]:
        if len(df) < 50:
            return None

        fast_e = self.ema(df["close"], self.p("fast_ema"))
        slow_e = self.ema(df["close"], self.p("slow_ema"))
        rsi    = self.rsi(df["close"], self.p("rsi_period"))
        atr_s  = self.atr(df, self.p("atr_period"))

        curr_fast, prev_fast = fast_e.iloc[-1], fast_e.iloc[-2]
        curr_slow, prev_slow = slow_e.iloc[-1], slow_e.iloc[-2]
        curr_rsi  = rsi.iloc[-1]
        atr       = atr_s.iloc[-1]
        price     = df["close"].iloc[-1]

        bullish_cross = prev_fast <= prev_slow and curr_fast > curr_slow
        bearish_cross = prev_fast >= prev_slow and curr_fast < curr_slow

        sl_mult = self.p("atr_sl_multiplier")
        tp_mult = self.p("atr_tp_multiplier")

        if bullish_cross and 30 < curr_rsi < self.p("rsi_long_max"):
            sl = price - atr * sl_mult
            tp = price + atr * tp_mult
            conf = min(1.0, (curr_rsi - 30) / 35 * 0.5 + 0.5)
            return Signal(SignalType.LONG, sl, tp, confidence=conf,
                          metadata={"fast_ema": curr_fast, "rsi": curr_rsi, "atr": atr})

        if bearish_cross and self.p("rsi_short_min") < curr_rsi < 70:
            sl = price + atr * sl_mult
            tp = price - atr * tp_mult
            conf = min(1.0, (70 - curr_rsi) / 35 * 0.5 + 0.5)
            return Signal(SignalType.SHORT, sl, tp, confidence=conf,
                          metadata={"fast_ema": curr_fast, "rsi": curr_rsi, "atr": atr})

        return Signal(SignalType.NEUTRAL)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Bollinger Mean Reversion Strategy
# ─────────────────────────────────────────────────────────────────────────────

class BollingerMeanReversionStrategy(BaseStrategy):
    """
    Bollinger Band extremes with RSI confirmation and volume filter.

    Entry:
      LONG:  price < lower_band, RSI < rsi_oversold, volume > avg_volume
      SHORT: price > upper_band, RSI > rsi_overbought, volume > avg_volume

    Exit:
      SL: 1 ATR beyond band
      TP: middle band (mean reversion target)
    """

    DEFAULTS = {
        "bb_period":      20,
        "bb_std":         2.0,
        "rsi_period":     14,
        "rsi_oversold":   30,
        "rsi_overbought": 70,
        "vol_lookback":   20,
        "atr_period":     14,
    }

    def __init__(self, params: Optional[Dict] = None):
        super().__init__({**self.DEFAULTS, **(params or {})})

    def generate_signal(self, df: pd.DataFrame, symbol: str = "") -> Optional[Signal]:
        if len(df) < 30:
            return None

        lower, mid, upper = self.bollinger(df["close"], self.p("bb_period"), self.p("bb_std"))
        rsi   = self.rsi(df["close"], self.p("rsi_period"))
        atr_s = self.atr(df, self.p("atr_period"))
        avg_v = df["volume"].rolling(self.p("vol_lookback")).mean()

        price    = df["close"].iloc[-1]
        curr_rsi = rsi.iloc[-1]
        atr      = atr_s.iloc[-1]
        vol      = df["volume"].iloc[-1]
        avg_vol  = avg_v.iloc[-1]

        if price < lower.iloc[-1] and curr_rsi < self.p("rsi_oversold") and vol > avg_vol:
            sl = lower.iloc[-1] - atr
            tp = mid.iloc[-1]
            conf = (self.p("rsi_oversold") - curr_rsi) / self.p("rsi_oversold") * 0.5 + 0.5
            return Signal(SignalType.LONG, sl, tp, confidence=min(1.0, conf),
                          metadata={"bb_lower": lower.iloc[-1], "rsi": curr_rsi})

        if price > upper.iloc[-1] and curr_rsi > self.p("rsi_overbought") and vol > avg_vol:
            sl = upper.iloc[-1] + atr
            tp = mid.iloc[-1]
            conf = (curr_rsi - self.p("rsi_overbought")) / (100 - self.p("rsi_overbought")) * 0.5 + 0.5
            return Signal(SignalType.SHORT, sl, tp, confidence=min(1.0, conf),
                          metadata={"bb_upper": upper.iloc[-1], "rsi": curr_rsi})

        return Signal(SignalType.NEUTRAL)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Smart Money / ICT Concepts Strategy
# ─────────────────────────────────────────────────────────────────────────────

class SmartMoneyStrategy(BaseStrategy):
    """
    ICT / Smart Money Concepts:
      - Market structure: Higher Highs / Higher Lows (uptrend), Lower Highs / Lower Lows (downtrend)
      - Change of Character (CHoCH): break of recent swing high/low
      - Order Block: last bearish candle before bullish impulse (or vice versa)
      - Fair Value Gap (FVG): 3-candle imbalance pattern
      - EMA trend filter (fast > slow = bullish bias)
      - ATR-based SL/TP

    Entry:
      LONG: Bullish CHoCH + price returning to OB/FVG + uptrend EMA
      SHORT: Bearish CHoCH + price returning to OB/FVG + downtrend EMA
    """

    DEFAULTS = {
        "swing_length":      10,
        "fast_ema":          8,
        "slow_ema":          21,
        "atr_period":        14,
        "atr_sl_multiplier": 1.2,
        "atr_tp_multiplier": 2.5,
        "fvg_lookback":      5,
        # additional robustness filters
        "min_volume_factor": 1.2,   # require current vol >= avg_vol * factor
        "ema_slope_min":     0.001, # minimum absolute EMA slope to consider
        "min_rr":            2.0,   # minimum reward:risk ratio
    }

    def __init__(self, params: Optional[Dict] = None):
        super().__init__({**self.DEFAULTS, **(params or {})})

    def _swing_points(self, df: pd.DataFrame, length: int):
        """Find swing highs and lows using rolling max/min."""
        highs = df["high"].rolling(length * 2 + 1, center=True).max()
        lows  = df["low"].rolling(length * 2 + 1, center=True).min()
        sh = (df["high"] == highs)
        sl = (df["low"] == lows)
        return sh, sl

    def _detect_fvg(self, df: pd.DataFrame) -> Optional[tuple]:
        """
        Fair Value Gap (FVG): 3-candle pattern where candle[i-1].high < candle[i+1].low (bullish)
        or candle[i-1].low > candle[i+1].high (bearish).
        """
        if len(df) < 3:
            return None
        # Check last 3 candles for FVG
        c1 = df.iloc[-3]
        c3 = df.iloc[-1]
        if c1["high"] < c3["low"]:  # bullish FVG
            return ("bullish", (c1["high"] + c3["low"]) / 2)
        if c1["low"] > c3["high"]:  # bearish FVG
            return ("bearish", (c1["low"] + c3["high"]) / 2)
        return None

    def generate_signal(self, df: pd.DataFrame, symbol: str = "") -> Optional[Signal]:
        if len(df) < 30:
            return None

        fast_e = self.ema(df["close"], self.p("fast_ema"))
        slow_e = self.ema(df["close"], self.p("slow_ema"))
        atr_s  = self.atr(df, self.p("atr_period"))
        rsi    = self.rsi(df["close"], 14)

        price     = df["close"].iloc[-1]
        atr       = atr_s.iloc[-1]
        trend_up  = fast_e.iloc[-1] > slow_e.iloc[-1]
        curr_rsi  = rsi.iloc[-1]

        # EMA slope strength
        ema_slope = (fast_e.iloc[-1] - fast_e.iloc[-5]) / fast_e.iloc[-5]

        # Volume spike filter (avoid low-volume false breaks)
        avg_vol = df["volume"].rolling(20).mean().iloc[-1]
        curr_vol = df["volume"].iloc[-1]
        # Allow sweep to bypass volume filter
        if not pd.isna(avg_vol) and curr_vol < avg_vol * self.p("min_volume_factor"):
            if not (liquidity_sweep_long or liquidity_sweep_short):
                return Signal(SignalType.NEUTRAL)

        # Recent swing: look for CHoCH
        recent = df.iloc[-self.p("swing_length"):]
        recent_high = recent["high"].max()
        recent_low  = recent["low"].min()

        # CHoCH detection
        prev_high = df["high"].iloc[-(self.p("swing_length") + 1):-1].max()
        prev_low  = df["low"].iloc[-(self.p("swing_length") + 1):-1].min()

        bullish_choch = price > prev_high and trend_up
        bearish_choch = price < prev_low and not trend_up

        # FVG check
        fvg = self._detect_fvg(df.iloc[-self.p("fvg_lookback"):])

        sl_mult = self.p("atr_sl_multiplier")
        tp_mult = self.p("atr_tp_multiplier")

        if bullish_choch and curr_rsi < 65 and ema_slope > 0:
            sl = recent_low - atr * sl_mult
            tp = price + atr * tp_mult
            # Avoid very weak EMA slope
            if abs(ema_slope) < self.p("ema_slope_min"):
                return Signal(SignalType.NEUTRAL)

            # Ensure reasonable reward:risk
            denom = price - sl if price - sl != 0 else 1e-9
            rr = (tp - price) / denom
            if rr < self.p("min_rr"):
                return Signal(SignalType.NEUTRAL)

            conf = min(1.0, abs(ema_slope) * 100 + 0.5)
            # Boost when liquidity sweep confirms the move
            if liquidity_sweep_long:
                conf = min(1.0, conf + 0.25)
            if fvg and fvg[0] == "bullish":
                conf = min(1.0, conf + 0.15)
            return Signal(SignalType.LONG, sl, tp, confidence=conf,
                          metadata={"choch": "bullish", "fvg": fvg, "rsi": curr_rsi})

        if bearish_choch and curr_rsi > 35 and ema_slope < 0:
            sl = recent_high + atr * sl_mult
            tp = price - atr * tp_mult
            # Avoid very weak EMA slope
            if abs(ema_slope) < self.p("ema_slope_min"):
                return Signal(SignalType.NEUTRAL)

            # Ensure reasonable reward:risk
            denom = sl - price if sl - price != 0 else 1e-9
            rr = (price - tp) / denom
            if rr < self.p("min_rr"):
                return Signal(SignalType.NEUTRAL)

            conf = min(1.0, abs(ema_slope) * 100 + 0.5)
            # Boost when liquidity sweep confirms the move
            if liquidity_sweep_short:
                conf = min(1.0, conf + 0.25)
            if fvg and fvg[0] == "bearish":
                conf = min(1.0, conf + 0.15)
            return Signal(SignalType.SHORT, sl, tp, confidence=conf,
                          metadata={"choch": "bearish", "fvg": fvg, "rsi": curr_rsi})

        return Signal(SignalType.NEUTRAL)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Breakout Strategy
# ─────────────────────────────────────────────────────────────────────────────

class BreakoutStrategy(BaseStrategy):
    """
    N-bar high/low breakout with volume confirmation.

    Entry:
      LONG:  close breaks above max(high[-n:]), volume > avg_vol × vol_factor
      SHORT: close breaks below min(low[-n:]), volume > avg_vol × vol_factor

    SL: Inside the range (50% of ATR below breakout level)
    TP: Range projection (breakout level + range × rr)
    """

    DEFAULTS = {
        "lookback":     20,
        "vol_lookback": 20,
        "vol_factor":   1.5,
        "atr_period":   14,
        "atr_sl_mult":  1.0,
        "rr":           2.0,
    }

    def __init__(self, params: Optional[Dict] = None):
        super().__init__({**self.DEFAULTS, **(params or {})})

    def generate_signal(self, df: pd.DataFrame, symbol: str = "") -> Optional[Signal]:
        if len(df) < self.p("lookback") + 5:
            return None

        lb   = self.p("lookback")
        hist = df.iloc[-lb - 1:-1]  # exclude current bar
        atr  = self.atr(df, self.p("atr_period")).iloc[-1]

        res  = hist["high"].max()  # resistance
        sup  = hist["low"].min()   # support
        rang = res - sup

        avg_vol = df["volume"].rolling(self.p("vol_lookback")).mean().iloc[-1]
        curr_v  = df["volume"].iloc[-1]
        curr_c  = df["close"].iloc[-1]

        vol_ok = curr_v > avg_vol * self.p("vol_factor")

        sl_mult = self.p("atr_sl_mult")
        rr      = self.p("rr")

        if curr_c > res and vol_ok:
            sl = res - atr * sl_mult
            tp = res + rang * rr
            conf = min(1.0, curr_v / avg_vol / 3)
            return Signal(SignalType.LONG, sl, tp, confidence=conf,
                          metadata={"breakout": "long", "resistance": res, "range": rang})

        if curr_c < sup and vol_ok:
            sl = sup + atr * sl_mult
            tp = sup - rang * rr
            conf = min(1.0, curr_v / avg_vol / 3)
            return Signal(SignalType.SHORT, sl, tp, confidence=conf,
                          metadata={"breakout": "short", "support": sup, "range": rang})

        return Signal(SignalType.NEUTRAL)


# ─────────────────────────────────────────────────────────────────────────────
# 5. VWAP Mean Reversion Strategy
# ─────────────────────────────────────────────────────────────────────────────

class VWAPMeanReversionStrategy(BaseStrategy):
    """
    VWAP deviation-based entries. Works best in intraday sideways markets.

    Entry:
      LONG:  price < VWAP × (1 - dev_pct), RSI < 40
      SHORT: price > VWAP × (1 + dev_pct), RSI > 60

    Exit:
      TP: VWAP (mean reversion)
      SL: ATR × sl_mult beyond entry
    """

    DEFAULTS = {
        "dev_pct":    0.005,   # 0.5% VWAP deviation
        "rsi_period": 14,
        "rsi_long_max": 40,
        "rsi_short_min": 60,
        "atr_period": 14,
        "atr_sl_mult": 1.5,
    }

    def __init__(self, params: Optional[Dict] = None):
        super().__init__({**self.DEFAULTS, **(params or {})})

    def generate_signal(self, df: pd.DataFrame, symbol: str = "") -> Optional[Signal]:
        if len(df) < 20:
            return None

        vwap_s = self.vwap(df)
        rsi    = self.rsi(df["close"], self.p("rsi_period"))
        atr    = self.atr(df, self.p("atr_period")).iloc[-1]

        price    = df["close"].iloc[-1]
        vwap_val = vwap_s.iloc[-1]
        curr_rsi = rsi.iloc[-1]
        dev_pct  = self.p("dev_pct")

        if pd.isna(vwap_val):
            return Signal(SignalType.NEUTRAL)

        long_level  = vwap_val * (1 - dev_pct)
        short_level = vwap_val * (1 + dev_pct)

        if price < long_level and curr_rsi < self.p("rsi_long_max"):
            sl   = price - atr * self.p("atr_sl_mult")
            tp   = vwap_val
            conf = min(1.0, (long_level - price) / (vwap_val * dev_pct) * 0.5 + 0.5)
            return Signal(SignalType.LONG, sl, tp, confidence=conf,
                          metadata={"vwap": vwap_val, "rsi": curr_rsi})

        if price > short_level and curr_rsi > self.p("rsi_short_min"):
            sl   = price + atr * self.p("atr_sl_mult")
            tp   = vwap_val
            conf = min(1.0, (price - short_level) / (vwap_val * dev_pct) * 0.5 + 0.5)
            return Signal(SignalType.SHORT, sl, tp, confidence=conf,
                          metadata={"vwap": vwap_val, "rsi": curr_rsi})

        return Signal(SignalType.NEUTRAL)


# ─────────────────────────────────────────────────────────────────────────────
# 6. AI-Filtered Strategy (ML-style signal scoring)
# ─────────────────────────────────────────────────────────────────────────────

class AIFilteredStrategy(BaseStrategy):
    """
    Wraps any BaseStrategy and adds multi-factor signal scoring:
      - Trend alignment (EMA 50/200 macro trend)
      - Momentum (ROC, rate of change)
      - Volatility regime (ATR relative to SMA of ATR)
      - Volume confirmation
      - RSI regime check

    Signal is suppressed if composite score < min_score (default 0.6).
    This mimics an AI confidence gate without a trained model.
    """

    def __init__(self, base_strategy: BaseStrategy, min_score: float = 0.55):
        super().__init__()
        self.base    = base_strategy
        self.min_score = min_score

    def _score_signal(self, df: pd.DataFrame, sig: Signal) -> float:
        """Return composite score [0, 1]. Higher = better quality signal."""
        scores = []

        # 1. Macro trend alignment (EMA 50/200)
        if len(df) >= 200:
            ema50  = self.ema(df["close"], 50).iloc[-1]
            ema200 = self.ema(df["close"], 200).iloc[-1]
            price  = df["close"].iloc[-1]
            if sig.type == SignalType.LONG:
                scores.append(1.0 if price > ema50 > ema200 else 0.4)
            elif sig.type == SignalType.SHORT:
                scores.append(1.0 if price < ema50 < ema200 else 0.4)

        # 2. Volume confirmation
        if len(df) >= 20:
            avg_v  = df["volume"].rolling(20).mean().iloc[-1]
            curr_v = df["volume"].iloc[-1]
            scores.append(min(1.0, curr_v / avg_v) if avg_v > 0 else 0.5)

        # 3. ATR regime (avoid very low volatility = fake signals)
        if len(df) >= 30:
            atr    = self.atr(df, 14).iloc[-1]
            atr_ma = self.atr(df, 14).rolling(30).mean().iloc[-1]
            scores.append(min(1.0, atr / atr_ma) if atr_ma > 0 else 0.5)

        # 4. RSI not at extremes (avoid chasing)
        rsi = self.rsi(df["close"], 14).iloc[-1]
        if sig.type == SignalType.LONG:
            scores.append(1.0 if 30 < rsi < 60 else 0.3)
        elif sig.type == SignalType.SHORT:
            scores.append(1.0 if 40 < rsi < 70 else 0.3)

        # 5. Momentum (price ROC over 5 bars)
        if len(df) >= 6:
            roc = (df["close"].iloc[-1] - df["close"].iloc[-6]) / df["close"].iloc[-6]
            if sig.type == SignalType.LONG:
                scores.append(min(1.0, max(0.0, roc * 100 + 0.5)))
            elif sig.type == SignalType.SHORT:
                scores.append(min(1.0, max(0.0, -roc * 100 + 0.5)))

        return sum(scores) / len(scores) if scores else 0.5

    def generate_signal(self, df: pd.DataFrame, symbol: str = "") -> Optional[Signal]:
        sig = self.base.generate_signal(df, symbol)
        if sig is None or sig.type == SignalType.NEUTRAL:
            return sig

        score = self._score_signal(df, sig)
        sig.confidence = score
        sig.metadata["ai_score"] = score

        if score < self.min_score:
            logger.debug("AI filter: score %.2f < %.2f — signal suppressed", score, self.min_score)
            return Signal(SignalType.NEUTRAL)

        logger.debug("AI filter: score %.2f ≥ %.2f — signal passed", score, self.min_score)
        return sig


# ─────────────────────────────────────────────────────────────────────────────
# Strategy Loader
# ─────────────────────────────────────────────────────────────────────────────

STRATEGY_MAP: Dict[str, Type[BaseStrategy]] = {
    "ema_crossover":            EMACrossoverStrategy,
    "bollinger_mean_reversion": BollingerMeanReversionStrategy,
    "smart_money":              SmartMoneyStrategy,
    "breakout":                 BreakoutStrategy,
    "vwap_mean_reversion":      VWAPMeanReversionStrategy,
}


def load_strategy(name: str, params: Optional[Dict] = None, ai_filter: bool = False) -> BaseStrategy:
    """Load a strategy by name with optional AI filter wrapper."""
    cls = STRATEGY_MAP.get(name)
    if cls is None:
        available = ", ".join(STRATEGY_MAP.keys())
        raise ValueError(f"Unknown strategy '{name}'. Available: {available}")
    strat = cls(params)
    if ai_filter:
        strat = AIFilteredStrategy(strat, min_score=0.55)
    return strat


__all__ = [
    "BaseStrategy", "Signal", "SignalType",
    "EMACrossoverStrategy", "BollingerMeanReversionStrategy",
    "SmartMoneyStrategy", "BreakoutStrategy", "VWAPMeanReversionStrategy",
    "AIFilteredStrategy", "load_strategy", "STRATEGY_MAP",
]
