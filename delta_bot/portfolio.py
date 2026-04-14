from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from delta_bot.config import PortfolioRiskSettings
from delta_bot.storage import AuditStore


@dataclass
class PortfolioPosition:
    trade_id: str
    symbol: str
    side: str
    notional_usd: float
    risk_usd: float
    opened_at: str


@dataclass
class PortfolioState:
    initial_capital: float
    current_equity: float
    current_capital: float
    peak_equity: float
    daily_start_equity: float
    daily_reset_date: str
    kill_switch: bool = False
    positions: Dict[str, PortfolioPosition] = field(default_factory=dict)


class PortfolioRiskManager:
    def __init__(self, settings: PortfolioRiskSettings, initial_capital: float, store: AuditStore):
        today = datetime.now(timezone.utc).date().isoformat()
        self.settings = settings
        self.store = store
        self.state = PortfolioState(
            initial_capital=initial_capital,
            current_equity=initial_capital,
            current_capital=initial_capital,
            peak_equity=initial_capital,
            daily_start_equity=initial_capital,
            daily_reset_date=today,
        )
        self._load_state()
        self._persist_state()

    def _load_state(self) -> None:
        states = self.store.list_runtime_states("portfolio")
        snapshot = next((item["value"] for item in states if item["state_key"] == "state"), None)
        if not snapshot:
            return
        positions = {
            key: PortfolioPosition(**value)
            for key, value in snapshot.get("positions", {}).items()
        }
        self.state = PortfolioState(
            initial_capital=snapshot.get("initial_capital", self.state.initial_capital),
            current_equity=snapshot.get("current_equity", self.state.current_equity),
            current_capital=snapshot.get("current_capital", self.state.current_capital),
            peak_equity=snapshot.get("peak_equity", self.state.peak_equity),
            daily_start_equity=snapshot.get("daily_start_equity", self.state.daily_start_equity),
            daily_reset_date=snapshot.get("daily_reset_date", self.state.daily_reset_date),
            kill_switch=bool(snapshot.get("kill_switch", False)),
            positions=positions,
        )

    def _refresh_daily_if_needed(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        if today != self.state.daily_reset_date:
            self.state.daily_reset_date = today
            self.state.daily_start_equity = self.state.current_capital
            self.state.kill_switch = False
            self._persist_state()

    def _drawdown_pct(self) -> float:
        if self.state.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.state.peak_equity - self.state.current_capital) / self.state.peak_equity)

    def _daily_loss_pct(self) -> float:
        if self.state.daily_start_equity <= 0:
            return 0.0
        return max(0.0, (self.state.daily_start_equity - self.state.current_capital) / self.state.daily_start_equity)

    def _open_notional(self) -> float:
        return sum(position.notional_usd for position in self.state.positions.values())

    def _open_risk(self) -> float:
        return sum(position.risk_usd for position in self.state.positions.values())

    def sync_equity(self, equity: float) -> None:
        if equity <= 0:
            return
        self._refresh_daily_if_needed()
        self.state.current_equity = equity
        self.state.current_capital = equity
        if equity > self.state.peak_equity:
            self.state.peak_equity = equity
        if self._drawdown_pct() < self.settings.max_drawdown_pct and self._daily_loss_pct() < self.settings.daily_loss_limit_pct:
            self.state.kill_switch = False
        self._persist_state()

    def can_open_trade(self, symbol: str, proposed_notional_usd: float, proposed_risk_usd: float) -> Tuple[bool, str]:
        self._refresh_daily_if_needed()
        if self.state.kill_switch:
            return False, "portfolio_kill_switch_active"
        if self._drawdown_pct() >= self.settings.max_drawdown_pct:
            self.state.kill_switch = True
            self._persist_state()
            return False, "portfolio_drawdown_limit"
        if self._daily_loss_pct() >= self.settings.daily_loss_limit_pct:
            return False, "portfolio_daily_loss_limit"
        if len(self.state.positions) >= self.settings.max_open_positions:
            return False, "portfolio_max_open_positions"

        total_capital = max(self.state.current_capital, 1.0)
        symbol_notional = sum(
            position.notional_usd for position in self.state.positions.values() if position.symbol == symbol
        )
        if (self._open_notional() + proposed_notional_usd) > total_capital * self.settings.max_total_exposure_pct:
            return False, "portfolio_total_exposure_limit"
        if (symbol_notional + proposed_notional_usd) > total_capital * self.settings.max_symbol_exposure_pct:
            return False, "portfolio_symbol_exposure_limit"
        if (self._open_risk() + proposed_risk_usd) > total_capital * self.settings.max_portfolio_risk_pct:
            return False, "portfolio_open_risk_limit"
        return True, "ok"

    def register_trade(self, trade_id: str, symbol: str, side: str, notional_usd: float, risk_usd: float) -> None:
        self.state.positions[trade_id] = PortfolioPosition(
            trade_id=trade_id,
            symbol=symbol,
            side=side,
            notional_usd=notional_usd,
            risk_usd=risk_usd,
            opened_at=datetime.now(timezone.utc).isoformat(),
        )
        self._persist_state()

    def close_trade(self, trade_id: str, pnl: float) -> None:
        self.state.positions.pop(trade_id, None)
        self.state.current_equity = max(0.0, self.state.current_equity + pnl)
        self.state.current_capital = self.state.current_equity
        if self.state.current_equity > self.state.peak_equity:
            self.state.peak_equity = self.state.current_equity
        if self._drawdown_pct() >= self.settings.max_drawdown_pct:
            self.state.kill_switch = True
        self._persist_state()

    def activate_kill_switch(self, reason: str) -> None:
        self.state.kill_switch = True
        self.store.record_event("risk", "portfolio_kill_switch", {"reason": reason}, severity="critical")
        self._persist_state()

    def snapshot(self) -> Dict[str, Any]:
        self._refresh_daily_if_needed()
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "initial_capital": self.state.initial_capital,
            "current_equity": self.state.current_equity,
            "current_capital": self.state.current_capital,
            "peak_equity": self.state.peak_equity,
            "daily_start_equity": self.state.daily_start_equity,
            "drawdown_pct": round(self._drawdown_pct(), 6),
            "daily_loss_pct": round(self._daily_loss_pct(), 6),
            "open_positions": len(self.state.positions),
            "open_notional_usd": round(self._open_notional(), 6),
            "open_risk_usd": round(self._open_risk(), 6),
            "kill_switch": self.state.kill_switch,
            "positions": {key: asdict(value) for key, value in self.state.positions.items()},
        }

    def _persist_state(self) -> None:
        snapshot = self.snapshot()
        self.store.set_runtime_state("portfolio", "state", snapshot)
        self.store.record_portfolio_snapshot(snapshot)

