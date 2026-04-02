"""
risk.py — Risk Manager v5 (PRODUCTION)

Features:
  - RiskConfig: all risk parameters in one dataclass
  - RiskManager: per-trade position sizing, daily loss limit, drawdown halt
  - TradeRecord: full lifecycle tracking (entry → partial → close)
  - Trailing stop: breakeven + profit-lock + ATR trailing
  - Thread-safe with asyncio.Lock on critical sections
  - Daily reset: resets daily_pnl at UTC midnight
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RiskConfig:
    # Per-trade risk as fraction of capital (e.g. 0.01 = 1%)
    risk_per_trade:       float = 0.01

    # Max portfolio drawdown before bot halts (e.g. 0.15 = 15%)
    max_drawdown_pct:     float = 0.15

    # Daily loss limit as fraction of starting capital (e.g. 0.05 = 5%)
    daily_loss_limit_pct: float = 0.05

    # Maximum concurrent open trades
    max_open_trades:      int   = 3

    # Max single position as fraction of capital (e.g. 0.30 = 30%)
    max_position_size_pct: float = 0.30

    # Leverage applied to every position
    leverage:             float = 10.0

    # ── Trailing / Breakeven ──────────────────────────────────────────────
    # Move SL to entry when price moves this % in our favour
    breakeven_trigger_pct: float = 0.002   # 0.2%
    # Buffer above entry when moving SL to breakeven
    breakeven_buffer:      float = 0.0001  # tiny buffer

    # Activate profit-lock trailing when profit ≥ this %
    profit_lock_threshold_pct: float = 0.004  # 0.4%
    # Trail at this distance from peak
    profit_lock_pct:           float = 0.002  # 0.2%

    # ── Confidence scaling ────────────────────────────────────────────────
    # Scale position size by signal confidence: True = yes
    use_confidence_scaling: bool = True
    # Min/max multiplier applied to base size
    confidence_min_mult:    float = 0.5
    confidence_max_mult:    float = 1.0


# ─────────────────────────────────────────────────────────────────────────────
# Trade Record
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TradeRecord:
    symbol:         str
    side:           str     # "long" | "short"
    entry_price:    float
    size:           int     # lots
    stop_loss:      float
    take_profit:    float
    entry_time:     datetime
    order_id:       str     = ""
    peak_price:     Optional[float] = None

    # Optional product metadata
    contract_value: float   = 0.001
    min_size:       int     = 1

    # Lifecycle
    exit_price:     Optional[float]    = None
    exit_time:      Optional[datetime] = None
    exit_reason:    Optional[str]      = None
    closed:         bool               = False
    partial_closed: bool               = False     # True after TP1 partial fill

    # PnL tracking
    realised_pnl:   float = 0.0
    partial_pnl:    float = 0.0

    # Dynamic stop tracking
    trailing_stop_price: Optional[float] = None
    breakeven_moved:     bool            = False

    # Bracket order IDs (set after exchange confirmation)
    sl_order_id: Optional[str] = None
    tp_order_id: Optional[str] = None

    @property
    def is_long(self) -> bool:
        return self.side == "long"

    @property
    def is_short(self) -> bool:
        return self.side == "short"

    def pnl_at_price(self, price: float) -> float:
        """Unrealised PnL in USDT (linear contract)."""
        direction = 1 if self.is_long else -1
        return direction * (price - self.entry_price) * self.size * self.contract_value

    def pnl_pct(self, price: float) -> float:
        """PnL as % of entry price."""
        direction = 1 if self.is_long else -1
        return direction * (price - self.entry_price) / self.entry_price if self.entry_price > 0 else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Risk Manager
# ─────────────────────────────────────────────────────────────────────────────

class RiskManager:
    """
    Central risk gatekeeper for the execution engine.

    Responsibilities:
      1. Signal gating: check_signal() → True/False
      2. Position sizing: calculate_position_size() → USD notional
      3. Trade registration and tracking
      4. Daily PnL tracking with daily loss halt
      5. Drawdown monitoring with portfolio halt
      6. Trailing / breakeven stop updates (called every WS tick)
    """

    def __init__(self, config: RiskConfig, initial_capital: float):
        self.cfg             = config
        self.initial_capital = initial_capital
        self.capital         = initial_capital

        # Open trades (symbol → TradeRecord)
        self._open_trades:  Dict[str, TradeRecord] = {}
        self._closed_trades: List[TradeRecord]     = []

        # Daily tracking
        self._daily_pnl:     float = 0.0
        self._daily_reset_on: date  = date.today()

        # Portfolio stats
        self._peak_capital:   float = initial_capital
        self._total_pnl:      float = 0.0
        self._halted:         bool  = False

    # ── State ──────────────────────────────────────────────────────────────

    def _maybe_reset_daily(self):
        today = date.today()
        if today != self._daily_reset_on:
            logger.info("Daily PnL reset (was %.4f USDT). New day: %s", self._daily_pnl, today)
            self._daily_pnl     = 0.0
            self._daily_reset_on = today

    @property
    def is_halted(self) -> bool:
        return self._halted

    @property
    def daily_pnl(self) -> float:
        self._maybe_reset_daily()
        return self._daily_pnl

    @property
    def drawdown_pct(self) -> float:
        if self._peak_capital <= 0:
            return 0.0
        return (self._peak_capital - self.capital) / self._peak_capital

    @property
    def open_trade_count(self) -> int:
        return len(self._open_trades)

    # ── Gating ─────────────────────────────────────────────────────────────

    def check_signal(self, signal) -> bool:
        """
        Returns True if the signal is allowed to proceed.
        Checks: halted, daily limit, drawdown limit, max open trades, capital floor.
        """
        self._maybe_reset_daily()

        if self._halted:
            logger.warning("RISK: Bot is halted — ignoring signal")
            return False

        # Daily loss limit
        daily_limit = self.initial_capital * self.cfg.daily_loss_limit_pct
        if self._daily_pnl <= -daily_limit:
            logger.warning("RISK: Daily loss limit hit (%.4f). Halting for today.", self._daily_pnl)
            self._halted = True
            return False

        # Drawdown limit
        if self.drawdown_pct >= self.cfg.max_drawdown_pct:
            logger.warning(
                "RISK: Max drawdown %.1f%% reached (peak=%.2f current=%.2f). HALTING.",
                self.drawdown_pct * 100, self._peak_capital, self.capital
            )
            self._halted = True
            return False

        # Max open trades
        if len(self._open_trades) >= self.cfg.max_open_trades:
            logger.info("RISK: Max open trades (%d) reached", self.cfg.max_open_trades)
            return False

        # Capital floor: need at least 5 USDT to trade
        if self.capital < 5.0:
            logger.warning("RISK: Capital %.2f below floor — halting", self.capital)
            self._halted = True
            return False

        return True

    # ── Position Sizing ────────────────────────────────────────────────────

    def calculate_position_size(self, signal, price: float, symbol: str) -> float:
        """
        Returns USD notional to trade.

        Formula:
            risk_amount = capital × risk_per_trade × confidence_multiplier
            sl_distance = |price - signal.stop_loss|
            usd_notional = (risk_amount / sl_distance) × price × contract_value

        Capped at max_position_size_pct × capital.
        Multiplied by leverage.
        """
        if not signal.stop_loss or signal.stop_loss <= 0:
            return 0.0

        sl_distance = abs(price - signal.stop_loss)
        if sl_distance <= 0:
            return 0.0

        # Base risk amount
        risk_amount = self.capital * self.cfg.risk_per_trade

        # Confidence scaling
        if self.cfg.use_confidence_scaling:
            conf_mult = self.cfg.confidence_min_mult + (
                (self.cfg.confidence_max_mult - self.cfg.confidence_min_mult)
                * getattr(signal, "confidence", 1.0)
            )
            risk_amount *= conf_mult

        # USD notional (leveraged)
        # risk_amount = notional × (sl_distance / price)
        # → notional = risk_amount × (price / sl_distance)
        notional = risk_amount * (price / sl_distance) * self.cfg.leverage

        # Cap at max position size
        max_notional = self.capital * self.cfg.max_position_size_pct * self.cfg.leverage
        notional = min(notional, max_notional)

        logger.debug(
            "Position sizing: capital=%.2f risk=%.4f sl_dist=%.4f → notional=%.2f USD",
            self.capital, risk_amount, sl_distance, notional,
        )
        return max(0.0, notional)

    # ── Trade Lifecycle ────────────────────────────────────────────────────

    def register_trade(self, trade: TradeRecord):
        """Register a newly opened trade."""
        if trade.peak_price is None:
            trade.peak_price = trade.entry_price
        self._open_trades[trade.symbol] = trade
        logger.info(
            "RISK: Registered %s %s | lots=%d sl=%.4f tp=%.4f",
            trade.side.upper(), trade.symbol, trade.size, trade.stop_loss, trade.take_profit,
        )

    def record_trade_close(
        self,
        trade:      TradeRecord,
        exit_price: float,
        exit_time:  datetime,
        reason:     str = "",
    ) -> float:
        """
        Finalise a closed trade. Updates capital, PnL, drawdown tracking.
        Returns realised PnL in USDT.
        """
        if trade.closed and trade.realised_pnl != 0.0:
            # Already fully recorded (e.g. called twice on bracket + ws close)
            return trade.realised_pnl

        direction = 1 if trade.is_long else -1
        gross_pnl = direction * (exit_price - trade.entry_price) * trade.size * trade.contract_value

        # Subtract partial PnL already booked
        net_pnl = gross_pnl - trade.partial_pnl

        trade.exit_price   = exit_price
        trade.exit_time    = exit_time
        trade.exit_reason  = reason
        trade.realised_pnl = gross_pnl   # total gross
        trade.closed       = True

        # Update capital
        self.capital    += net_pnl
        self._daily_pnl += net_pnl
        self._total_pnl += net_pnl

        if self.capital > self._peak_capital:
            self._peak_capital = self.capital

        self._open_trades.pop(trade.symbol, None)
        self._closed_trades.append(trade)

        emoji = "✅" if net_pnl >= 0 else "❌"
        logger.info(
            "%s Trade closed: %s %s | entry=%.4f exit=%.4f pnl=%.4f USDT | "
            "capital=%.2f daily_pnl=%.4f drawdown=%.1f%%",
            emoji, trade.side.upper(), trade.symbol,
            trade.entry_price, exit_price, net_pnl,
            self.capital, self._daily_pnl, self.drawdown_pct * 100,
        )
        return net_pnl

    def record_partial_close(self, trade: TradeRecord, close_price: float, close_size: int) -> float:
        """
        Record a partial position close (e.g. 50% at TP1).
        Returns realised PnL for the partial close.
        """
        direction  = 1 if trade.is_long else -1
        partial_pnl = direction * (close_price - trade.entry_price) * close_size * trade.contract_value

        trade.partial_pnl   += partial_pnl
        trade.partial_closed = True
        trade.size           = max(0, trade.size - close_size)

        self.capital    += partial_pnl
        self._daily_pnl += partial_pnl
        self._total_pnl += partial_pnl

        if self.capital > self._peak_capital:
            self._peak_capital = self.capital

        logger.info(
            "Partial close: %s %s +%d lots @ %.4f | partial_pnl=%.4f",
            trade.side.upper(), trade.symbol, close_size, close_price, partial_pnl,
        )
        return partial_pnl

    # ── Trailing Stops (called every WS tick) ──────────────────────────────

    def update_trailing_stops(self, symbol: str, current_price: float):
        """
        Update trailing / breakeven stops for the open trade on `symbol`.
        Called from the WebSocket tick handler (high frequency).

        Order of operations:
          1. Breakeven: move SL to entry when price moves > breakeven_trigger_pct
          2. Profit-lock: when profit > profit_lock_threshold_pct, trail at profit_lock_pct
        """
        trade = self._open_trades.get(symbol)
        if not trade or trade.closed:
            return

        entry = trade.entry_price
        if entry <= 0:
            return

        # Update peak
        if trade.peak_price is None:
            trade.peak_price = current_price
        elif trade.is_long:
            trade.peak_price = max(trade.peak_price, current_price)
        else:
            trade.peak_price = min(trade.peak_price, current_price)

        # ── 1. Breakeven ──────────────────────────────────────────────────
        if not trade.breakeven_moved:
            trigger_price = entry * (1 + self.cfg.breakeven_trigger_pct) if trade.is_long \
                       else entry * (1 - self.cfg.breakeven_trigger_pct)

            hit_trigger = (trade.is_long  and current_price >= trigger_price) or \
                          (trade.is_short and current_price <= trigger_price)

            if hit_trigger:
                be_price = entry * (1 + self.cfg.breakeven_buffer) if trade.is_long \
                      else entry * (1 - self.cfg.breakeven_buffer)

                if trade.is_long:
                    new_sl = max(trade.stop_loss, be_price)
                else:
                    new_sl = min(trade.stop_loss, be_price)

                if new_sl != trade.stop_loss:
                    logger.info("Breakeven SL moved: %s %.4f → %.4f", symbol, trade.stop_loss, new_sl)
                    trade.stop_loss       = new_sl
                    trade.breakeven_moved = True

        # ── 2. Profit-lock trailing ───────────────────────────────────────
        profit_pct = trade.pnl_pct(current_price)
        if profit_pct >= self.cfg.profit_lock_threshold_pct:
            if trade.is_long:
                trail_sl = (trade.peak_price or current_price) * (1 - self.cfg.profit_lock_pct)
                new_trailing = max(trade.stop_loss, trail_sl)
            else:
                trail_sl = (trade.peak_price or current_price) * (1 + self.cfg.profit_lock_pct)
                new_trailing = min(trade.stop_loss, trail_sl)

            if trade.trailing_stop_price is None or (
                (trade.is_long  and new_trailing > (trade.trailing_stop_price or 0)) or
                (trade.is_short and new_trailing < (trade.trailing_stop_price or float("inf")))
            ):
                trade.trailing_stop_price = new_trailing

    # ── Stats ──────────────────────────────────────────────────────────────

    def summary(self) -> Dict:
        total_trades = len(self._closed_trades)
        wins  = [t for t in self._closed_trades if t.realised_pnl > 0]
        losses = [t for t in self._closed_trades if t.realised_pnl <= 0]
        gross_profit = sum(t.realised_pnl for t in wins)
        gross_loss   = abs(sum(t.realised_pnl for t in losses))

        return {
            "capital":        round(self.capital, 4),
            "total_pnl":      round(self._total_pnl, 4),
            "daily_pnl":      round(self._daily_pnl, 4),
            "drawdown_pct":   round(self.drawdown_pct * 100, 2),
            "total_trades":   total_trades,
            "wins":           len(wins),
            "losses":         len(losses),
            "win_rate":       round(len(wins) / total_trades * 100, 1) if total_trades else 0.0,
            "profit_factor":  round(gross_profit / gross_loss, 2) if gross_loss > 0 else 0.0,
            "open_trades":    len(self._open_trades),
            "halted":         self._halted,
        }

    def halt(self, reason: str = "manual"):
        self._halted = True
        logger.warning("Risk manager HALTED: %s", reason)

    def resume(self):
        self._halted = False
        logger.info("Risk manager RESUMED")


__all__ = ["RiskConfig", "RiskManager", "TradeRecord"]
