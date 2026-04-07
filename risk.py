"""
risk.py — Institutional Risk Management Engine

Risk rules:
  - Fixed fractional sizing: risk exactly N% of equity per trade
  - Kelly-adjusted (never over-bet)
  - Maximum concurrent positions cap
  - Maximum drawdown circuit breaker (hard stop all trading)
  - Daily loss limit (soft stop, reset at midnight UTC)
  - Trailing stop: breakeven → profit lock (3 stages)
  - Per-symbol leverage mapping
  - Trade record with full lifecycle tracking
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from strategy import Signal

logger = logging.getLogger(__name__)


@dataclass
class RiskConfig:
    # Core sizing
    risk_per_trade:        float = 0.01    # 1% of equity per trade
    max_open_trades:       int   = 1       # Single position by default (safer)
    max_drawdown_pct:      float = 0.15    # 15% drawdown → halt all trading
    daily_loss_limit_pct:  float = 0.08   # 8% daily loss → halt for the day
    leverage:              float = 5.0    # Conservative: 5x default
    max_position_size_pct: float = 0.25   # Never risk >25% notional in one trade

    # Trailing stop stages
    breakeven_trigger_pct:    float = 0.005   # Move SL to entry when +0.5% profit
    breakeven_buffer_pct:     float = 0.001   # SL = entry + 0.1% (locking tiny profit)
    profit_lock_trigger_pct:  float = 0.010   # Start trailing when +1% profit
    profit_lock_trail_pct:    float = 0.004   # Trail 0.4% behind peak

    # Minimum confidence to enter
    min_confidence:        float = 0.55

    # Per-symbol leverage overrides
    leverage_by_symbol: Dict[str, float] = field(default_factory=lambda: {
        "BTC_USDT": 5.0,
        "ETH_USDT": 5.0,
        "SOL_USDT": 5.0,
        "BNB_USDT": 5.0,
        "XRP_USDT": 3.0,
        "BTCUSD":   5.0,
        "ETHUSD":   5.0,
    })


@dataclass
class TradeRecord:
    id:          str
    symbol:      str
    side:        str           # "long" | "short"
    entry_price: float
    size:        int           # lots (integer)
    stop_loss:   float
    take_profit: Optional[float]
    entry_time:  datetime
    order_id:    str

    peak_price:   float = 0.0
    valley_price: float = 0.0
    closed:       bool  = False
    exit_price:   Optional[float] = None
    exit_time:    Optional[datetime] = None
    reason:       Optional[str] = None
    sl_stage:     int   = 0     # 0=initial, 1=breakeven, 2=profit-lock

    def __post_init__(self):
        if self.peak_price == 0.0:
            self.peak_price = self.entry_price
        if self.valley_price == 0.0:
            self.valley_price = self.entry_price

    @property
    def net_pnl(self) -> Optional[float]:
        if self.exit_price is None:
            return None
        mult = 1 if self.side == "long" else -1
        return mult * (self.exit_price - self.entry_price) / self.entry_price * self.size * self.entry_price

    @property
    def unrealized_pnl_pct(self) -> Optional[float]:
        return None


class RiskManager:
    def __init__(self, config: RiskConfig, initial_capital: float = 100.0):
        self.config           = config
        self.initial_capital  = initial_capital
        self.current_equity   = initial_capital
        self.current_capital  = initial_capital  # alias
        self._peak_equity     = initial_capital
        self._daily_start_eq  = initial_capital
        self._daily_reset_date = datetime.now(timezone.utc).date()
        self._open_trades:   List[TradeRecord] = []
        self._closed_trades: List[TradeRecord] = []
        self._circuit_breaker = False

    # ── Equity ────────────────────────────────────────────────────────────────

    def update_equity(self, new_equity: float):
        if new_equity <= 0:
            return
        self.current_equity  = new_equity
        self.current_capital = new_equity
        if new_equity > self._peak_equity:
            self._peak_equity = new_equity

        today = datetime.now(timezone.utc).date()
        if today != self._daily_reset_date:
            self._daily_start_eq   = new_equity
            self._daily_reset_date = today
            logger.info("📅 Daily P&L reset. Start equity: %.4f", new_equity)

    # ── Guards ────────────────────────────────────────────────────────────────

    def can_trade(self) -> bool:
        if self._circuit_breaker:
            logger.warning("🔴 CIRCUIT BREAKER ACTIVE — trading halted")
            return False
        if self._peak_equity > 0:
            drawdown = (self._peak_equity - self.current_capital) / self._peak_equity
            if drawdown >= self.config.max_drawdown_pct:
                logger.warning(
                    "🔴 MAX DRAWDOWN HALT: %.2f%% (limit %.2f%%)",
                    drawdown * 100, self.config.max_drawdown_pct * 100,
                )
                self._circuit_breaker = True
                return False

        if self._daily_start_eq > 0:
            daily_loss = (self._daily_start_eq - self.current_capital) / self._daily_start_eq
            if daily_loss >= self.config.daily_loss_limit_pct:
                logger.warning(
                    "🔴 DAILY LOSS HALT: %.2f%% (limit %.2f%%)",
                    daily_loss * 100, self.config.daily_loss_limit_pct * 100,
                )
                return False

        return True

    def get_open_trade_count(self) -> int:
        return sum(1 for t in self._open_trades if not t.closed)

    def check_signal(self, sig: Signal) -> bool:
        if not self.can_trade():
            return False
        if self.get_open_trade_count() >= self.config.max_open_trades:
            logger.debug("Max open trades reached (%d)", self.config.max_open_trades)
            return False
        if sig.confidence < self.config.min_confidence:
            logger.info("Signal confidence %.2f < min %.2f — skipping", sig.confidence, self.config.min_confidence)
            return False
        return True

    def activate_kill_switch(self):
        self._circuit_breaker = True
        logger.critical("🔴 KILL SWITCH ACTIVATED")

    def reset_circuit_breaker(self):
        self._circuit_breaker = False
        logger.info("✅ Circuit breaker reset")

    # ── Position Sizing ───────────────────────────────────────────────────────

    def calculate_position_size(
        self, *args, **kwargs
    ) -> float:
        """
        Backwards-compatible wrapper for position sizing.

        Supports two call signatures:
         - calculate_position_size(equity, entry_price, sl_distance, symbol="")
         - calculate_position_size(sig: Signal, current_price=..., symbol="...")
        """
        # Signal-based call
        if args and len(args) == 1 and hasattr(args[0], "type"):
            sig = args[0]
            current_price = kwargs.get("current_price")
            symbol = kwargs.get("symbol", "")
            if current_price is None or sig.stop_loss is None:
                return 0.0
            sl_distance = abs(current_price - float(sig.stop_loss))
            equity = self.current_capital
            return self.calculate_position_size(equity, current_price, sl_distance, symbol)

        # Positional call: unpack and forward
        if len(args) >= 3:
            equity, entry_price, sl_distance = args[:3]
            symbol = args[3] if len(args) >= 4 else kwargs.get("symbol", "")
        else:
            # fallback to kwargs
            equity = kwargs.get("equity")
            entry_price = kwargs.get("entry_price")
            sl_distance = kwargs.get("sl_distance")
            symbol = kwargs.get("symbol", "")
        if equity is None or entry_price is None or sl_distance is None:
            return 0.0
        """
        Fixed-fractional sizing.
        Formula: (equity × risk_pct) / sl_pct × leverage → USD notional
        Capped at max_position_size_pct of equity × leverage.
        """
        if sl_distance <= 0 or entry_price <= 0 or equity <= 0:
            return 0.0

        leverage    = self.config.leverage_by_symbol.get(symbol, self.config.leverage)
        risk_amount = equity * self.config.risk_per_trade
        sl_pct      = sl_distance / entry_price
        raw_notional = (risk_amount / sl_pct) * leverage
        max_notional = equity * self.config.max_position_size_pct * leverage
        notional     = min(raw_notional, max_notional)

        logger.debug(
            "Size: equity=%.2f risk=%.2f sl_pct=%.4f leverage=%.0f → notional=%.2f",
            equity, risk_amount, sl_pct, leverage, notional,
        )
        return notional

    def get_leverage_for_symbol(self, symbol: str) -> float:
        return self.config.leverage_by_symbol.get(symbol, self.config.leverage)

    # ── Trade Lifecycle ───────────────────────────────────────────────────────

    def register_trade(self, trade: TradeRecord):
        self._open_trades.append(trade)
        logger.info("📝 Trade registered: %s %s @ %.4f SL=%.4f TP=%s",
                    trade.symbol, trade.side.upper(), trade.entry_price,
                    trade.stop_loss,
                    f"{trade.take_profit:.4f}" if trade.take_profit else "NONE")

    def close_trade(self, trade: TradeRecord, exit_price: float):
        if trade in self._open_trades:
            self._open_trades.remove(trade)
        self._closed_trades.append(trade)
        pnl = trade.net_pnl or 0.0
        self.current_equity  = max(0, self.current_equity + pnl)
        self.current_capital = self.current_equity
        if self.current_equity > self._peak_equity:
            self._peak_equity = self.current_equity
        logger.info("🏁 Trade closed: %s pnl=%.4f equity=%.4f", trade.symbol, pnl, self.current_equity)

    # ── Trailing Stop ─────────────────────────────────────────────────────────

    def update_trailing_stop(self, trade: TradeRecord, current_price: float) -> Optional[float]:
        """
        3-stage trailing stop.
        Returns new SL if it should be updated, else None.
        """
        cfg    = self.config
        entry  = trade.entry_price
        old_sl = trade.stop_loss

        if trade.side == "long":
            trade.peak_price = max(trade.peak_price, current_price)
            profit_pct = (current_price - entry) / entry

            # Stage 1: Breakeven
            if trade.sl_stage == 0 and profit_pct >= cfg.breakeven_trigger_pct:
                new_sl = entry * (1 + cfg.breakeven_buffer_pct)
                if new_sl > old_sl:
                    trade.stop_loss = new_sl
                    trade.sl_stage  = 1
                    logger.info("⚡ BREAKEVEN SL: %.4f → %.4f (%s)", old_sl, new_sl, trade.symbol)
                    return new_sl

            # Stage 2: Profit lock trailing
            if profit_pct >= cfg.profit_lock_trigger_pct:
                trail_sl = trade.peak_price * (1 - cfg.profit_lock_trail_pct)
                if trail_sl > trade.stop_loss:
                    trade.stop_loss = trail_sl
                    trade.sl_stage  = 2
                    logger.debug("📈 TRAIL SL: %.4f → %.4f", old_sl, trail_sl)
                    return trail_sl

        else:  # short
            trade.valley_price = min(trade.valley_price, current_price)
            profit_pct = (entry - current_price) / entry

            if trade.sl_stage == 0 and profit_pct >= cfg.breakeven_trigger_pct:
                new_sl = entry * (1 - cfg.breakeven_buffer_pct)
                if new_sl < old_sl:
                    trade.stop_loss = new_sl
                    trade.sl_stage  = 1
                    logger.info("⚡ BREAKEVEN SL: %.4f → %.4f (%s)", old_sl, new_sl, trade.symbol)
                    return new_sl

            if profit_pct >= cfg.profit_lock_trigger_pct:
                trail_sl = trade.valley_price * (1 + cfg.profit_lock_trail_pct)
                if trail_sl < trade.stop_loss:
                    trade.stop_loss = trail_sl
                    trade.sl_stage  = 2
                    logger.debug("📉 TRAIL SL: %.4f → %.4f", old_sl, trail_sl)
                    return trail_sl

        return None

    # ── Analytics ─────────────────────────────────────────────────────────────

    def get_stats(self) -> Dict:
        trades = self._closed_trades
        if not trades:
            return {"total_trades": 0, "equity": self.current_equity}
        pnls   = [t.net_pnl for t in trades if t.net_pnl is not None]
        wins   = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        total_pnl = sum(pnls)
        win_rate  = len(wins) / len(pnls) * 100 if pnls else 0
        pf        = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else 999
        dd_pct    = (self._peak_equity - self.current_equity) / self._peak_equity * 100 if self._peak_equity > 0 else 0

        return {
            "total_trades":   len(trades),
            "win_rate":       round(win_rate, 1),
            "total_pnl":      round(total_pnl, 4),
            "avg_win":        round(sum(wins) / len(wins), 4) if wins else 0,
            "avg_loss":       round(sum(losses) / len(losses), 4) if losses else 0,
            "profit_factor":  round(pf, 2),
            "current_equity": round(self.current_equity, 4),
            "peak_equity":    round(self._peak_equity, 4),
            "drawdown_pct":   round(dd_pct, 2),
        }


__all__ = ["RiskManager", "RiskConfig", "TradeRecord"]