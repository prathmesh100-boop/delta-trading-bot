"""
regime.py — lightweight causal regime detector

Provides `RegimeDetector.detect_regime(df)` which returns one of:
  - "trend"
  - "range"
  - "volatile"

Uses only past/current bars (causal) and cheap indicators: ADX, ATR/price, Bollinger band width.
"""
from typing import Optional

import numpy as np
import pandas as pd


def _adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
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
    return dx.ewm(alpha=1 / period, adjust=False).mean()


class RegimeDetector:
    def __init__(self, adx_period: int = 14, bb_period: int = 20, atr_period: int = 14,
                 adx_trend_threshold: float = 20.0, adx_range_threshold: float = 15.0,
                 atr_vol_threshold: float = 0.01, bb_width_narrow: float = 0.03):
        self.adx_period = adx_period
        self.bb_period = bb_period
        self.atr_period = atr_period
        self.adx_trend_threshold = adx_trend_threshold
        self.adx_range_threshold = adx_range_threshold
        self.atr_vol_threshold = atr_vol_threshold
        self.bb_width_narrow = bb_width_narrow

    def detect_regime(self, df: pd.DataFrame) -> Optional[str]:
        """Return regime label using only past/current bars — causal.

        df must contain `high`, `low`, `close` columns. Returns one of
        'trend', 'range', or 'volatile'. If insufficient data, returns None.
        """
        if len(df) < max(self.adx_period, self.bb_period, self.atr_period) + 2:
            return None

        close = df["close"].astype(float)

        # ATR / price (volatility)
        high_low = df["high"] - df["low"]
        high_close = (df["high"] - df["close"].shift()).abs()
        low_close = (df["low"] - df["close"].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / self.atr_period, adjust=False).mean().iloc[-1]
        atr_pct = float(atr / close.iloc[-1]) if close.iloc[-1] > 0 else 0.0

        if atr_pct >= self.atr_vol_threshold:
            return "volatile"

        # Bollinger band width (use rolling std)
        std = close.rolling(self.bb_period).std().iloc[-1]
        middle = close.rolling(self.bb_period).mean().iloc[-1]
        if pd.isna(std) or pd.isna(middle) or middle == 0:
            bb_width = 0.0
        else:
            bb_width = float((2 * std) / middle)  # normalized width (upper-lower)/middle

        # ADX (trend strength)
        adx_series = _adx(df, period=self.adx_period)
        adx_now = float(adx_series.iloc[-1]) if len(adx_series) else 0.0

        # Decide
        if adx_now >= self.adx_trend_threshold and bb_width > self.bb_width_narrow:
            return "trend"

        if adx_now <= self.adx_range_threshold and bb_width <= self.bb_width_narrow:
            return "range"

        # fallback: if bb_width small -> range, else trend
        if bb_width <= self.bb_width_narrow:
            return "range"
        return "trend"


__all__ = ["RegimeDetector"]
