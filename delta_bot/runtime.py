from __future__ import annotations

from delta_bot.config import PortfolioRiskSettings, StorageConfig
from delta_bot.portfolio import PortfolioRiskManager
from delta_bot.storage import AuditStore


def build_audit_store(storage: StorageConfig | None = None) -> AuditStore:
    cfg = storage or StorageConfig()
    return AuditStore(cfg.database_path)


def build_portfolio_risk_manager(initial_capital: float, store: AuditStore) -> PortfolioRiskManager:
    return PortfolioRiskManager(PortfolioRiskSettings(), initial_capital=initial_capital, store=store)

