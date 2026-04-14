from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class StorageConfig:
    root: Path = Path.cwd() / ".bot_data"
    database_name: str = "system.db"

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

