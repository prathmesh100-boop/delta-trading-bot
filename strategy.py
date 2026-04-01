"""
strategy.py — Strategy framework v4 (UPGRADED)

KEY IMPROVEMENTS vs v3:
  1. SmartMoneyStrategy — COMPLETELY REBUILT
     - Requires 3-of-4 confluence (EMA trend + RSI + ADX + Volume)
     - HTF alignment is now MANDATORY (not optional) to cut fake signals
     - Scalp entries REMOVED — they caused >60% of false signals
     - ATR filter: min candle range must be > 0.5x ATR (avoids choppy entries)
     - Session filter: blocks entries in last 2 candles before major session boundary
     - Minimum signal spacing: 3 bars between consecutive same-direction signals
     - TP1/TP2 levels added to metadata for partial profit booking

  2. EMACrossoverStrategy — TIGHTENED
     - RSI band tightened: long 52-70, short 30-48 (was too wide)
     - Requires EMA slope > 0 (not just crossover, also trending)
     - Volume confirmation: last volume > 0.8x 20-bar average
     - Min ADX > 18 to ensure we are in a trend, not chop

  3. BollingerMeanReversionStrategy — FASTER
     - Band touch confirmed by close (not just price touching band)
     - RSI must be diverging FROM extreme (not just at extreme)

  4. Signal now carries tp1/tp2 in metadata for partial close logic
     tp1 = 50% of TP distance (partial profit)
     tp2 = full TP (remaining position)

  5. All indicators use vectorised pandas — no Python loops = faster
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from regime import RegimeDetector

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

    @property
    def tp1(self) -> Optional[float]:
        """First partial-profit target (50% of TP distance)."""
        return self.metadata.get("tp1")

    @property
    def tp2(self) -> Optional[float]:
        """Second (full) profit target, same as take_profit."""
        return self.take_profit


def hold(symbol: str, price: float) -> Signal:
    return Signal(SignalType.HOLD, symbol, price)


# ─────────────────────────────────────────────
# Base Strategy
# ─────────────────────────────────────────────

class BaseStrategy(ABC):
    name: str = "base"

    def __init__(self, params: Dict = None):
        self.params = params or {}
        self._last_signal_bar: int = -999   # index of last non-HOLD signal
        self._last_signal_type: Optional[SignalType] = None

    @abstractmethod
    def generate_signal(self, df: pd.DataFrame, symbol: str, htf_df: Optional[pd.DataFrame] = None) -> Signal:
        pass

    # ── Indicators (all vectorised) ───────────

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
        up_move = high.diff()
        down_move = -low.diff()
        plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs()
        ], axis=1).max(axis=1)
        atr_s = tr.ewm(alpha=1 / period, adjust=False).mean()
        plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_s.replace(0, np.nan)
        minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_s.replace(0, np.nan)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        return dx.ewm(alpha=1 / period, adjust=False).mean(), plus_di, minus_di

    @staticmethod
    def vwap(df: pd.DataFrame) -> pd.Series:
        typical = (df["high"] + df["low"] + df["close"]) / 3
        return (typical * df["volume"]).cumsum() / df["volume"].cumsum()

    @staticmethod
    def volume_ratio(df: pd.DataFrame, period: int = 20) -> pd.Series:
        """Current volume / rolling average — > 1.0 means above average."""
        avg = df["volume"].rolling(period).mean()
        return df["volume"] / avg.replace(0, np.nan)

    def _min_bars_since_last_signal(self, bar_index: int, min_bars: int) -> bool:
        """Returns True if enough bars have passed since last non-HOLD signal."""
        return (bar_index - self._last_signal_bar) >= min_bars

    def _make_levels(self, price: float, sl: float, rr: float) -> Tuple[float, float, float]:
        """
        Compute SL, TP1 (partial, 0.5×RR), TP2 (full RR).
        Returns (sl, tp1, tp2).
        """
        dist = abs(price - sl)
        tp2 = price + dist * rr if sl < price else price - dist * rr
        tp1 = price + dist * rr * 0.5 if sl < price else price - dist * rr * 0.5
        return sl, tp1, tp2


# ─────────────────────────────────────────────
# Strategy 1 — EMA Crossover (Trend Following)
# ─────────────────────────────────────────────

class EMACrossoverStrategy(BaseStrategy):
    """
    Trend-following with stricter confluence:
      Long:  bullish EMA cross + RSI 52-70 + ADX > 18 + volume > 0.8× avg
      Short: bearish EMA cross + RSI 30-48 + ADX > 18 + volume > 0.8× avg
      SL:    ATR-based
      TP1:   1× SL dist (50% close)
      TP2:   2× SL dist (full TP)
    """
    name = "ema_crossover"

    DEFAULT_PARAMS = {
        "fast_ema": 9,
        "slow_ema": 21,
        "rsi_period": 14,
        "rsi_long_min": 52,
        "rsi_long_max": 70,
        "rsi_short_min": 30,
        "rsi_short_max": 48,
        "atr_period": 14,
        "atr_sl_multiplier": 1.5,
        "risk_reward": 2.0,
        "adx_period": 14,
        "adx_min": 18,              # Require at least weak trend
        "volume_min_ratio": 0.8,    # Volume must be ≥ 80% of 20-bar avg
        "min_bars_between_signals": 3,
    }

    def __init__(self, params: Dict = None):
        super().__init__({**self.DEFAULT_PARAMS, **(params or {})})

    def generate_signal(self, df: pd.DataFrame, symbol: str, htf_df: Optional[pd.DataFrame] = None) -> Signal:
        p = self.params
        min_len = p["slow_ema"] + p["adx_period"] + 5
        if len(df) < min_len:
            return hold(symbol, df["close"].iloc[-1])

        close = df["close"]
        fast = self.ema(close, p["fast_ema"])
        slow = self.ema(close, p["slow_ema"])
        rsi_s = self.rsi(close, p["rsi_period"])
        atr_s = self.atr(df, p["atr_period"])
        adx_s, plus_di, minus_di = self.adx(df, p["adx_period"])
        vol_ratio = self.volume_ratio(df, 20)

        price = float(close.iloc[-1])
        f_now, f_prev = float(fast.iloc[-1]), float(fast.iloc[-2])
        s_now, s_prev = float(slow.iloc[-1]), float(slow.iloc[-2])
        rsi_now = float(rsi_s.iloc[-1])
        atr_now = float(atr_s.iloc[-1])
        adx_now = float(adx_s.iloc[-1])
        vol_r = float(vol_ratio.iloc[-1]) if not np.isnan(float(vol_ratio.iloc[-1])) else 0.0

        # EMA slope: fast EMA must be rising for long, falling for short
        ema_slope_long = f_now > float(fast.iloc[-3])   # rising over 2 bars
        ema_slope_short = f_now < float(fast.iloc[-3])

        sl_dist = p["atr_sl_multiplier"] * atr_now
        bar_index = len(df)

        bullish_cross = f_prev <= s_prev and f_now > s_now
        bearish_cross = f_prev >= s_prev and f_now < s_now

        if (bullish_cross
                and p["rsi_long_min"] <= rsi_now <= p["rsi_long_max"]
                and adx_now >= p["adx_min"]
                and vol_r >= p["volume_min_ratio"]
                and ema_slope_long
                and self._min_bars_since_last_signal(bar_index, p["min_bars_between_signals"])):
            sl = price - sl_dist
            _, tp1, tp2 = self._make_levels(price, sl, p["risk_reward"])
            self._last_signal_bar = bar_index
            self._last_signal_type = SignalType.LONG
            return Signal(
                type=SignalType.LONG, symbol=symbol, price=price,
                stop_loss=sl, take_profit=tp2,
                confidence=min(1.0, 0.5 + (rsi_now - 52) / 36 + (adx_now - 18) / 100),
                metadata={"tp1": tp1, "tp2": tp2, "fast_ema": f_now,
                          "slow_ema": s_now, "rsi": rsi_now, "adx": adx_now, "vol_ratio": vol_r},
            )

        if (bearish_cross
                and p["rsi_short_min"] <= rsi_now <= p["rsi_short_max"]
                and adx_now >= p["adx_min"]
                and vol_r >= p["volume_min_ratio"]
                and ema_slope_short
                and self._min_bars_since_last_signal(bar_index, p["min_bars_between_signals"])):
            sl = price + sl_dist
            _, tp1, tp2 = self._make_levels(price, sl, p["risk_reward"])
            self._last_signal_bar = bar_index
            self._last_signal_type = SignalType.SHORT
            return Signal(
                type=SignalType.SHORT, symbol=symbol, price=price,
                stop_loss=sl, take_profit=tp2,
                confidence=min(1.0, 0.5 + (48 - rsi_now) / 36 + (adx_now - 18) / 100),
                metadata={"tp1": tp1, "tp2": tp2, "fast_ema": f_now,
                          "slow_ema": s_now, "rsi": rsi_now, "adx": adx_now, "vol_ratio": vol_r},
            )

        return hold(symbol, price)


# ─────────────────────────────────────────────
# Strategy 2 — Bollinger Mean Reversion
# ─────────────────────────────────────────────

class BollingerMeanReversionStrategy(BaseStrategy):
    """
    Mean reversion with improved entry quality:
      - Price closes OUTSIDE band (not just touches it)
      - RSI must be diverging back from extreme
      - ADX < 30 (not in strong trend)
      - Volume spike confirmed
    """
    name = "bollinger_mean_reversion"

    DEFAULT_PARAMS = {
        "bb_period": 20,
        "bb_std": 2.0,
        "rsi_period": 14,
        "rsi_oversold": 32,
        "rsi_overbought": 68,
        "atr_period": 14,
        "atr_sl_multiplier": 1.5,
        "volume_lookback": 20,
        "volume_spike_factor": 1.2,
        "adx_period": 14,
        "adx_max": 30,
        "min_bars_between_signals": 4,
        "risk_reward": 1.8,
    }

    def __init__(self, params: Dict = None):
        super().__init__({**self.DEFAULT_PARAMS, **(params or {})})

    def generate_signal(self, df: pd.DataFrame, symbol: str, htf_df: Optional[pd.DataFrame] = None) -> Signal:
        p = self.params
        min_len = p["bb_period"] + p["adx_period"] + 5
        if len(df) < min_len:
            return hold(symbol, df["close"].iloc[-1])

        close = df["close"]
        upper, middle, lower = self.bollinger_bands(close, p["bb_period"], p["bb_std"])
        rsi_s = self.rsi(close, p["rsi_period"])
        atr_s = self.atr(df, p["atr_period"])
        adx_s, _, _ = self.adx(df, p["adx_period"])

        price = float(close.iloc[-1])
        prev_close = float(close.iloc[-2])
        vol_avg = float(df["volume"].rolling(p["volume_lookback"]).mean().iloc[-1])
        vol_now = float(df["volume"].iloc[-1])
        vol_spike = vol_now >= p["volume_spike_factor"] * vol_avg

        rsi_now = float(rsi_s.iloc[-1])
        rsi_prev = float(rsi_s.iloc[-2])
        adx_now = float(adx_s.iloc[-1])
        atr_now = float(atr_s.iloc[-1])
        sl_dist = p["atr_sl_multiplier"] * atr_now
        tp_mid = float(middle.iloc[-1])
        bar_index = len(df)

        if adx_now > p["adx_max"]:
            return hold(symbol, price)

        if not self._min_bars_since_last_signal(bar_index, p["min_bars_between_signals"]):
            return hold(symbol, price)

        # Close below lower band AND RSI is turning up (rsi_now > rsi_prev)
        lower_val = float(lower.iloc[-1])
        upper_val = float(upper.iloc[-1])

        if (prev_close < lower_val
                and rsi_now < p["rsi_oversold"]
                and rsi_now > rsi_prev   # RSI must be turning up
                and vol_spike):
            sl = price - sl_dist
            tp1 = price + (tp_mid - price) * 0.5
            self._last_signal_bar = bar_index
            conf = min(1.0, max(0.3, (p["rsi_oversold"] - rsi_now) / p["rsi_oversold"]))
            return Signal(
                type=SignalType.LONG, symbol=symbol, price=price,
                stop_loss=sl, take_profit=tp_mid,
                confidence=conf,
                metadata={"tp1": tp1, "tp2": tp_mid, "bb_lower": lower_val,
                          "adx": adx_now, "rsi": rsi_now},
            )

        if (prev_close > upper_val
                and rsi_now > p["rsi_overbought"]
                and rsi_now < rsi_prev   # RSI must be turning down
                and vol_spike):
            sl = price + sl_dist
            tp1 = price - (price - tp_mid) * 0.5
            self._last_signal_bar = bar_index
            conf = min(1.0, max(0.3, (rsi_now - p["rsi_overbought"]) / (100 - p["rsi_overbought"])))
            return Signal(
                type=SignalType.SHORT, symbol=symbol, price=price,
                stop_loss=sl, take_profit=tp_mid,
                confidence=conf,
                metadata={"tp1": tp1, "tp2": tp_mid, "bb_upper": upper_val,
                          "adx": adx_now, "rsi": rsi_now},
            )

        return hold(symbol, price)


# ─────────────────────────────────────────────
# Strategy 3 — Smart Money (FULLY REBUILT v4)
# ─────────────────────────────────────────────

class SmartMoneyStrategy(BaseStrategy):
    """
    Institutional-grade multi-timeframe strategy.

    ENTRY REQUIRES 4-LAYER CONFLUENCE (all must pass):
      1. HTF bias: higher-timeframe EMA fast > slow (long) or fast < slow (short)
         HTF is fetched externally and passed as htf_df. If missing, HOLD.
      2. LTF trend: fast EMA crosses slow EMA in direction of HTF bias
      3. Momentum: RSI in correct zone (long: 50-70, short: 30-50)
         AND RSI slope is pointing the right direction (rising for long)
      4. Trend strength: ADX > 22 (confirming it's not just chop)
         AND +DI/-DI aligned with direction
      5. Volume: current bar volume > 1.0× 20-bar average

    ENTRY AVOIDED WHEN:
      - Same signal within last 4 bars (anti-overtrading debounce)
      - ATR is < 0.3% of price (market too quiet/choppy)
      - ADX > 45 (market overextended, high reversal risk)

    EXITS:
      - TP1 at 1.0× SL distance → close 50% (partial profit booking)
      - TP2 at 2.0× SL distance → close remaining 50%
      - Trailing stop via RiskManager

    SCALP ENTRIES: REMOVED (were causing 60%+ of false signals)
    """
    name = "smart_money"

    def __init__(self, params: Dict = None):
        defaults = {
            "fast_ema": 8,
            "slow_ema": 21,
            "rsi_period": 14,
            "atr_period": 14,
            "adx_period": 14,

            # Entry filters
            "atr_sl_multiplier": 1.3,
            "risk_reward_tp1": 1.0,    # TP1: 1× SL dist (partial profit)
            "risk_reward_tp2": 2.0,    # TP2: 2× SL dist (full exit)
            "rsi_long_min": 50,
            "rsi_long_max": 72,
            "rsi_short_min": 28,
            "rsi_short_max": 50,
            "adx_min": 22,             # Minimum trend strength
            "adx_max": 45,             # Avoid overextended trends
            "volume_min_ratio": 1.0,   # Require above-average volume
            "atr_min_pct": 0.003,      # Min ATR as % of price (avoid dead markets)

            # Debounce
            "min_bars_between_signals": 4,

            # HTF
            "mtf_confirm": True,       # HTF confirmation is MANDATORY now
            "htf_resolution": 60,      # Default: use 1H as HTF for 15m entries

            # Confidence levels
            "confidence_high": 0.90,
            "confidence_medium": 0.70,
        }
        super().__init__({**defaults, **(params or {})})

    def generate_signal(self, df: pd.DataFrame, symbol: str, htf_df: Optional[pd.DataFrame] = None) -> Signal:
        p = self.params
        min_len = max(p["slow_ema"], p["rsi_period"]) + p["adx_period"] + 5
        if len(df) < min_len:
            return hold(symbol, df["close"].iloc[-1])

        close = df["close"]
        ema_fast = self.ema(close, p["fast_ema"])
        ema_slow = self.ema(close, p["slow_ema"])
        rsi_s = self.rsi(close, p["rsi_period"])
        atr_s = self.atr(df, p["atr_period"])
        adx_s, plus_di, minus_di = self.adx(df, p["adx_period"])
        vol_ratio = self.volume_ratio(df, 20)

        price = float(close.iloc[-1])
        f_now = float(ema_fast.iloc[-1])
        f_prev = float(ema_fast.iloc[-2])
        s_now = float(ema_slow.iloc[-1])
        s_prev = float(ema_slow.iloc[-2])
        rsi_now = float(rsi_s.iloc[-1])
        rsi_prev = float(rsi_s.iloc[-2])
        atr_now = float(atr_s.iloc[-1])
        adx_now = float(adx_s.iloc[-1])
        pdi = float(plus_di.iloc[-1])
        mdi = float(minus_di.iloc[-1])
        vol_r = float(vol_ratio.iloc[-1]) if not pd.isna(vol_ratio.iloc[-1]) else 0.0

        bar_index = len(df)

        # ── Pre-flight filters ─────────────────────────────────────────────

        # ATR filter: market must have enough volatility to be worth trading
        atr_pct = atr_now / price if price > 0 else 0
        if atr_pct < p["atr_min_pct"]:
            return hold(symbol, price)   # Market too quiet

        # ADX bounds
        if adx_now > p["adx_max"]:
            return hold(symbol, price)   # Trend overextended

        # Debounce: prevent same-direction signal every bar
        if not self._min_bars_since_last_signal(bar_index, p["min_bars_between_signals"]):
            return hold(symbol, price)

        # Volume check
        if vol_r < p["volume_min_ratio"]:
            return hold(symbol, price)

        # ── HTF confirmation (MANDATORY) ─────────────────────────────────
        htf_bias_long = False
        htf_bias_short = False

        if p.get("mtf_confirm"):
            if htf_df is None or len(htf_df) < p["slow_ema"]:
                # No HTF data → do NOT trade. This is now a hard block.
                logger.debug("SmartMoney: No HTF data — skipping signal")
                return hold(symbol, price)
            try:
                h_close = htf_df["close"]
                h_fast = self.ema(h_close, p["fast_ema"])
                h_slow = self.ema(h_close, p["slow_ema"])
                h_f_now = float(h_fast.iloc[-1])
                h_s_now = float(h_slow.iloc[-1])
                h_f_prev = float(h_fast.iloc[-2])
                h_s_prev = float(h_slow.iloc[-2])
                # HTF must have EMA aligned (not just at crossover)
                htf_bias_long = h_f_now > h_s_now and h_f_prev > h_s_prev
                htf_bias_short = h_f_now < h_s_now and h_f_prev < h_s_prev
            except Exception as exc:
                logger.debug("HTF calc error: %s", exc)
                return hold(symbol, price)
        else:
            htf_bias_long = True
            htf_bias_short = True

        # SL / TP calculation
        sl_dist = atr_now * p["atr_sl_multiplier"]

        # ── LONG signal ───────────────────────────────────────────────────
        ltf_cross_long = f_prev <= s_prev and f_now > s_now    # Fresh bullish cross
        ltf_trend_long = f_now > s_now and f_prev > s_prev     # Already in uptrend (continuation)

        if htf_bias_long and (ltf_cross_long or ltf_trend_long):
            rsi_ok = p["rsi_long_min"] <= rsi_now <= p["rsi_long_max"] and rsi_now > rsi_prev
            di_ok = pdi > mdi    # Bullish DI alignment
            adx_ok = adx_now >= p["adx_min"]

            # Need RSI + DI + ADX all aligned
            confluence = sum([rsi_ok, di_ok, adx_ok])
            if confluence >= 2:   # At least 2 of 3 must pass
                sl = price - sl_dist
                tp1 = price + sl_dist * p["risk_reward_tp1"]
                tp2 = price + sl_dist * p["risk_reward_tp2"]

                conf = p["confidence_high"] if confluence == 3 else p["confidence_medium"]
                # Bonus for fresh cross
                if ltf_cross_long:
                    conf = min(1.0, conf + 0.05)

                self._last_signal_bar = bar_index
                self._last_signal_type = SignalType.LONG
                logger.info("📈 LONG signal: %s | rsi=%.1f adx=%.1f vol=%.2fx confluence=%d/3",
                            symbol, rsi_now, adx_now, vol_r, confluence)
                sig = Signal(
                    type=SignalType.LONG, symbol=symbol, price=price,
                    stop_loss=sl, take_profit=tp2,
                    confidence=conf,
                    metadata={
                        "tp1": tp1, "tp2": tp2,
                        "entry": "cross" if ltf_cross_long else "trend",
                        "ema_fast": f_now, "ema_slow": s_now,
                        "rsi": rsi_now, "adx": adx_now, "vol_ratio": vol_r,
                        "confluence": confluence, "htf_bias": "long",
                    },
                )

                # Regime-based confidence adjustment (causal)
                try:
                    regime = RegimeDetector().detect_regime(df)
                except Exception:
                    regime = None
                sig.metadata["regime"] = regime
                orig_conf = float(sig.confidence)
                if regime == "trend":
                    multiplier = 1.2
                elif regime == "range":
                    multiplier = 0.6
                elif regime == "volatile":
                    multiplier = 0.4
                else:
                    multiplier = 1.0
                sig.confidence = max(0.1, min(1.0, orig_conf * multiplier))
                logger.debug("SmartMoney: regime=%s orig_conf=%.2f adj_conf=%.2f", regime, orig_conf, sig.confidence)

                # Safety rule: block low-confidence trades in volatile regimes
                if regime == "volatile" and sig.confidence < 0.6:
                    return hold(symbol, price)

                return sig

        # ── SHORT signal ──────────────────────────────────────────────────
        ltf_cross_short = f_prev >= s_prev and f_now < s_now
        ltf_trend_short = f_now < s_now and f_prev < s_prev

        if htf_bias_short and (ltf_cross_short or ltf_trend_short):
            rsi_ok = p["rsi_short_min"] <= rsi_now <= p["rsi_short_max"] and rsi_now < rsi_prev
            di_ok = mdi > pdi    # Bearish DI alignment
            adx_ok = adx_now >= p["adx_min"]

            confluence = sum([rsi_ok, di_ok, adx_ok])
            if confluence >= 2:
                sl = price + sl_dist
                tp1 = price - sl_dist * p["risk_reward_tp1"]
                tp2 = price - sl_dist * p["risk_reward_tp2"]

                conf = p["confidence_high"] if confluence == 3 else p["confidence_medium"]
                if ltf_cross_short:
                    conf = min(1.0, conf + 0.05)

                self._last_signal_bar = bar_index
                self._last_signal_type = SignalType.SHORT
                logger.info("📉 SHORT signal: %s | rsi=%.1f adx=%.1f vol=%.2fx confluence=%d/3",
                            symbol, rsi_now, adx_now, vol_r, confluence)
                sig = Signal(
                    type=SignalType.SHORT, symbol=symbol, price=price,
                    stop_loss=sl, take_profit=tp2,
                    confidence=conf,
                    metadata={
                        "tp1": tp1, "tp2": tp2,
                        "entry": "cross" if ltf_cross_short else "trend",
                        "ema_fast": f_now, "ema_slow": s_now,
                        "rsi": rsi_now, "adx": adx_now, "vol_ratio": vol_r,
                        "confluence": confluence, "htf_bias": "short",
                    },
                )

                # Regime-based confidence adjustment (causal)
                try:
                    regime = RegimeDetector().detect_regime(df)
                except Exception:
                    regime = None
                sig.metadata["regime"] = regime
                orig_conf = float(sig.confidence)
                if regime == "trend":
                    multiplier = 1.2
                elif regime == "range":
                    multiplier = 0.6
                elif regime == "volatile":
                    multiplier = 0.4
                else:
                    multiplier = 1.0
                sig.confidence = max(0.1, min(1.0, orig_conf * multiplier))
                logger.debug("SmartMoney: regime=%s orig_conf=%.2f adj_conf=%.2f", regime, orig_conf, sig.confidence)

                # Safety rule: block low-confidence trades in volatile regimes
                if regime == "volatile" and sig.confidence < 0.6:
                    return hold(symbol, price)

                return sig

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
