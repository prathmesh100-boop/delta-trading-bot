from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class StorageConfig:
    root: Path = Path.cwd() / ".bot_data"
    database_name: str = "system.db"
    sqlite_busy_timeout_ms: int = 15_000

    @property
    def database_path(self) -> Path:
        return self.root / self.database_name


@dataclass
class PortfolioRiskSettings:
    max_total_exposure_pct: float = 1.0
    max_symbol_exposure_pct: float = 0.35
    max_open_positions: int = 3
    max_portfolio_risk_pct: float = 0.03
    max_drawdown_pct: float = 0.15
    daily_loss_limit_pct: float = 0.08


@dataclass
class MonitoringConfig:
    stale_after_seconds: float = 180.0
    slow_loop_threshold_ms: float = 2_500.0
    persist_interval_seconds: float = 15.0
