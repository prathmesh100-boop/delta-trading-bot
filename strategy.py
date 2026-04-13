"""
strategy.py - Confluence strategy with breakout, pullback, and range entries.
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class SignalType(str, Enum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"
    HOLD = "neutral"


@dataclass
class Signal:
    type: SignalType
    symbol: Optional[str] = None
    price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    confidence: float = 1.0
    metadata: Dict = field(default_factory=dict)


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


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hi, lo, cl = df["high"], df["low"], df["close"]
    tr = pd.concat(
        [
            hi - lo,
            (hi - cl.shift()).abs(),
            (lo - cl.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal_p: int = 9):
    fast_ema = series.ewm(span=fast, adjust=False).mean()
    slow_ema = series.ewm(span=slow, adjust=False).mean()
    macd_line = fast_ema - slow_ema
    sig_line = macd_line.ewm(span=signal_p, adjust=False).mean()
    histogram = macd_line - sig_line
    return macd_line, sig_line, histogram


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    tr_s = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()],
        axis=1,
    ).max(axis=1)
    atr_s = tr_s.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_s.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_s.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False).mean(), plus_di, minus_di


def find_swing_highs(df: pd.DataFrame, lookback: int = 5) -> pd.Series:
    highs = df["high"]
    return highs == highs.rolling(lookback * 2 + 1, center=True).max()


def find_swing_lows(df: pd.DataFrame, lookback: int = 5) -> pd.Series:
    lows = df["low"]
    return lows == lows.rolling(lookback * 2 + 1, center=True).min()


def detect_regime(df: pd.DataFrame) -> str:
    if len(df) < 30:
        return "trend"
    adx_series, _, _ = adx(df, 14)
    adx_val = adx_series.iloc[-1]
    if pd.isna(adx_val):
        return "trend"
    return "trend" if adx_val >= 18 else "range"


def higher_timeframe_trend(df: pd.DataFrame) -> str:
    if len(df) < 200:
        return "neutral"
    close = df["close"]
    ema50 = ema(close, 50).iloc[-1]
    ema200 = ema(close, 200).iloc[-1]
    price = close.iloc[-1]
    slope50 = ema(close, 50).iloc[-1] - ema(close, 50).iloc[-5]

    if price > ema50 > ema200 and slope50 > 0:
        return "bull"
    if price < ema50 < ema200 and slope50 < 0:
        return "bear"
    return "neutral"


class ConfluenceStrategy:
    """
    A balanced entry model:
    - breakout continuation for strong expansions
    - pullback/reclaim entries for trend continuation without chasing
    - Bollinger range fades with reversal confirmation
    """

    def __init__(self, params: Optional[Dict] = None):
        p = params or {}
        self.fast_ema = p.get("fast_ema", 9)
        self.mid_ema = p.get("mid_ema", 21)
        self.slow_ema = p.get("slow_ema", 50)
        self.trend_ema = p.get("trend_ema", 200)
        self.rsi_period = p.get("rsi_period", 14)
        self.atr_period = p.get("atr_period", 14)
        self.adx_threshold = p.get("adx_threshold", 18.0)
        self.vol_factor = p.get("vol_factor", 1.05)
        self.rsi_long_min = p.get("rsi_long_min", 40)
        self.rsi_long_max = p.get("rsi_long_max", 60)
        self.rsi_short_min = p.get("rsi_short_min", 40)
        self.rsi_short_max = p.get("rsi_short_max", 60)
        self.max_ema_distance_pct = p.get("max_ema_distance_pct", 0.025)
        self.funding_long_max = p.get("funding_long_max", 0.02)
        self.funding_short_min = p.get("funding_short_min", -0.02)
        self.sl_atr_mult = p.get("sl_atr_mult", 1.2)
        self.tp_rr = p.get("tp_rr", 2.0)
        self.swing_lookback = p.get("swing_lookback", 4)
        self.bb_period = p.get("bb_period", 20)
        self.bb_std = p.get("bb_std", 2.0)
        self.breakout_lookback = p.get("breakout_lookback", 20)
        self.breakout_buffer_atr = p.get("breakout_buffer_atr", 0.12)

    def _neutral_signal(self, regime: str, **metadata: Any) -> Signal:
        payload = {"regime": regime}
        payload.update(metadata)
        return Signal(SignalType.NEUTRAL, confidence=0.0, metadata=payload)

    def generate_signal(self, df: pd.DataFrame, symbol: str = "", funding_rate: float = 0.0) -> Optional[Signal]:
        if len(df) < 210:
            return Signal(SignalType.NEUTRAL, confidence=0.0)

        close = df["close"]
        price = float(close.iloc[-1])

        ema9 = ema(close, self.fast_ema)
        ema21 = ema(close, self.mid_ema)
        ema50 = ema(close, self.slow_ema)
        ema200 = ema(close, self.trend_ema)
        rsi_s = rsi(close, self.rsi_period)
        atr_s = atr(df, self.atr_period)
        atr_ma = atr_s.rolling(30).mean()
        adx_s, plus_di_s, minus_di_s = adx(df, 14)
        _, _, macd_hist = macd(close)
        avg_vol = df["volume"].rolling(20).mean()

        curr = {
            "price": price,
            "open": float(df["open"].iloc[-1]),
            "prev_close": float(close.iloc[-2]),
            "ema9": float(ema9.iloc[-1]),
            "ema21": float(ema21.iloc[-1]),
            "ema50": float(ema50.iloc[-1]),
            "ema200": float(ema200.iloc[-1]),
            "rsi": float(rsi_s.iloc[-1]),
            "atr": float(atr_s.iloc[-1]),
            "atr_ma": float(atr_ma.iloc[-1]) if not pd.isna(atr_ma.iloc[-1]) else float(atr_s.iloc[-1]),
            "adx": float(adx_s.iloc[-1]) if not pd.isna(adx_s.iloc[-1]) else 0.0,
            "plus_di": float(plus_di_s.iloc[-1]) if not pd.isna(plus_di_s.iloc[-1]) else 0.0,
            "minus_di": float(minus_di_s.iloc[-1]) if not pd.isna(minus_di_s.iloc[-1]) else 0.0,
            "macd_h": float(macd_hist.iloc[-1]),
            "prev_macd_h": float(macd_hist.iloc[-2]),
            "volume": float(df["volume"].iloc[-1]),
            "avg_volume": float(avg_vol.iloc[-1]) if not pd.isna(avg_vol.iloc[-1]) else 0.0,
        }
        curr["vol_ok"] = curr["avg_volume"] > 0 and curr["volume"] >= curr["avg_volume"] * self.vol_factor
        curr["vol_alive"] = curr["atr"] >= curr["atr_ma"] * 0.85

        regime = detect_regime(df)
        htf = higher_timeframe_trend(df)
        last_swing_low = self._last_swing_low(df, self.swing_lookback)
        last_swing_high = self._last_swing_high(df, self.swing_lookback)

        if regime == "trend":
            return self._trend_signal(df, symbol, htf, curr, last_swing_low, last_swing_high, funding_rate)
        return self._range_signal(df, symbol, curr, last_swing_low, last_swing_high)

    def _trend_signal(self, df: pd.DataFrame, symbol: str, htf: str, curr: Dict[str, float], last_swing_low: Optional[float], last_swing_high: Optional[float], funding_rate: float) -> Signal:
        price = curr["price"]
        ema9 = curr["ema9"]
        ema21 = curr["ema21"]
        ema50 = curr["ema50"]
        ema200 = curr["ema200"]
        rsi_val = curr["rsi"]
        atr_val = curr["atr"]
        adx_val = curr["adx"]
        plus_di = curr["plus_di"]
        minus_di = curr["minus_di"]
        macd_h = curr["macd_h"]
        prev_macd_h = curr["prev_macd_h"]
        vol_ok = curr["vol_ok"]
        vol_alive = curr["vol_alive"]

        trend_bull = price > ema50 > ema200 and ema9 >= ema21 >= ema50
        trend_bear = price < ema50 < ema200 and ema9 <= ema21 <= ema50
        di_bull = plus_di > minus_di
        di_bear = minus_di > plus_di
        adx_ok = adx_val >= self.adx_threshold

        recent_high = float(df["high"].iloc[-self.breakout_lookback:-1].max())
        recent_low = float(df["low"].iloc[-self.breakout_lookback:-1].min())
        breakout_long = (
            trend_bull and htf != "bear" and adx_ok and di_bull and vol_alive and vol_ok and funding_rate <= self.funding_long_max
            and price > recent_high + atr_val * self.breakout_buffer_atr and rsi_val <= 67 and macd_h >= prev_macd_h and last_swing_low is not None
        )
        breakout_short = (
            trend_bear and htf != "bull" and adx_ok and di_bear and vol_alive and vol_ok and funding_rate >= self.funding_short_min
            and price < recent_low - atr_val * self.breakout_buffer_atr and rsi_val >= 33 and macd_h <= prev_macd_h and last_swing_high is not None
        )

        pullback_long = (
            trend_bull and htf != "bear" and di_bull and vol_alive and funding_rate <= self.funding_long_max
            and abs(price - ema21) / max(ema21, 1e-9) <= self.max_ema_distance_pct and self.rsi_long_min <= rsi_val <= self.rsi_long_max
            and price >= curr["open"] and price >= curr["prev_close"] and macd_h >= prev_macd_h and last_swing_low is not None
        )
        pullback_short = (
            trend_bear and htf != "bull" and di_bear and vol_alive and funding_rate >= self.funding_short_min
            and abs(price - ema21) / max(ema21, 1e-9) <= self.max_ema_distance_pct and self.rsi_short_min <= (100 - rsi_val) <= self.rsi_short_max
            and price <= curr["open"] and price <= curr["prev_close"] and macd_h <= prev_macd_h and last_swing_high is not None
        )

        if breakout_long:
            return self._build_signal(SignalType.LONG, symbol, price, last_swing_low, atr_val, self._score_trend_confidence(rsi_val, adx_val, plus_di, minus_di, vol_ok, breakout=True), {"regime": "trend", "setup": "breakout_long", "htf": htf, "rsi": round(rsi_val, 1), "adx": round(adx_val, 1), "breakout_level": round(recent_high, 4)})
        if breakout_short:
            return self._build_signal(SignalType.SHORT, symbol, price, last_swing_high, atr_val, self._score_trend_confidence(rsi_val, adx_val, minus_di, plus_di, vol_ok, breakout=True), {"regime": "trend", "setup": "breakout_short", "htf": htf, "rsi": round(rsi_val, 1), "adx": round(adx_val, 1), "breakout_level": round(recent_low, 4)})
        if pullback_long:
            return self._build_signal(SignalType.LONG, symbol, price, last_swing_low, atr_val, self._score_trend_confidence(rsi_val, adx_val, plus_di, minus_di, vol_ok, breakout=False), {"regime": "trend", "setup": "pullback_long", "htf": htf, "rsi": round(rsi_val, 1), "adx": round(adx_val, 1), "ema21": round(ema21, 4)})
        if pullback_short:
            return self._build_signal(SignalType.SHORT, symbol, price, last_swing_high, atr_val, self._score_trend_confidence(rsi_val, adx_val, minus_di, plus_di, vol_ok, breakout=False), {"regime": "trend", "setup": "pullback_short", "htf": htf, "rsi": round(rsi_val, 1), "adx": round(adx_val, 1), "ema21": round(ema21, 4)})

        blockers = []
        if not trend_bull and not trend_bear:
            blockers.append("ema_alignment_weak")
        if not adx_ok:
            blockers.append("adx_too_low")
        if not vol_alive:
            blockers.append("volatility_dead")
        if htf == "bull" and not trend_bull:
            blockers.append("pullback_not_ready_long")
        if htf == "bear" and not trend_bear:
            blockers.append("pullback_not_ready_short")
        if not blockers:
            blockers.append("entry_not_confirmed")
        return self._neutral_signal("trend", htf=htf, rsi=round(rsi_val, 1), adx=round(adx_val, 1), blockers=blockers)

    def _range_signal(self, df: pd.DataFrame, symbol: str, curr: Dict[str, float], last_swing_low: Optional[float], last_swing_high: Optional[float]) -> Signal:
        close = df["close"]
        mid = close.rolling(self.bb_period).mean()
        std = close.rolling(self.bb_period).std()
        lower = (mid - self.bb_std * std).iloc[-1]
        upper = (mid + self.bb_std * std).iloc[-1]
        mid_v = mid.iloc[-1]
        price = curr["price"]
        atr_val = curr["atr"]
        rsi_val = curr["rsi"]

        if pd.isna(lower) or pd.isna(upper):
            return self._neutral_signal("range")

        long_reversal = price <= lower * 1.01 and rsi_val <= 43 and price >= curr["open"] and price >= curr["prev_close"] and last_swing_low is not None
        short_reversal = price >= upper * 0.99 and rsi_val >= 57 and price <= curr["open"] and price <= curr["prev_close"] and last_swing_high is not None

        if long_reversal:
            sl = min(last_swing_low - atr_val * 0.45, price * 0.9925)
            risk = price - sl
            tp = min(float(upper), price + risk * max(1.35, self.tp_rr))
            if risk > 0 and tp > price:
                confidence = min(0.78, 0.56 + max(0.0, (lower - price) / max(atr_val, 1e-9)) * 0.08 + (0.04 if curr["vol_ok"] else 0.0))
                logger.info("RANGE LONG | price=%.4f | sl=%.4f | tp=%.4f | rsi=%.1f", price, sl, tp, rsi_val)
                return Signal(SignalType.LONG, symbol, price, round(sl, 4), round(tp, 4), round(confidence, 3), {"regime": "range", "setup": "range_long", "bb_lower": round(lower, 4), "bb_mid": round(mid_v, 4), "rsi": round(rsi_val, 1)})

        if short_reversal:
            sl = max(last_swing_high + atr_val * 0.45, price * 1.0075)
            risk = sl - price
            tp = max(float(lower), price - risk * max(1.35, self.tp_rr))
            if risk > 0 and tp < price:
                confidence = min(0.78, 0.56 + max(0.0, (price - upper) / max(atr_val, 1e-9)) * 0.08 + (0.04 if curr["vol_ok"] else 0.0))
                logger.info("RANGE SHORT | price=%.4f | sl=%.4f | tp=%.4f | rsi=%.1f", price, sl, tp, rsi_val)
                return Signal(SignalType.SHORT, symbol, price, round(sl, 4), round(tp, 4), round(confidence, 3), {"regime": "range", "setup": "range_short", "bb_upper": round(upper, 4), "bb_mid": round(mid_v, 4), "rsi": round(rsi_val, 1)})

        blockers = []
        if not (price <= lower * 1.01 or price >= upper * 0.99):
            blockers.append("not_at_band_edge")
        if 43 < rsi_val < 57:
            blockers.append("rsi_mid_range")
        if price < curr["open"] and price < curr["prev_close"] and price <= lower * 1.01:
            blockers.append("no_reversal_candle")
        if price > curr["open"] and price > curr["prev_close"] and price >= upper * 0.99:
            blockers.append("no_reversal_candle")
        if not blockers:
            blockers.append("range_setup_not_ready")
        return self._neutral_signal("range", rsi=round(rsi_val, 1), bb_mid=round(mid_v, 4), blockers=blockers)

    def _build_signal(self, side: SignalType, symbol: str, price: float, stop_anchor: float, atr_value: float, confidence: float, metadata: Dict[str, Any]) -> Signal:
        if side == SignalType.LONG:
            sl = min(stop_anchor - atr_value * self.sl_atr_mult, price * 0.985)
            risk = price - sl
            tp = price + risk * self.tp_rr
        else:
            sl = max(stop_anchor + atr_value * self.sl_atr_mult, price * 1.015)
            risk = sl - price
            tp = price - risk * self.tp_rr
        logger.info("%s SIGNAL | setup=%s | price=%.4f | sl=%.4f | tp=%.4f | conf=%.2f", side.value.upper(), metadata.get("setup"), price, sl, tp, confidence)
        return Signal(side, symbol, price, round(sl, 4), round(tp, 4), round(confidence, 3), metadata)

    def _score_trend_confidence(self, rsi_val: float, adx_val: float, lead_di: float, lag_di: float, vol_ok: bool, breakout: bool) -> float:
        di_edge = max(0.0, (lead_di - lag_di) / max(lead_di + lag_di, 1e-9))
        rsi_score = 1.0 - min(1.0, abs(rsi_val - 52.0) / 18.0)
        adx_score = min(1.0, adx_val / 35.0)
        base = 0.48 + (0.10 if breakout else 0.05)
        confidence = base + 0.14 * rsi_score + 0.12 * adx_score + 0.10 * di_edge + (0.06 if vol_ok else 0.0)
        return min(0.92, confidence)

    def _last_swing_low(self, df: pd.DataFrame, lookback: int) -> Optional[float]:
        window = df.tail(50)
        lows = window["low"]
        for i in range(len(lows) - lookback - 1, lookback - 1, -1):
            if all(lows.iloc[i] <= lows.iloc[i - j] for j in range(1, lookback + 1)) and all(lows.iloc[i] <= lows.iloc[i + j] for j in range(1, min(lookback + 1, len(lows) - i))):
                return float(lows.iloc[i])
        return float(lows.min())

    def _last_swing_high(self, df: pd.DataFrame, lookback: int) -> Optional[float]:
        window = df.tail(50)
        highs = window["high"]
        for i in range(len(highs) - lookback - 1, lookback - 1, -1):
            if all(highs.iloc[i] >= highs.iloc[i - j] for j in range(1, lookback + 1)) and all(highs.iloc[i] >= highs.iloc[i + j] for j in range(1, min(lookback + 1, len(highs) - i))):
                return float(highs.iloc[i])
        return float(highs.max())


STRATEGY_MAP = {
    "confluence": ConfluenceStrategy,
}


def load_strategy(name: str = "confluence", params: Optional[Dict] = None) -> ConfluenceStrategy:
    cls = STRATEGY_MAP.get(name, ConfluenceStrategy)
    return cls(params)


__all__ = ["ConfluenceStrategy", "Signal", "SignalType", "load_strategy", "STRATEGY_MAP"]
