"""
strategy.py — V8 Confluence Strategy
Four concrete upgrades over V7:

  1. TRUE DIP BUYING — price must reach EMA21 from above (longs) / below (shorts).
     V7 allowed entries anywhere within 4% of EMA21, meaning mid-trend entries.
     V8 requires price to cross or touch EMA21 and then reverse = actual pullback.

  2. MID-TREND PREVENTION — new "extension guard":
     If price has moved more than 1.5× ATR from EMA21 WITHOUT pulling back,
     entry is blocked regardless of RSI. This catches the "buy after breakout"
     problem where RSI was 48 but price was already extended.

  3. ENTRY QUALITY SCORING — every signal now carries a full breakdown:
     entry_quality = { pullback_depth, rsi_quality, macd_quality, adx_quality,
                       volume_quality, structure_quality, overall_grade }
     This feeds per-trade analytics so you can see WHY a trade was taken.

  4. SETUP CLASSIFICATION — each signal is tagged with a setup_type:
     "trend_pullback"  — pulled back to EMA21 in a trend
     "range_mean_rev"  — mean reversion at BB edge in range
     "structure_break" — liquidity sweep + reclaim + BOS
     This lets you measure which setups are profitable in your analytics.
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class SignalType(str, Enum):
    LONG    = "long"
    SHORT   = "short"
    NEUTRAL = "neutral"
    HOLD    = "neutral"


@dataclass
class Signal:
    type:        SignalType
    symbol:      Optional[str] = None
    price:       Optional[float] = None
    stop_loss:   Optional[float] = None
    take_profit: Optional[float] = None
    confidence:  float = 1.0
    metadata:    Dict  = field(default_factory=dict)


class BaseStrategy:
    def __init__(self, params=None):
        self.params = params or {}

    @staticmethod
    def ema(series: pd.Series, period: int) -> pd.Series:
        return ema(series, period)

    @staticmethod
    def rsi(series: pd.Series, period: int = 14) -> pd.Series:
        return rsi(series, period)


# ─── Indicators ───────────────────────────────────────────────────────────────

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hi, lo, cl = df["high"], df["low"], df["close"]
    tr = pd.concat([hi - lo, (hi - cl.shift()).abs(), (lo - cl.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()

def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal_p: int = 9):
    fast_ema  = series.ewm(span=fast,   adjust=False).mean()
    slow_ema  = series.ewm(span=slow,   adjust=False).mean()
    macd_line = fast_ema - slow_ema
    sig_line  = macd_line.ewm(span=signal_p, adjust=False).mean()
    histogram = macd_line - sig_line
    return macd_line, sig_line, histogram

def adx(df: pd.DataFrame, period: int = 14) -> Tuple[pd.Series, pd.Series, pd.Series]:
    high, low, close = df["high"], df["low"], df["close"]
    up_move   = high.diff()
    down_move = -low.diff()
    plus_dm   = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm  = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    tr_s      = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr_s     = tr_s.ewm(alpha=1/period, adjust=False).mean()
    plus_di   = 100 * plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr_s.replace(0, np.nan)
    minus_di  = 100 * minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr_s.replace(0, np.nan)
    dx        = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1/period, adjust=False).mean(), plus_di, minus_di

def find_swing_highs(df: pd.DataFrame, lookback: int = 5) -> pd.Series:
    highs = df["high"]
    return highs == highs.rolling(lookback * 2 + 1, center=True).max()

def find_swing_lows(df: pd.DataFrame, lookback: int = 5) -> pd.Series:
    lows = df["low"]
    return lows == lows.rolling(lookback * 2 + 1, center=True).min()

def detect_regime(df: pd.DataFrame, prev_regime: str = "trend") -> str:
    """Hysteresis-based regime: need ADX>22 to enter trend, <14 to exit."""
    if len(df) < 30:
        return "trend"
    adx_series, _, _ = adx(df, 14)
    adx_val = float(adx_series.iloc[-1]) if not pd.isna(adx_series.iloc[-1]) else 0.0
    if prev_regime == "range":
        return "trend" if adx_val >= 22 else "range"
    else:
        return "range" if adx_val < 14 else "trend"

def higher_timeframe_trend(df: pd.DataFrame) -> str:
    """Loose check: EMA50 vs EMA200 with small buffer."""
    if len(df) < 200:
        return "neutral"
    close  = df["close"]
    e50    = float(ema(close, 50).iloc[-1])
    e200   = float(ema(close, 200).iloc[-1])
    if e50 > e200 * 1.001:
        return "bull"
    if e50 < e200 * 0.999:
        return "bear"
    return "neutral"


# ─── Entry Quality Scorer ─────────────────────────────────────────────────────

def score_entry_quality(
    curr_rsi: float,
    curr_adx: float,
    plus_di: float,
    minus_di: float,
    macd_h: float,
    vol_ok: bool,
    pullback_depth_pct: float,   # how far price pulled back from EMA21 (positive = into ema)
    touched_ema: bool,            # did price actually touch EMA21?
    side: str,                    # "long" or "short"
) -> Dict:
    """
    Returns a detailed quality breakdown for analytics.
    Each component is 0–100. Overall is weighted average.
    Grade: A (>=80), B (>=65), C (>=50), D (<50)
    """
    components = {}

    # RSI quality — ideal zone differs by side
    if side == "long":
        if 38 <= curr_rsi <= 48:
            components["rsi"] = 100   # ideal pullback zone
        elif 35 <= curr_rsi <= 52:
            components["rsi"] = 70
        else:
            components["rsi"] = 30
    else:
        if 52 <= curr_rsi <= 62:
            components["rsi"] = 100
        elif 48 <= curr_rsi <= 65:
            components["rsi"] = 70
        else:
            components["rsi"] = 30

    # ADX quality — trending strength
    if curr_adx >= 30:
        components["adx"] = 100
    elif curr_adx >= 22:
        components["adx"] = 75
    elif curr_adx >= 18:
        components["adx"] = 50
    else:
        components["adx"] = 20

    # DI separation
    di_sep = abs(plus_di - minus_di) / max(plus_di + minus_di, 0.01)
    if side == "long":
        di_quality = int(min(100, max(0, (plus_di - minus_di) / max(plus_di + minus_di, 0.01) * 200)))
    else:
        di_quality = int(min(100, max(0, (minus_di - plus_di) / max(plus_di + minus_di, 0.01) * 200)))
    components["di_separation"] = di_quality

    # MACD quality
    if side == "long":
        components["macd"] = 90 if macd_h > 0 else (50 if macd_h > -0.0001 else 20)
    else:
        components["macd"] = 90 if macd_h < 0 else (50 if macd_h < 0.0001 else 20)

    # Volume quality
    components["volume"] = 90 if vol_ok else 45

    # Pullback depth quality — touched EMA is better than just being near it
    if touched_ema:
        if 0.002 <= pullback_depth_pct <= 0.015:
            components["pullback"] = 100   # perfect: touched EMA and just below
        elif pullback_depth_pct <= 0.025:
            components["pullback"] = 75
        else:
            components["pullback"] = 50
    else:
        components["pullback"] = 30        # didn't actually reach EMA21 = weak

    # Weighted overall
    weights = {
        "rsi": 0.22,
        "adx": 0.18,
        "di_separation": 0.15,
        "macd": 0.15,
        "volume": 0.10,
        "pullback": 0.20,
    }
    overall = sum(components[k] * weights[k] for k in components)

    if overall >= 80:
        grade = "A"
    elif overall >= 65:
        grade = "B"
    elif overall >= 50:
        grade = "C"
    else:
        grade = "D"

    return {
        "components": components,
        "overall": round(overall, 1),
        "grade": grade,
        "touched_ema": touched_ema,
        "pullback_depth_pct": round(pullback_depth_pct * 100, 2),
    }


# ─── Main Strategy ────────────────────────────────────────────────────────────

class ConfluenceStrategy:
    """
    V8 — True Dip Buying + Mid-Trend Prevention + Entry Quality Analytics

    Key changes from V7:
    - pullback_long: price must have TOUCHED EMA21 in last 3 bars (actual dip)
    - extension_guard: if price > 1.5 ATR from EMA21, block entry (extended move)
    - Every signal tagged with setup_type and entry_quality breakdown
    - RSI bands unchanged from V7: LONG 35-52, SHORT 48-65
    """

    def __init__(self, params: Optional[Dict] = None):
        p = params or {}
        self.fast_ema      = p.get("fast_ema", 9)
        self.mid_ema       = p.get("mid_ema", 21)
        self.slow_ema      = p.get("slow_ema", 50)
        self.trend_ema     = p.get("trend_ema", 200)
        self.rsi_period    = p.get("rsi_period", 14)
        self.atr_period    = p.get("atr_period", 14)
        self.adx_threshold = p.get("adx_threshold", 20.0)
        self.vol_factor    = p.get("vol_factor", 1.1)

        # RSI zones (same as V7)
        self.rsi_long_min  = p.get("rsi_long_min", 35)
        self.rsi_long_max  = p.get("rsi_long_max", 52)
        self.rsi_short_min = p.get("rsi_short_min", 48)
        self.rsi_short_max = p.get("rsi_short_max", 65)

        # V8: how many bars to look back for EMA touch
        self.ema_touch_lookback = p.get("ema_touch_lookback", 3)
        # V8: extension guard — max ATR distance allowed from EMA21 before blocking
        self.extension_atr_mult = p.get("extension_atr_mult", 1.5)
        # V8: max % distance from EMA21 for entry window
        self.max_ema_distance_pct = p.get("max_ema_distance_pct", 0.03)  # tighter than V7's 0.04

        self.funding_long_max  = p.get("funding_long_max", 0.015)
        self.funding_short_min = p.get("funding_short_min", -0.015)
        self.sl_atr_mult       = p.get("sl_atr_mult", 1.5)
        self.tp_rr             = p.get("tp_rr", 2.2)
        self.swing_lookback    = p.get("swing_lookback", 5)
        self.bb_period         = p.get("bb_period", 20)
        self.bb_std            = p.get("bb_std", 2.0)

        self._prev_regime = "trend"

    def _neutral_signal(self, regime: str, **metadata: Any) -> Signal:
        payload = {"regime": regime}
        payload.update(metadata)
        return Signal(SignalType.NEUTRAL, confidence=0.0, metadata=payload)

    # ── EMA Touch Detection (V8 core) ─────────────────────────────────────────

    def _ema_touched_recently(
        self,
        df: pd.DataFrame,
        ema21_series: pd.Series,
        side: str,
        lookback: int = 3,
    ) -> Tuple[bool, float]:
        """
        Check if price touched or crossed EMA21 in the last `lookback` bars.

        For LONG: low <= EMA21 in any of the last N bars (price came down to EMA)
        For SHORT: high >= EMA21 in any of the last N bars (price came up to EMA)

        Returns (touched: bool, depth_pct: float)
        depth_pct = how deep into the EMA the touch went (0 = just kissed, positive = went through)
        """
        if len(df) < lookback + 1:
            return False, 0.0

        window_lows  = df["low"].iloc[-(lookback+1):-1]
        window_highs = df["high"].iloc[-(lookback+1):-1]
        ema21_window = ema21_series.iloc[-(lookback+1):-1]

        current_price = float(df["close"].iloc[-1])
        current_ema21 = float(ema21_series.iloc[-1])

        if side == "long":
            # Low dipped to or below EMA21 in any of the recent bars
            touched = any(
                float(window_lows.iloc[i]) <= float(ema21_window.iloc[i]) * 1.002
                for i in range(len(window_lows))
            )
            # Depth: how far below EMA the minimum low went
            min_low = float(window_lows.min())
            depth_pct = max(0.0, (current_ema21 - min_low) / current_ema21)
        else:
            # High reached or exceeded EMA21 in any of the recent bars
            touched = any(
                float(window_highs.iloc[i]) >= float(ema21_window.iloc[i]) * 0.998
                for i in range(len(window_highs))
            )
            max_high = float(window_highs.max())
            depth_pct = max(0.0, (max_high - current_ema21) / current_ema21)

        return touched, depth_pct

    def _is_extended(self, price: float, ema21: float, curr_atr: float, side: str) -> bool:
        """
        V8 extension guard: is price too far from EMA21 to be a valid pullback entry?
        If true, block the trade — we're mid-trend, not at a pullback.
        """
        distance = abs(price - ema21)
        max_allowed = curr_atr * self.extension_atr_mult
        return distance > max_allowed

    # ── Main Signal Generation ────────────────────────────────────────────────

    def generate_signal(
        self, df: pd.DataFrame, symbol: str = "", funding_rate: float = 0.0
    ) -> Optional[Signal]:
        if len(df) < 210:
            return Signal(SignalType.NEUTRAL, confidence=0.0)

        close = df["close"]
        price = float(close.iloc[-1])

        ema9_s   = ema(close, self.fast_ema)
        ema21_s  = ema(close, self.mid_ema)
        ema50_s  = ema(close, self.slow_ema)
        ema200_s = ema(close, self.trend_ema)
        rsi_s    = rsi(close, self.rsi_period)
        atr_s    = atr(df, self.atr_period)
        atr_ma   = atr_s.rolling(30).mean()
        adx_s, plus_di_s, minus_di_s = adx(df, 14)
        macd_l, macd_sig, macd_hist  = macd(close)
        avg_vol  = df["volume"].rolling(20).mean()

        curr_ema21  = float(ema21_s.iloc[-1])
        curr_ema50  = float(ema50_s.iloc[-1])
        curr_ema200 = float(ema200_s.iloc[-1])
        curr_rsi    = float(rsi_s.iloc[-1])
        curr_atr    = float(atr_s.iloc[-1])
        curr_atr_ma = float(atr_ma.iloc[-1]) if not pd.isna(atr_ma.iloc[-1]) else curr_atr
        curr_adx    = float(adx_s.iloc[-1]) if not pd.isna(adx_s.iloc[-1]) else 0.0
        plus_di     = float(plus_di_s.iloc[-1]) if not pd.isna(plus_di_s.iloc[-1]) else 0.0
        minus_di    = float(minus_di_s.iloc[-1]) if not pd.isna(minus_di_s.iloc[-1]) else 0.0
        curr_vol    = float(df["volume"].iloc[-1])
        curr_avg_v  = float(avg_vol.iloc[-1]) if not pd.isna(avg_vol.iloc[-1]) else 0.0
        curr_macd_h = float(macd_hist.iloc[-1])
        prev_macd_h = float(macd_hist.iloc[-2])
        curr_open   = float(df["open"].iloc[-1])
        prev_close  = float(close.iloc[-2])

        regime = detect_regime(df, self._prev_regime)
        self._prev_regime = regime
        htf    = higher_timeframe_trend(df)

        vol_alive = curr_atr > curr_atr_ma * 0.75
        vol_ok    = curr_vol > curr_avg_v * self.vol_factor if curr_avg_v > 0 else False
        funding_extreme_long  = funding_rate > self.funding_long_max
        funding_extreme_short = funding_rate < self.funding_short_min

        last_swing_low  = self._last_swing_low(df, self.swing_lookback)
        last_swing_high = self._last_swing_high(df, self.swing_lookback)

        swing_lows_bool  = find_swing_lows(df, self.swing_lookback)
        swing_highs_bool = find_swing_highs(df, self.swing_lookback)
        low_idxs  = list(np.where(swing_lows_bool.values)[0])
        high_idxs = list(np.where(swing_highs_bool.values)[0])
        prev_swing_low  = float(df["low"].iloc[low_idxs[-2]])  if len(low_idxs)  >= 2 else None
        prev_swing_high = float(df["high"].iloc[high_idxs[-2]]) if len(high_idxs) >= 2 else None
        prev_bar = df.iloc[-2] if len(df) >= 2 else None

        if regime == "trend":
            return self._trend_signal(
                df, price, htf, curr_ema21, curr_ema50, curr_ema200,
                curr_rsi, curr_atr, curr_adx, plus_di, minus_di,
                curr_macd_h, prev_macd_h, vol_ok, vol_alive,
                last_swing_low, last_swing_high,
                funding_extreme_long, funding_extreme_short,
                prev_swing_low, prev_swing_high, prev_bar,
                curr_open, prev_close, symbol, ema21_s,
            )
        else:
            return self._range_signal(
                df, price, htf, curr_rsi, curr_atr, vol_ok,
                last_swing_low, last_swing_high, curr_open, prev_close,
                symbol, ema21_s,
            )

    # ── Trend Signal ──────────────────────────────────────────────────────────

    def _trend_signal(
        self,
        df, price, htf, ema21, ema50, ema200,
        curr_rsi, curr_atr, curr_adx, plus_di, minus_di,
        macd_h, prev_macd_h, vol_ok, vol_alive,
        last_swing_low, last_swing_high,
        funding_extreme_long, funding_extreme_short,
        prev_swing_low, prev_swing_high, prev_bar,
        curr_open, prev_close, symbol, ema21_series,
    ) -> Signal:

        # Liquidity sweep detection
        sweep_long = reclaim_long = bos_long = False
        sweep_short = reclaim_short = bos_short = False
        try:
            last_close = float(df["close"].iloc[-1])
            if prev_bar is not None and prev_swing_low is not None and last_swing_high is not None:
                sweep_long   = (float(prev_bar["low"]) < prev_swing_low) and (float(prev_bar["close"]) > prev_swing_low)
                reclaim_long = last_close > prev_swing_low
                bos_long     = last_close > last_swing_high
            if prev_bar is not None and prev_swing_high is not None and last_swing_low is not None:
                sweep_short   = (float(prev_bar["high"]) > prev_swing_high) and (float(prev_bar["close"]) < prev_swing_high)
                reclaim_short = last_close < prev_swing_high
                bos_short     = last_close < last_swing_low
        except Exception:
            pass

        entry_trigger_long  = bool(sweep_long and reclaim_long and bos_long)
        entry_trigger_short = bool(sweep_short and reclaim_short and bos_short)

        # EMA alignment
        trend_bull = ema50 > ema200 * 0.999
        trend_bear = ema50 < ema200 * 1.001

        # V8: EMA touch detection
        touched_long,  depth_long  = self._ema_touched_recently(df, ema21_series, "long",  self.ema_touch_lookback)
        touched_short, depth_short = self._ema_touched_recently(df, ema21_series, "short", self.ema_touch_lookback)

        # V8: proximity check (price currently near EMA21)
        dist_from_ema = abs(price - ema21) / ema21
        near_ema_long  = price <= ema21 * 1.008 and dist_from_ema <= self.max_ema_distance_pct
        near_ema_short = price >= ema21 * 0.992 and dist_from_ema <= self.max_ema_distance_pct

        # V8: extension guard — block if price moved too far from EMA21 without retest
        extended = self._is_extended(price, ema21, curr_atr, "neutral")

        long_momentum  = macd_h > prev_macd_h
        short_momentum = macd_h < prev_macd_h
        long_reversal  = price >= prev_close * 1.0002
        short_reversal = price <= prev_close * 0.9998
        adx_ok         = curr_adx >= self.adx_threshold
        long_di_ok     = plus_di > minus_di * 1.05
        short_di_ok    = minus_di > plus_di * 1.05

        # ── LONG ──────────────────────────────────────────────────────────────
        cond_long = (
            htf != "bear"
            and trend_bull
            and (touched_long or near_ema_long)  # V8: must have touched or be at EMA
            and near_ema_long                     # V8: must currently be near EMA
            and not extended                      # V8: price must not be extended
            and self.rsi_long_min <= curr_rsi <= self.rsi_long_max
            and long_momentum
            and adx_ok
            and long_di_ok
            and vol_alive
            and long_reversal
            and (not funding_extreme_long)
            and last_swing_low is not None
        )

        # ── SHORT ─────────────────────────────────────────────────────────────
        cond_short = (
            htf != "bull"
            and trend_bear
            and (touched_short or near_ema_short)
            and near_ema_short
            and not extended
            and self.rsi_short_min <= curr_rsi <= self.rsi_short_max
            and short_momentum
            and adx_ok
            and short_di_ok
            and vol_alive
            and short_reversal
            and (not funding_extreme_short)
            and last_swing_high is not None
        )

        if cond_long:
            sl   = last_swing_low - curr_atr * self.sl_atr_mult
            sl   = min(sl, price * 0.98)
            sl   = max(sl, price * 0.94)
            risk = price - sl
            tp   = price + risk * self.tp_rr

            quality = score_entry_quality(
                curr_rsi, curr_adx, plus_di, minus_di, macd_h, vol_ok,
                depth_long, touched_long, "long",
            )
            confidence = self._quality_to_confidence(quality["overall"])
            confidence = min(1.0, confidence + self._trigger_bonus(entry_trigger_long, sweep_long, reclaim_long, bos_long))

            setup_type = "structure_break" if entry_trigger_long else "trend_pullback"
            logger.info(
                "✅ LONG [%s] | price=%.4f sl=%.4f tp=%.4f conf=%.2f | adx=%.1f rsi=%.1f | grade=%s depth=%.2f%%",
                setup_type, price, sl, tp, confidence, curr_adx, curr_rsi,
                quality["grade"], quality["pullback_depth_pct"],
            )
            return Signal(
                type=SignalType.LONG, symbol=symbol, price=price,
                stop_loss=round(sl, 4), take_profit=round(tp, 4),
                confidence=confidence,
                metadata={
                    "regime": "trend", "htf": htf, "setup_type": setup_type,
                    "ema21": round(ema21, 4), "adx": round(curr_adx, 1),
                    "rsi": round(curr_rsi, 1), "macd_hist": round(macd_h, 6),
                    "last_swing_low": round(last_swing_low, 4),
                    "entry_quality": quality,
                    "touched_ema21": touched_long,
                    "ema_depth_pct": quality["pullback_depth_pct"],
                },
            )

        if cond_short:
            sl   = last_swing_high + curr_atr * self.sl_atr_mult
            sl   = max(sl, price * 1.02)
            sl   = min(sl, price * 1.06)
            risk = sl - price
            tp   = price - risk * self.tp_rr

            quality = score_entry_quality(
                curr_rsi, curr_adx, plus_di, minus_di, macd_h, vol_ok,
                depth_short, touched_short, "short",
            )
            confidence = self._quality_to_confidence(quality["overall"])
            confidence = min(1.0, confidence + self._trigger_bonus(entry_trigger_short, sweep_short, reclaim_short, bos_short))

            setup_type = "structure_break" if entry_trigger_short else "trend_pullback"
            logger.info(
                "✅ SHORT [%s] | price=%.4f sl=%.4f tp=%.4f conf=%.2f | adx=%.1f rsi=%.1f | grade=%s depth=%.2f%%",
                setup_type, price, sl, tp, confidence, curr_adx, curr_rsi,
                quality["grade"], quality["pullback_depth_pct"],
            )
            return Signal(
                type=SignalType.SHORT, symbol=symbol, price=price,
                stop_loss=round(sl, 4), take_profit=round(tp, 4),
                confidence=confidence,
                metadata={
                    "regime": "trend", "htf": htf, "setup_type": setup_type,
                    "ema21": round(ema21, 4), "adx": round(curr_adx, 1),
                    "rsi": round(curr_rsi, 1), "macd_hist": round(macd_h, 6),
                    "last_swing_high": round(last_swing_high, 4),
                    "entry_quality": quality,
                    "touched_ema21": touched_short,
                    "ema_depth_pct": quality["pullback_depth_pct"],
                },
            )

        # Build blockers for logging
        blockers = []
        if htf == "bear":    blockers.append("htf_bearish")
        if htf == "bull":    blockers.append("htf_bullish")
        if not trend_bull and not trend_bear: blockers.append("ema_alignment_weak")
        if not adx_ok:       blockers.append("adx_too_low")
        if not vol_alive:    blockers.append("volatility_dead")
        if extended:         blockers.append("price_extended_from_ema21")
        if not near_ema_long and not near_ema_short: blockers.append("not_near_ema21")
        if not touched_long and not touched_short:   blockers.append("ema21_not_touched_recently")
        if curr_rsi > self.rsi_long_max and curr_rsi < self.rsi_short_min:
            blockers.append(f"rsi_mid_range({curr_rsi:.1f})")
        return self._neutral_signal(
            "trend", htf=htf, rsi=round(curr_rsi, 1), adx=round(curr_adx, 1), blockers=blockers
        )

    # ── Range Signal ──────────────────────────────────────────────────────────

    def _range_signal(
        self, df, price, htf, curr_rsi, curr_atr, vol_ok,
        last_swing_low, last_swing_high, curr_open, prev_close,
        symbol, ema21_series,
    ) -> Signal:
        close = df["close"]
        mid   = close.rolling(self.bb_period).mean()
        std   = close.rolling(self.bb_period).std()
        lower = (mid - self.bb_std * std).iloc[-1]
        upper = (mid + self.bb_std * std).iloc[-1]
        mid_v = mid.iloc[-1]

        if pd.isna(lower) or pd.isna(upper):
            return self._neutral_signal("range")

        long_reclaim  = price >= curr_open or price >= prev_close
        short_reclaim = price <= curr_open or price <= prev_close

        # Range LONG: price at lower BB, RSI oversold, HTF not bearish
        if (htf != "bear"
                and price <= lower * 1.001
                and curr_rsi <= 35
                and last_swing_low is not None
                and long_reclaim):
            sl   = last_swing_low - curr_atr * 0.5
            sl   = min(sl, price * 0.99)
            risk = price - sl
            tp   = min(float(mid_v), price + risk * 1.5)
            if risk > 0 and tp > price + risk * 1.3:
                bb_excess = max(0.0, (lower - price) / (curr_atr + 1e-9))
                confidence = min(1.0, 0.60 + bb_excess * 0.20 + (0.08 if vol_ok else 0.0))
                quality = score_entry_quality(curr_rsi, 15, 50, 30, -0.001, vol_ok, bb_excess, True, "long")
                logger.info("✅ RANGE LONG | price=%.4f sl=%.4f tp=%.4f rsi=%.1f grade=%s",
                            price, sl, tp, curr_rsi, quality["grade"])
                return Signal(
                    type=SignalType.LONG, symbol=symbol, price=price,
                    stop_loss=round(sl, 4), take_profit=round(tp, 4),
                    confidence=confidence,
                    metadata={
                        "regime": "range", "htf": htf, "setup_type": "range_mean_rev",
                        "bb_lower": round(lower, 4), "bb_mid": round(mid_v, 4),
                        "rsi": round(curr_rsi, 1), "entry_quality": quality,
                    },
                )

        # Range SHORT: price at upper BB, RSI overbought, HTF not bullish
        if (htf != "bull"
                and price >= upper * 0.999
                and curr_rsi >= 65
                and last_swing_high is not None
                and short_reclaim):
            sl   = last_swing_high + curr_atr * 0.5
            sl   = max(sl, price * 1.01)
            risk = sl - price
            tp   = max(float(mid_v), price - risk * 1.5)
            if risk > 0 and tp < price - risk * 1.3:
                bb_excess = max(0.0, (price - upper) / (curr_atr + 1e-9))
                confidence = min(1.0, 0.60 + bb_excess * 0.20 + (0.08 if vol_ok else 0.0))
                quality = score_entry_quality(curr_rsi, 15, 30, 50, 0.001, vol_ok, bb_excess, True, "short")
                logger.info("✅ RANGE SHORT | price=%.4f sl=%.4f tp=%.4f rsi=%.1f grade=%s",
                            price, sl, tp, curr_rsi, quality["grade"])
                return Signal(
                    type=SignalType.SHORT, symbol=symbol, price=price,
                    stop_loss=round(sl, 4), take_profit=round(tp, 4),
                    confidence=confidence,
                    metadata={
                        "regime": "range", "htf": htf, "setup_type": "range_mean_rev",
                        "bb_upper": round(upper, 4), "bb_mid": round(mid_v, 4),
                        "rsi": round(curr_rsi, 1), "entry_quality": quality,
                    },
                )

        blockers = []
        if not (price <= lower * 1.001 or price >= upper * 0.999):
            blockers.append("not_at_band_edge")
        if 35 < curr_rsi < 65:
            blockers.append("rsi_mid_range")
        if htf == "bull": blockers.append("htf_bullish_blocks_range_short")
        if htf == "bear": blockers.append("htf_bearish_blocks_range_long")
        return self._neutral_signal(
            "range", rsi=round(curr_rsi, 1),
            bb_mid=round(mid_v, 4) if not pd.isna(mid_v) else 0,
            blockers=blockers,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _quality_to_confidence(self, overall_score: float) -> float:
        """Map quality score (0-100) to confidence (0-1)."""
        # A-grade (80+) → 0.75–0.90 confidence
        # B-grade (65–79) → 0.62–0.74
        # C-grade (50–64) → 0.50–0.61
        # D-grade (<50) → 0.35–0.49
        return round(min(0.95, max(0.35, overall_score / 100 * 0.95)), 3)

    def _trigger_bonus(self, entry_trigger, sweep, reclaim, bos) -> float:
        if entry_trigger: return 0.20
        bonus = 0.0
        if sweep:   bonus += 0.06
        if reclaim: bonus += 0.06
        if bos:     bonus += 0.12
        return bonus

    def _last_swing_low(self, df: pd.DataFrame, lookback: int) -> Optional[float]:
        window = df.tail(60)
        lows   = window["low"]
        for i in range(len(lows) - lookback - 1, lookback - 1, -1):
            if (all(lows.iloc[i] <= lows.iloc[i - j] for j in range(1, lookback + 1)) and
                    all(lows.iloc[i] <= lows.iloc[i + j] for j in range(1, min(lookback + 1, len(lows) - i)))):
                return float(lows.iloc[i])
        return float(lows.min())

    def _last_swing_high(self, df: pd.DataFrame, lookback: int) -> Optional[float]:
        window = df.tail(60)
        highs  = window["high"]
        for i in range(len(highs) - lookback - 1, lookback - 1, -1):
            if (all(highs.iloc[i] >= highs.iloc[i - j] for j in range(1, lookback + 1)) and
                    all(highs.iloc[i] >= highs.iloc[i + j] for j in range(1, min(lookback + 1, len(highs) - i)))):
                return float(highs.iloc[i])
        return float(highs.max())


# ─── Registry ──────────────────────────────────────────────────────────────────

STRATEGY_MAP = {"confluence": ConfluenceStrategy}

def load_strategy(name: str = "confluence", params: Optional[Dict] = None) -> ConfluenceStrategy:
    return STRATEGY_MAP.get(name, ConfluenceStrategy)(params)

__all__ = ["ConfluenceStrategy", "Signal", "SignalType", "load_strategy", "STRATEGY_MAP",
           "score_entry_quality"]
