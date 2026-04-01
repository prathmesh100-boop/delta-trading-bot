import numpy as np
import pandas as pd

from strategy import BaseStrategy


def make_price_series(n=200, seed=1):
    rng = np.random.default_rng(seed)
    price = 100 + np.cumsum(rng.normal(0, 0.5, size=n))
    return pd.Series(price)


def test_ema_iterative_matches_vectorised():
    s = make_price_series(500)
    # vectorised
    v = BaseStrategy.ema(s, 10)

    # iterative EMA (causal)
    alpha = 2 / (10 + 1)
    it = [s.iloc[0]]
    for i in range(1, len(s)):
        it.append(alpha * s.iloc[i] + (1 - alpha) * it[-1])
    it = pd.Series(it, index=s.index)

    # compare last 400 values (skip first transient)
    diff = (v - it).abs().dropna()
    assert diff.max() < 1e-8


def test_rsi_causal_properties():
    s = make_price_series(300)
    r = BaseStrategy.rsi(s, period=14)

    # RSI should be between 0 and 100 and NaN only at start
    assert r.dropna().between(0, 100).all()
    assert r.isna().sum() < 20
