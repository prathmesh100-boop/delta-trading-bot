from __future__ import annotations

import logging
import uuid

from delta_bot.config import PortfolioRiskSettings, StorageConfig
from delta_bot.portfolio import PortfolioRiskManager
from delta_bot.storage import AuditStore

logger = logging.getLogger(__name__)


def build_audit_store(storage: StorageConfig | None = None) -> AuditStore:
    cfg = storage or StorageConfig()
    try:
        return AuditStore(cfg.database_path)
    except Exception as exc:
        fallback_path = cfg.root / f"system-recovery-{uuid.uuid4().hex[:8]}.db"
        logger.warning("Primary audit store unavailable at %s, falling back to %s: %s", cfg.database_path, fallback_path, exc)
        return AuditStore(fallback_path)


def build_portfolio_risk_manager(initial_capital: float, store: AuditStore) -> PortfolioRiskManager:
    return PortfolioRiskManager(PortfolioRiskSettings(), initial_capital=initial_capital, store=store)
