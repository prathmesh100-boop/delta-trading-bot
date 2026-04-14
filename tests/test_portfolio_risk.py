from pathlib import Path

from delta_bot.config import PortfolioRiskSettings
from delta_bot.portfolio import PortfolioRiskManager
from delta_bot.storage import AuditStore


def test_portfolio_rejects_when_symbol_exposure_too_large(tmp_path: Path):
    store = AuditStore(tmp_path / "system.db")
    settings = PortfolioRiskSettings(max_symbol_exposure_pct=0.20)
    portfolio = PortfolioRiskManager(settings, initial_capital=1000.0, store=store)

    allowed, reason = portfolio.can_open_trade("BTC_USDT", proposed_notional_usd=250.0, proposed_risk_usd=10.0)

    assert allowed is False
    assert reason == "portfolio_symbol_exposure_limit"


def test_portfolio_tracks_open_positions_and_close_pnl(tmp_path: Path):
    store = AuditStore(tmp_path / "system.db")
    portfolio = PortfolioRiskManager(PortfolioRiskSettings(), initial_capital=1000.0, store=store)

    portfolio.register_trade("t1", "ETH_USDT", "long", notional_usd=100.0, risk_usd=8.0)
    snapshot_open = portfolio.snapshot()
    portfolio.close_trade("t1", pnl=25.0)
    snapshot_closed = portfolio.snapshot()

    assert snapshot_open["open_positions"] == 1
    assert snapshot_open["open_notional_usd"] == 100.0
    assert snapshot_closed["open_positions"] == 0
    assert snapshot_closed["current_equity"] == 1025.0
