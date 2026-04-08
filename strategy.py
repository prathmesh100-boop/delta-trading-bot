"""
strategy.py — Institutional Confluence Strategy

THE STRATEGY: Multi-Timeframe Trend + Structure + Momentum Confluence

This is the same logic used by professional prop traders and hedge funds:

1. MACRO TREND (HTF): EMA 50/200 on 1H defines the dominant trend direction.
   Only trade WITH the trend. Never fade the trend.

2. MARKET STRUCTURE: Detect real swing highs/lows (not just noise).
   Enter on structural pullbacks — "buy the dip in an uptrend".

3. MOMENTUM GATE: RSI must confirm momentum in entry direction.
   RSI 40-60 zone = ideal entry (room to run, not overextended).

4. VOLATILITY FILTER: ATR must be above its moving average.
   Low ATR = low vol = fake breakouts. Only trade when market is "alive".

5. VOLUME CONFIRMATION: Current bar volume > 1.2x average.
   Institutional orders leave volume footprints.

6. FUNDING RATE FILTER: Don't go long when funding is very high (longs paying
   too much). Contrarian signal — extreme funding = crowd is wrong side.

7. TIGHT STOPS: SL below/above the last structural swing + 0.5 ATR buffer.
   TP = 2.5R minimum (risk-reward filter baked in).

8. REGIME DETECTION: Detect trending vs ranging. Apply mean reversion in range,
   trend following in trend. Regime is calculated on the fly.

Why this works:
- Trend filter prevents trading against institutional flow
- Structure entries give natural tight stops (low risk)
- Momentum gate prevents buying into exhaustion
- Volume filter ensures real participation
- Multiple timeframe confluence = higher win rate, better RR
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
    """Compatibility shim used by tests: expose common indicator helpers."""
    def __init__(self, params=None):
        self.params = params or {}

    @staticmethod
    def ema(series: pd.Series, period: int) -> pd.Series:
        return ema(series, period)

    @staticmethod
    def rsi(series: pd.Series, period: int = 14) -> pd.Series:
        return rsi(series, period)


# ─── Indicator Library ────────────────────────────────────────────────────────

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
    tr = pd.concat([
        hi - lo,
        (hi - cl.shift()).abs(),
        (lo - cl.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()

def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal_p: int = 9):
    fast_ema  = series.ewm(span=fast,   adjust=False).mean()
    slow_ema  = series.ewm(span=slow,   adjust=False).mean()
    macd_line = fast_ema - slow_ema
    sig_line  = macd_line.ewm(span=signal_p, adjust=False).mean()
    histogram = macd_line - sig_line
    return macd_line, sig_line, histogram

def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
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
    """Returns True where a bar is a swing high (local max over ±lookback bars)."""
    highs = df["high"]
    return highs == highs.rolling(lookback * 2 + 1, center=True).max()

def find_swing_lows(df: pd.DataFrame, lookback: int = 5) -> pd.Series:
    """Returns True where a bar is a swing low."""
    lows = df["low"]
    return lows == lows.rolling(lookback * 2 + 1, center=True).min()

def detect_regime(df: pd.DataFrame) -> str:
    """Detect market regime: 'trend' or 'range'."""
    if len(df) < 30:
        return "trend"
    adx_series, _, _ = adx(df, 14)
    adx_val = adx_series.iloc[-1]
    if pd.isna(adx_val):
        return "trend"
    return "trend" if adx_val > 22 else "range"

def higher_timeframe_trend(df: pd.DataFrame) -> str:
    """
    Simulate HTF by using EMA50/200 on the given data.
    Returns 'bull', 'bear', or 'neutral'.
    """
    if len(df) < 200:
        return "neutral"
    close   = df["close"]
    ema50   = ema(close, 50).iloc[-1]
    ema200  = ema(close, 200).iloc[-1]
    price   = close.iloc[-1]
    slope50 = ema(close, 50).iloc[-1] - ema(close, 50).iloc[-5]

    if price > ema50 > ema200 and slope50 > 0:
        return "bull"
    if price < ema50 < ema200 and slope50 < 0:
        return "bear"
    return "neutral"


# ─── Main Strategy ────────────────────────────────────────────────────────────

class ConfluenceStrategy:
    """
    Institutional Multi-Timeframe Confluence Strategy.

    Entry conditions (LONG):
      ✅ HTF trend = bull (EMA50 > EMA200, price above both)
      ✅ Price pulling back to EMA21 (structure support)
      ✅ RSI between 35-55 (reset, not overextended)
      ✅ MACD histogram turning positive (momentum inflection)
      ✅ ATR > ATR_MA (volatility alive)
      ✅ Volume > 1.2x average (real participation)
      ✅ ADX > 20 (trending market)
      ✅ Last swing low intact (structure not broken)
      ✅ Funding rate not extreme (< 0.1%)

    Entry conditions (SHORT): Mirror of above.

    SL: Below last swing low (LONG) / Above last swing high (SHORT) + 0.5 ATR buffer
    TP: 2.5R from entry (minimum 2.5:1 risk-reward)

    Regime adaptation:
      - TREND regime: Use above full confluence
      - RANGE regime: Use BB + RSI mean reversion (buy low/sell high of range)
    """

    def __init__(self, params: Optional[Dict] = None):
        p = params or {}
        # Core
        self.fast_ema       = p.get("fast_ema", 9)
        self.mid_ema        = p.get("mid_ema", 21)
        self.slow_ema       = p.get("slow_ema", 50)
        self.trend_ema      = p.get("trend_ema", 200)
        self.rsi_period     = p.get("rsi_period", 14)
        self.atr_period     = p.get("atr_period", 14)
        self.adx_threshold  = p.get("adx_threshold", 20.0)
        self.vol_factor     = p.get("vol_factor", 1.2)
        self.rsi_long_min   = p.get("rsi_long_min", 30)
        self.rsi_long_max   = p.get("rsi_long_max", 70)
        self.rsi_short_min  = p.get("rsi_short_min", 30)
        self.rsi_short_max  = p.get("rsi_short_max", 70)
        self.max_ema_distance_pct = p.get("max_ema_distance_pct", 0.03)
        self.funding_long_max = p.get("funding_long_max", 0.02)
        self.funding_short_min = p.get("funding_short_min", -0.02)
        # RR
        self.sl_atr_mult    = p.get("sl_atr_mult", 1.5)
        self.tp_rr          = p.get("tp_rr", 2.5)
        # Swing detection
        self.swing_lookback = p.get("swing_lookback", 5)
        # BB for ranging
        self.bb_period      = p.get("bb_period", 20)
        self.bb_std         = p.get("bb_std", 2.0)

    def generate_signal(self, df: pd.DataFrame, symbol: str = "", funding_rate: float = 0.0) -> Optional[Signal]:
        if len(df) < 210:
            return Signal(SignalType.NEUTRAL, confidence=0.0)

        close   = df["close"]
        price   = float(close.iloc[-1])
        bar     = df.iloc[-1]

        # ── Indicators ────────────────────────────────────────────────────────
        ema9    = ema(close, self.fast_ema)
        ema21   = ema(close, self.mid_ema)
        ema50   = ema(close, self.slow_ema)
        ema200  = ema(close, self.trend_ema)
        rsi_s   = rsi(close, self.rsi_period)
        atr_s   = atr(df, self.atr_period)
        atr_ma  = atr_s.rolling(30).mean()
        adx_s, plus_di, minus_di = adx(df, 14)
        macd_l, macd_sig, macd_hist = macd(close)
        avg_vol = df["volume"].rolling(20).mean()

        # Current bar values
        curr_ema9   = float(ema9.iloc[-1])
        curr_ema21  = float(ema21.iloc[-1])
        curr_ema50  = float(ema50.iloc[-1])
        curr_ema200 = float(ema200.iloc[-1])
        curr_rsi    = float(rsi_s.iloc[-1])
        curr_atr    = float(atr_s.iloc[-1])
        curr_atr_ma = float(atr_ma.iloc[-1]) if not pd.isna(atr_ma.iloc[-1]) else curr_atr
        curr_adx    = float(adx_s.iloc[-1]) if not pd.isna(adx_s.iloc[-1]) else 0.0
        curr_plus_di  = float(plus_di.iloc[-1]) if not pd.isna(plus_di.iloc[-1]) else 0.0
        curr_minus_di = float(minus_di.iloc[-1]) if not pd.isna(minus_di.iloc[-1]) else 0.0
        curr_vol    = float(df["volume"].iloc[-1])
        curr_avg_v  = float(avg_vol.iloc[-1]) if not pd.isna(avg_vol.iloc[-1]) else 0.0
        curr_macd_h = float(macd_hist.iloc[-1])
        prev_macd_h = float(macd_hist.iloc[-2])

        # ── Regime Detection ──────────────────────────────────────────────────
        regime   = detect_regime(df)
        htf      = higher_timeframe_trend(df)

        # ── Shared Filters ────────────────────────────────────────────────────
        vol_alive = curr_atr > curr_atr_ma * 0.8  # volatility not dead
        vol_ok    = curr_vol > curr_avg_v * self.vol_factor if curr_avg_v > 0 else False

        # Funding rate guard (extreme funding = crowded trade = fade it)
        funding_extreme_long  = funding_rate > self.funding_long_max
        funding_extreme_short = funding_rate < self.funding_short_min

        # ── Find last swing high/low for SL placement ─────────────────────────
        last_swing_low  = self._last_swing_low(df, self.swing_lookback)
        last_swing_high = self._last_swing_high(df, self.swing_lookback)
        # — find prior swing levels for liquidity-sweep detection
        swing_lows_bool = find_swing_lows(df, self.swing_lookback)
        swing_highs_bool = find_swing_highs(df, self.swing_lookback)
        low_idxs = list(np.where(swing_lows_bool.values)[0])
        high_idxs = list(np.where(swing_highs_bool.values)[0])
        prev_swing_low = None
        prev_swing_high = None
        if len(low_idxs) >= 2:
            prev_swing_low = float(df["low"].iloc[low_idxs[-2]])
        if len(high_idxs) >= 2:
            prev_swing_high = float(df["high"].iloc[high_idxs[-2]])
        # previous bar for sweep detection (require at least two bars)
        prev_bar = df.iloc[-2] if len(df) >= 2 else None

        # ── TREND REGIME ──────────────────────────────────────────────────────
        if regime == "trend":
            return self._trend_signal(
                df,
                price, htf, curr_ema9, curr_ema21, curr_ema50, curr_ema200,
                curr_rsi, curr_atr, curr_adx, curr_plus_di, curr_minus_di,
                curr_macd_h, prev_macd_h, vol_ok, vol_alive,
                last_swing_low, last_swing_high,
                funding_extreme_long, funding_extreme_short,
                prev_swing_low, prev_swing_high, prev_bar,
                symbol,
            )
        else:
            # RANGE REGIME: Bollinger mean reversion
            return self._range_signal(
                df, price, curr_rsi, curr_atr, vol_ok,
                last_swing_low, last_swing_high, symbol,
            )

    def _trend_signal(
        self,
        df,
        price, htf, ema9, ema21, ema50, ema200,
        curr_rsi, curr_atr, curr_adx, plus_di, minus_di,
        macd_h, prev_macd_h,
        vol_ok, vol_alive,
        last_swing_low, last_swing_high,
        funding_extreme_long, funding_extreme_short,
        prev_swing_low, prev_swing_high, prev_bar,
        symbol,
    ) -> Signal:

        # ── Liquidity Sweep + Reclaim + BOS (order-flow trigger)
        sweep_long = False
        reclaim_long = False
        bos_long = False
        sweep_short = False
        reclaim_short = False
        bos_short = False
        try:
            last_close = float(df["close"].iloc[-1])
            # check previous bar existed and prior swing levels are available
            if prev_bar is not None and prev_swing_low is not None and last_swing_high is not None:
                sweep_long = (float(prev_bar["low"]) < prev_swing_low) and (float(prev_bar["close"]) > prev_swing_low)
                reclaim_long = last_close > prev_swing_low
                bos_long = last_close > last_swing_high
            if prev_bar is not None and prev_swing_high is not None and last_swing_low is not None:
                sweep_short = (float(prev_bar["high"]) > prev_swing_high) and (float(prev_bar["close"]) < prev_swing_high)
                reclaim_short = last_close < prev_swing_high
                bos_short = last_close < last_swing_low
        except Exception:
            sweep_long = reclaim_long = bos_long = False
            sweep_short = reclaim_short = bos_short = False

        entry_trigger_long = bool(sweep_long and reclaim_long and bos_long)
        entry_trigger_short = bool(sweep_short and reclaim_short and bos_short)

        # ── LONG CONDITIONS ───────────────────────────────────────────────────
        cond_long = (
            htf != "bear"                               # Allow neutral if local trend confirms
            and price > ema50                           # Price above trend EMA
            and ema50 > ema200                          # Golden cross condition
            and price > ema21                           # Price above mid EMA (pullback ended)
            and abs(price - ema21) / ema21 < self.max_ema_distance_pct
            and self.rsi_long_min < curr_rsi < self.rsi_long_max  # RSI in valid zone
            and macd_h > prev_macd_h                   # MACD histogram rising (momentum)
            and curr_adx > self.adx_threshold           # Trend is strong
            and plus_di > minus_di                     # Bulls in control
            and vol_alive                               # Volatility alive
            and (not funding_extreme_long)             # Not crowded long
            and last_swing_low is not None             # Have reference for SL
        )

        # ── SHORT CONDITIONS ──────────────────────────────────────────────────
        cond_short = (
            htf != "bull"                               # Allow neutral if local trend confirms
            and price < ema50                           # Price below trend EMA
            and ema50 < ema200                          # Death cross condition
            and price < ema21                           # Price below mid EMA
            and abs(price - ema21) / ema21 < self.max_ema_distance_pct
            and self.rsi_short_min < curr_rsi < self.rsi_short_max
            and macd_h < prev_macd_h                   # MACD histogram falling
            and curr_adx > self.adx_threshold
            and minus_di > plus_di                     # Bears in control
            and vol_alive
            and (not funding_extreme_short)
            and last_swing_high is not None
        )

        if cond_long:
            sl  = last_swing_low - curr_atr * self.sl_atr_mult
            sl  = min(sl, price * 0.98)  # max 2% SL
            sl  = max(sl, price * 0.95)  # min 5% SL (prevent too tight)
            risk = price - sl
            tp  = price + risk * self.tp_rr

            confidence = self._calc_confidence_long(curr_rsi, curr_adx, plus_di, minus_di, macd_h, vol_ok)
            confidence = min(1.0, confidence + self._trigger_bonus(entry_trigger_long, sweep_long, reclaim_long, bos_long))
            logger.info("✅ LONG SIGNAL | price=%.4f | sl=%.4f | tp=%.4f | conf=%.2f | adx=%.1f | rsi=%.1f",
                       price, sl, tp, confidence, curr_adx, curr_rsi)
            return Signal(
                type       = SignalType.LONG,
                symbol     = symbol,
                price      = price,
                stop_loss  = round(sl, 4),
                take_profit= round(tp, 4),
                confidence = confidence,
                metadata   = {
                    "regime": "trend", "htf": htf,
                    "ema21": round(ema21, 4), "adx": round(curr_adx, 1),
                    "rsi": round(curr_rsi, 1), "macd_hist": round(macd_h, 6),
                    "last_swing_low": round(last_swing_low, 4),
                    "entry_trigger": entry_trigger_long,
                },
            )

        if cond_short:
            sl  = last_swing_high + curr_atr * self.sl_atr_mult
            sl  = max(sl, price * 1.02)
            sl  = min(sl, price * 1.05)
            risk = sl - price
            tp  = price - risk * self.tp_rr

            confidence = self._calc_confidence_short(curr_rsi, curr_adx, plus_di, minus_di, macd_h, vol_ok)
            confidence = min(1.0, confidence + self._trigger_bonus(entry_trigger_short, sweep_short, reclaim_short, bos_short))
            logger.info("✅ SHORT SIGNAL | price=%.4f | sl=%.4f | tp=%.4f | conf=%.2f | adx=%.1f | rsi=%.1f",
                       price, sl, tp, confidence, curr_adx, curr_rsi)
            return Signal(
                type       = SignalType.SHORT,
                symbol     = symbol,
                price      = price,
                stop_loss  = round(sl, 4),
                take_profit= round(tp, 4),
                confidence = confidence,
                metadata   = {
                    "regime": "trend", "htf": htf,
                    "ema21": round(ema21, 4), "adx": round(curr_adx, 1),
                    "rsi": round(curr_rsi, 1), "macd_hist": round(macd_h, 6),
                    "last_swing_high": round(last_swing_high, 4),
                    "entry_trigger": entry_trigger_short,
                },
            )

        return Signal(SignalType.NEUTRAL, confidence=0.0, metadata={"regime": "trend", "htf": htf, "rsi": round(curr_rsi, 1)})

    def _range_signal(
        self,
        df, price, curr_rsi, curr_atr, vol_ok,
        last_swing_low, last_swing_high, symbol,
    ) -> Signal:
        """Bollinger Band mean reversion for ranging markets."""
        close = df["close"]
        mid   = close.rolling(self.bb_period).mean()
        std   = close.rolling(self.bb_period).std()
        lower = (mid - self.bb_std * std).iloc[-1]
        upper = (mid + self.bb_std * std).iloc[-1]
        mid_v = mid.iloc[-1]

        if pd.isna(lower) or pd.isna(upper):
            return Signal(SignalType.NEUTRAL, confidence=0.0, metadata={"regime": "range"})

        # Long: price below lower BB, RSI oversold
        if price < lower and curr_rsi < 35 and last_swing_low is not None:
            sl   = last_swing_low - curr_atr * 0.5
            sl   = min(sl, price * 0.985)
            risk = price - sl
            tp   = mid_v  # TP at mid BB
            if tp > price + risk * 1.5:  # Ensure at least 1.5R
                confidence = min(1.0, (lower - price) / (curr_atr + 0.0001) * 0.3 + 0.55)
                logger.info("✅ RANGE LONG | price=%.4f | sl=%.4f | tp=%.4f | rsi=%.1f", price, sl, tp, curr_rsi)
                return Signal(
                    type=SignalType.LONG, symbol=symbol, price=price,
                    stop_loss=round(sl, 4), take_profit=round(tp, 4),
                    confidence=confidence,
                    metadata={"regime": "range", "bb_lower": round(lower, 4), "rsi": round(curr_rsi, 1)},
                )

        # Short: price above upper BB, RSI overbought
        if price > upper and curr_rsi > 65 and last_swing_high is not None:
            sl   = last_swing_high + curr_atr * 0.5
            sl   = max(sl, price * 1.015)
            risk = sl - price
            tp   = mid_v
            if tp < price - risk * 1.5:
                confidence = min(1.0, (price - upper) / (curr_atr + 0.0001) * 0.3 + 0.55)
                logger.info("✅ RANGE SHORT | price=%.4f | sl=%.4f | tp=%.4f | rsi=%.1f", price, sl, tp, curr_rsi)
                return Signal(
                    type=SignalType.SHORT, symbol=symbol, price=price,
                    stop_loss=round(sl, 4), take_profit=round(tp, 4),
                    confidence=confidence,
                    metadata={"regime": "range", "bb_upper": round(upper, 4), "rsi": round(curr_rsi, 1)},
                )

        return Signal(SignalType.NEUTRAL, confidence=0.0, metadata={"regime": "range", "rsi": round(curr_rsi, 1)})

    def _last_swing_low(self, df: pd.DataFrame, lookback: int) -> Optional[float]:
        """Find the most recent significant swing low in the last 50 bars."""
        window = df.tail(50)
        lows   = window["low"]
        for i in range(len(lows) - lookback - 1, lookback - 1, -1):
            if all(lows.iloc[i] <= lows.iloc[i - j] for j in range(1, lookback + 1)) and \
               all(lows.iloc[i] <= lows.iloc[i + j] for j in range(1, min(lookback + 1, len(lows) - i))):
                return float(lows.iloc[i])
        return float(lows.min())

    def _last_swing_high(self, df: pd.DataFrame, lookback: int) -> Optional[float]:
        """Find the most recent significant swing high in the last 50 bars."""
        window = df.tail(50)
        highs  = window["high"]
        for i in range(len(highs) - lookback - 1, lookback - 1, -1):
            if all(highs.iloc[i] >= highs.iloc[i - j] for j in range(1, lookback + 1)) and \
               all(highs.iloc[i] >= highs.iloc[i + j] for j in range(1, min(lookback + 1, len(highs) - i))):
                return float(highs.iloc[i])
        return float(highs.max())

    def _calc_confidence_long(self, rsi, adx, plus_di, minus_di, macd_h, vol_ok) -> float:
        scores = []
        # RSI in ideal zone 40-55
        if 40 <= rsi <= 55:
            scores.append(1.0)
        elif 35 <= rsi <= 60:
            scores.append(0.7)
        else:
            scores.append(0.4)
        # ADX strength
        scores.append(min(1.0, adx / 40))
        # DI separation
        di_sep = (plus_di - minus_di) / max(plus_di + minus_di, 0.01)
        scores.append(min(1.0, max(0.0, di_sep * 2)))
        # MACD strength
        scores.append(0.8 if macd_h > 0 else 0.3)
        # Volume
        scores.append(0.9 if vol_ok else 0.5)
        return round(sum(scores) / len(scores), 3)

    def _calc_confidence_short(self, rsi, adx, plus_di, minus_di, macd_h, vol_ok) -> float:
        scores = []
        if 45 <= rsi <= 60:
            scores.append(1.0)
        elif 40 <= rsi <= 65:
            scores.append(0.7)
        else:
            scores.append(0.4)
        scores.append(min(1.0, adx / 40))
        di_sep = (minus_di - plus_di) / max(plus_di + minus_di, 0.01)
        scores.append(min(1.0, max(0.0, di_sep * 2)))
        scores.append(0.8 if macd_h < 0 else 0.3)
        scores.append(0.9 if vol_ok else 0.5)
        return round(sum(scores) / len(scores), 3)

    def _trigger_bonus(self, entry_trigger: bool, sweep: bool, reclaim: bool, bos: bool) -> float:
        if entry_trigger:
            return 0.20
        bonus = 0.0
        if sweep:
            bonus += 0.05
        if reclaim:
            bonus += 0.05
        if bos:
            bonus += 0.10
        return bonus


# ─── Strategy Registry ────────────────────────────────────────────────────────

STRATEGY_MAP = {
    "confluence": ConfluenceStrategy,
}

def load_strategy(name: str = "confluence", params: Optional[Dict] = None) -> ConfluenceStrategy:
    cls = STRATEGY_MAP.get(name, ConfluenceStrategy)
    return cls(params)


__all__ = ["ConfluenceStrategy", "Signal", "SignalType", "load_strategy", "STRATEGY_MAP"]
