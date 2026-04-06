"""
risk.py — Risk Management Engine (PRODUCTION v6)

Features:
  - Per-trade risk sizing (fixed % of equity)
  - Max open trades limit
  - Max drawdown halt (15% default)
  - Daily loss limit halt (10% default)
  - Trailing stop: breakeven + profit-lock + ATR trailing
  - Symbol-specific leverage mapping
  - Circuit breaker (kill switch)
  - TradeRecord dataclass for full lifecycle tracking
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class RiskConfig:
    risk_per_trade:          float = 0.01   # 1% of equity per trade
    max_open_trades:         int   = 2
    max_drawdown_pct:        float = 0.15   # halt at 15% drawdown
    daily_loss_limit_pct:    float = 0.10   # halt at 10% daily loss
    leverage:                float = 10.0
    max_position_size_pct:   float = 0.30   # max 30% of equity in one trade

    # Trailing stop parameters
    breakeven_trigger_pct:   float = 0.003  # move SL to entry when +0.3%
    breakeven_buffer:        float = 0.0002 # SL slightly above entry (+0.02%)
    profit_lock_threshold_pct: float = 0.005 # start locking profit at +0.5%
    profit_lock_pct:         float = 0.003  # lock SL at current_price - 0.3%

    # Per-symbol leverage override
    leverage_by_symbol: Dict[str, float] = field(default_factory=lambda: {
        "BTC_USDT": 10.0,
        "ETH_USDT": 10.0,
        "SOL_USDT": 10.0,
        "BNB_USDT": 10.0,
        "XRP_USDT": 10.0,
        "BTCUSD":   10.0,
        "ETHUSD":   10.0,
    })


@dataclass
class TradeRecord:
    id:          str
    symbol:      str
    side:        str          # "long" | "short"
    entry_price: float
    size:        int          # lots
    stop_loss:   float
    take_profit: Optional[float]
    entry_time:  datetime
    order_id:    str          # exchange bracket entry order ID

    peak_price:  float = 0.0  # for trailing SL
    valley_price: float = 0.0 # for short trailing SL
    closed:      bool  = False
    exit_price:  Optional[float] = None
    exit_time:   Optional[datetime] = None
    reason:      Optional[str] = None

    def __post_init__(self):
        if self.peak_price == 0.0:
            self.peak_price = self.entry_price
        if self.valley_price == 0.0:
            self.valley_price = self.entry_price

    @property
    def unrealized_pnl(self) -> Optional[float]:
        return None

    @property
    def net_pnl(self) -> Optional[float]:
        if self.exit_price is None:
            return None
        mult = 1 if self.side == "long" else -1
        return mult * (self.exit_price - self.entry_price) / self.entry_price * self.size * self.entry_price


class RiskManager:
    def __init__(self, config: RiskConfig, initial_capital: float = 1000.0):
        self.config          = config
        self.initial_capital = initial_capital
        self.current_equity  = initial_capital
        self._peak_equity    = initial_capital
        self._daily_start_equity = initial_capital
        self._daily_reset_date   = datetime.now(timezone.utc).date()
        self._open_trades:  List[TradeRecord] = []
        self._closed_trades: List[TradeRecord] = []
        self._circuit_breaker = False

    # ── Equity Tracking ────────────────────────────────────────────────────

    def update_equity(self, new_equity: float):
        """Update equity from wallet balance."""
        if new_equity <= 0:
            return
        self.current_equity = new_equity
        if new_equity > self._peak_equity:
            self._peak_equity = new_equity

        # Reset daily P&L at midnight
        today = datetime.now(timezone.utc).date()
        if today != self._daily_reset_date:
            self._daily_start_equity = new_equity
            self._daily_reset_date   = today
            logger.info("Daily P&L reset. Start equity: %.2f", new_equity)

    # ── Risk Checks ────────────────────────────────────────────────────────

    def can_trade(self) -> bool:
        """True if all risk limits are within bounds."""
        if self._circuit_breaker:
            logger.warning("🔴 CIRCUIT BREAKER ACTIVE — trading halted")
            return False

        # Drawdown check
        if self._peak_equity > 0:
            drawdown = (self._peak_equity - self.current_equity) / self._peak_equity
            if drawdown >= self.config.max_drawdown_pct:
                logger.warning("🔴 MAX DRAWDOWN HALT: %.1f%% drawdown (limit %.1f%%)",
                               drawdown * 100, self.config.max_drawdown_pct * 100)
                self._circuit_breaker = True
                return False

        # Daily loss check
        daily_loss = (self._daily_start_equity - self.current_equity) / self._daily_start_equity
        if daily_loss >= self.config.daily_loss_limit_pct:
            logger.warning("🔴 DAILY LOSS HALT: %.1f%% daily loss (limit %.1f%%)",
                           daily_loss * 100, self.config.daily_loss_limit_pct * 100)
            return False

        return True

    def get_open_trade_count(self) -> int:
        return sum(1 for t in self._open_trades if not t.closed)

    def activate_kill_switch(self):
        """Emergency: disable all trading."""
        self._circuit_breaker = True
        logger.critical("🔴 KILL SWITCH ACTIVATED")

    def reset_circuit_breaker(self):
        """Manually reset after review."""
        self._circuit_breaker = False
        logger.info("✅ Circuit breaker reset")

    # ── Position Sizing ────────────────────────────────────────────────────

    def calculate_position_size(
        self, equity: float, entry_price: float, sl_distance: float
    ) -> float:
        """
        Calculate USD notional for position.

        Formula: risk_amount / sl_pct × leverage → USD notional
        Capped at max_position_size_pct of equity.
        """
        if sl_distance <= 0 or entry_price <= 0:
            return 0.0

        risk_amount  = equity * self.config.risk_per_trade
        sl_pct       = sl_distance / entry_price
        raw_notional = risk_amount / sl_pct * self.config.leverage

        max_notional = equity * self.config.max_position_size_pct * self.config.leverage
        notional     = min(raw_notional, max_notional)

        logger.debug(
            "Position size: equity=%.2f risk_amt=%.2f sl_pct=%.4f → notional=%.2f",
            equity, risk_amount, sl_pct, notional
        )
        return notional

    def get_leverage_for_symbol(self, symbol: str) -> float:
        return self.config.leverage_by_symbol.get(symbol, self.config.leverage)

    # ── Trade Lifecycle ────────────────────────────────────────────────────

    def register_trade(self, trade: TradeRecord):
        self._open_trades.append(trade)
        logger.info("Trade registered: %s %s @ %.4f", trade.symbol, trade.side, trade.entry_price)

    def close_trade(self, trade: TradeRecord, exit_price: float):
        if trade in self._open_trades:
            self._open_trades.remove(trade)
        self._closed_trades.append(trade)
        pnl = trade.net_pnl or 0.0
        self.current_equity += pnl
        if self.current_equity > self._peak_equity:
            self._peak_equity = self.current_equity
        logger.info("Trade closed: %s pnl=%.2f equity=%.2f", trade.symbol, pnl, self.current_equity)

    # ── Trailing Stop Logic ────────────────────────────────────────────────

    def update_trailing_stop(self, trade: TradeRecord, current_price: float) -> Optional[float]:
        """
        Update trailing SL. Returns new SL price if it moved, else None.
        Three modes (applied in order):
          1. Breakeven: move SL to entry + buffer when profit > breakeven_trigger_pct
          2. Profit-lock trailing: trail at profit_lock_pct from peak when profit > profit_lock_threshold_pct
        """
        cfg = self.config
        entry  = trade.entry_price
        old_sl = trade.stop_loss

        if trade.side == "long":
            trade.peak_price = max(trade.peak_price, current_price)
            profit_pct = (current_price - entry) / entry

            # Stage 1: breakeven
            if profit_pct >= cfg.breakeven_trigger_pct:
                be_sl = entry * (1 + cfg.breakeven_buffer)
                if be_sl > old_sl:
                    trade.stop_loss = be_sl
                    logger.debug("Breakeven SL: %.4f → %.4f", old_sl, be_sl)

            # Stage 2: profit-lock trailing
            if profit_pct >= cfg.profit_lock_threshold_pct:
                trail_sl = trade.peak_price * (1 - cfg.profit_lock_pct)
                if trail_sl > trade.stop_loss:
                    trade.stop_loss = trail_sl
                    logger.debug("Profit-lock SL: %.4f → %.4f", old_sl, trail_sl)

        else:  # short
            trade.valley_price = min(trade.valley_price, current_price)
            profit_pct = (entry - current_price) / entry

            # Stage 1: breakeven
            if profit_pct >= cfg.breakeven_trigger_pct:
                be_sl = entry * (1 - cfg.breakeven_buffer)
                if be_sl < old_sl:
                    trade.stop_loss = be_sl

            # Stage 2: profit-lock trailing
            if profit_pct >= cfg.profit_lock_threshold_pct:
                trail_sl = trade.valley_price * (1 + cfg.profit_lock_pct)
                if trail_sl < trade.stop_loss:
                    trade.stop_loss = trail_sl

        return trade.stop_loss if trade.stop_loss != old_sl else None

    # ── Analytics ──────────────────────────────────────────────────────────

    def get_stats(self) -> Dict:
        trades = self._closed_trades
        if not trades:
            return {"total_trades": 0}
        pnls  = [t.net_pnl for t in trades if t.net_pnl is not None]
        wins  = [p for p in pnls if p > 0]
        losses= [p for p in pnls if p <= 0]
        return {
            "total_trades": len(trades),
            "win_rate":     len(wins) / len(trades) * 100 if trades else 0,
            "total_pnl":    sum(pnls),
            "avg_win":      sum(wins) / len(wins) if wins else 0,
            "avg_loss":     sum(losses) / len(losses) if losses else 0,
            "profit_factor": abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else 999,
            "current_equity": self.current_equity,
            "peak_equity":    self._peak_equity,
            "drawdown_pct":   (self._peak_equity - self.current_equity) / self._peak_equity * 100,
        }


__all__ = ["RiskManager", "RiskConfig", "TradeRecord"]
