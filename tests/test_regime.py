import pandas as pd
from regime import RegimeDetector


def _make_df(close, high=None, low=None, volume=None):
    if high is None:
        high = [c * 1.002 for c in close]
    if low is None:
        low = [c * 0.998 for c in close]
    if volume is None:
        volume = [100 for _ in close]
    return pd.DataFrame({"high": high, "low": low, "close": close, "volume": volume})


def test_detect_trend():
    # steady upward trend should be classified as 'trend'
    close = [100 + i * 0.5 for i in range(120)]
    df = _make_df(close)
    r = RegimeDetector().detect_regime(df)
    assert r == "trend"


def test_detect_range():
    # small oscillation around a fixed price -> 'range'
    close = [100 + ((i % 6) - 3) * 0.05 for i in range(120)]
    df = _make_df(close)
    r = RegimeDetector().detect_regime(df)
    assert r == "range"


def test_detect_volatile():
    # introduce large high/low swings to trigger high ATR -> 'volatile'
    close = []
    highs = []
    lows = []
    for i in range(120):
        base = 100 + ((i % 5) - 2) * 0.2
        if i % 25 == 0:
            # big spike/bar range
            high = base + 8.0
            low = base - 8.0
            close.append(base + 4.0)
        else:
            high = base + 0.3
            low = base - 0.3
            close.append(base)
        highs.append(high)
        lows.append(low)

    df = _make_df(close, high=highs, low=lows)
    detector = RegimeDetector(atr_vol_threshold=0.01)
    r = detector.detect_regime(df)
    assert r == "volatile"
