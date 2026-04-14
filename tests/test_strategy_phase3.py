from strategy import ConfluenceStrategy, Signal, SignalType, StrategyCandidate, normalize_signal


def test_normalize_signal_adds_consistency_metadata():
    signal = Signal(
        SignalType.LONG,
        symbol="ETH_USDT",
        price=123.456789,
        stop_loss=120.123456,
        take_profit=130.987654,
        confidence=0.812345,
        metadata={"setup_type": "trend_pullback", "regime": "trend"},
    )

    normalized = normalize_signal(signal)

    assert normalized is not None
    assert normalized.confidence == 0.8123
    assert normalized.metadata["strategy_family"] == "confluence"
    assert "consistency_key" in normalized.metadata


def test_candidate_selector_prefers_regime_aligned_setup():
    strategy = ConfluenceStrategy()
    candidates = [
        StrategyCandidate(
            signal_type=SignalType.LONG,
            setup_type="trend_pullback",
            regime="trend",
            confidence=0.74,
            stop_loss=99.0,
            take_profit=103.0,
            score=0.82,
        ),
        StrategyCandidate(
            signal_type=SignalType.LONG,
            setup_type="range_mean_rev",
            regime="range",
            confidence=0.79,
            stop_loss=98.0,
            take_profit=101.0,
            score=0.84,
        ),
    ]

    selected = strategy._select_candidate(candidates, regime="trend")

    assert selected is not None
    assert selected.setup_type == "trend_pullback"
