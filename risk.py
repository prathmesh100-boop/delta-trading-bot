"""
risk.py — Risk management: position sizing, drawdown protection,
          daily loss limits, trailing stops.

FIXES:
  - calculate_position_size() returns USD notional (float).
    The execution engine converts this to lots via usd_to_lots().
  - record_trade_close() no longer assumes size is in BTC —
    PnL is tracked via realised_pnl which is set by the engine.
  - TradeRecord.size is now treated as integer lots everywhere.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, Optional

from strategy import Signal, SignalType

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

@dataclass
class RiskConfig:
    # Position sizing
    risk_per_trade: float = 0.01          # 1% of capital per trade
    max_position_size_pct: float = 0.20   # never use more than 20% of capital (with leverage)
    leverage: float = 1.0                 # match the leverage set on Delta Exchange

    # Portfolio limits
    max_open_trades: int = 1              # conservative: 1 at a time for small accounts
    max_correlated_exposure: float = 0.10

    # Drawdown / loss limits
    max_drawdown_pct: float = 0.10        # 10% drawdown → halt
    daily_loss_limit_pct: float = 0.05   # 5% daily loss → pause
    weekly_loss_limit_pct: float = 0.10

    # Trailing stop
    trailing_stop_pct: float = 0.02       # 2% from peak

    # Fees (for PnL estimation)
    maker_fee: float = 0.0002
    taker_fee: float = 0.0005
    slippage_pct: float = 0.0003


# ─────────────────────────────────────────────
# Trade record
# ─────────────────────────────────────────────

@dataclass
class TradeRecord:
    symbol: str
    side: str                             # "long" or "short"
    entry_price: float
    size: int                             # ← integer lots
    stop_loss: float
    take_profit: float
    entry_time: datetime
    trailing_stop_price: Optional[float] = None
    peak_price: Optional[float] = None
    order_id: Optional[str] = None
    realised_pnl: float = 0.0
    closed: bool = False
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None


# ─────────────────────────────────────────────
# RiskManager
# ─────────────────────────────────────────────

class RiskManager:
    """
    Central risk management layer.
    calculate_position_size() → returns USD notional (float).
    Execution engine converts that to lots via rest.usd_to_lots().
    """

    def __init__(self, config: RiskConfig, initial_capital: float):
        self.cfg = config
        self.initial_capital = initial_capital
        self.current_capital = initial_capital
        self.peak_capital = initial_capital

        self._open_trades: Dict[str, TradeRecord] = {}

        self._daily_start_capital: float = initial_capital
        self._daily_date: date = date.today()
        self._weekly_start_capital: float = initial_capital

        self.trading_halted: bool = False
        self.daily_limit_hit: bool = False

    def _refresh_daily(self):
        today = date.today()
        if today != self._daily_date:
            logger.info("New trading day — resetting daily PnL tracker")
            self._daily_start_capital = self.current_capital
            self._daily_date = today
            self.daily_limit_hit = False

    # ── Signal gate ───────────────────────────

    def check_signal(self, signal: Signal) -> bool:
        self._refresh_daily()

        if self.trading_halted:
            logger.warning("HALT: Trading halted due to max drawdown breach.")
            return False

        if self.daily_limit_hit:
            logger.warning("HALT: Daily loss limit already hit for today.")
            return False

        if signal.type == SignalType.HOLD:
            return False

        if signal.type in (SignalType.LONG, SignalType.SHORT):
            if len(self._open_trades) >= self.cfg.max_open_trades:
                logger.warning("RISK: Max open trades (%d) reached.", self.cfg.max_open_trades)
                return False

        drawdown = (self.peak_capital - self.current_capital) / self.peak_capital
        if drawdown >= self.cfg.max_drawdown_pct:
            logger.error(
                "HALT: Max drawdown %.2f%% reached (threshold %.2f%%). Halting.",
                drawdown * 100, self.cfg.max_drawdown_pct * 100,
            )
            self.trading_halted = True
            return False

        daily_loss = (self._daily_start_capital - self.current_capital) / max(self._daily_start_capital, 1)
        if daily_loss >= self.cfg.daily_loss_limit_pct:
            logger.error(
                "HALT: Daily loss limit %.2f%% hit. Pausing for today.",
                self.cfg.daily_loss_limit_pct * 100,
            )
            self.daily_limit_hit = True
            return False

        return True

    # ── Position sizing ───────────────────────

    def calculate_position_size(
        self,
        signal: Signal,
        current_price: float,
    ) -> float:
        """
        Returns USD notional to trade.
        The caller converts this to integer lots using rest.usd_to_lots().

        Formula:
            stop_distance_pct = |price - stop_loss| / price
            risk_usd          = capital × risk_per_trade × confidence
            notional_usd      = risk_usd / stop_distance_pct × leverage
            capped at         = capital × max_position_size_pct × leverage
        """
        if signal.stop_loss is None or signal.stop_loss == current_price:
            logger.warning("No valid stop-loss — using default 1% stop distance.")
            stop_distance_pct = 0.01
        else:
            stop_distance_pct = abs(current_price - signal.stop_loss) / current_price
            stop_distance_pct = max(stop_distance_pct, 1e-6)  # avoid division by zero

        risk_usd = self.current_capital * self.cfg.risk_per_trade * signal.confidence
        position_usd = (risk_usd / stop_distance_pct) * self.cfg.leverage

        max_usd = self.current_capital * self.cfg.max_position_size_pct * self.cfg.leverage
        position_usd = min(position_usd, max_usd)

        logger.info(
            "Size calc: capital=%.2f risk_usd=%.2f stop_dist=%.4f%% → %.2f USD notional",
            self.current_capital, risk_usd, stop_distance_pct * 100, position_usd,
        )
        return round(position_usd, 2)

    # ── Trade lifecycle ───────────────────────

    def register_trade(self, trade: TradeRecord):
        key = trade.order_id or trade.symbol
        self._open_trades[key] = trade
        logger.info(
            "Trade registered: %s %s lots=%d entry=%.4f sl=%.4f tp=%.4f",
            trade.side, trade.symbol, trade.size, trade.entry_price,
            trade.stop_loss, trade.take_profit,
        )

    def update_trailing_stops(self, symbol: str, current_price: float) -> Optional[float]:
        for trade in self._open_trades.values():
            if trade.symbol != symbol or trade.closed:
                continue

            if trade.side == "long":
                trade.peak_price = max(trade.peak_price or trade.entry_price, current_price)
                new_trail = trade.peak_price * (1 - self.cfg.trailing_stop_pct)
                if trade.trailing_stop_price is None or new_trail > trade.trailing_stop_price:
                    trade.trailing_stop_price = new_trail
                    logger.debug("Trail stop updated: %s long → %.4f", symbol, new_trail)
                    return new_trail

            elif trade.side == "short":
                trade.peak_price = min(trade.peak_price or trade.entry_price, current_price)
                new_trail = trade.peak_price * (1 + self.cfg.trailing_stop_pct)
                if trade.trailing_stop_price is None or new_trail < trade.trailing_stop_price:
                    trade.trailing_stop_price = new_trail
                    logger.debug("Trail stop updated: %s short → %.4f", symbol, new_trail)
                    return new_trail

        return None

    def should_exit_by_stop(self, symbol: str, current_price: float) -> Optional[TradeRecord]:
        for trade in self._open_trades.values():
            if trade.symbol != symbol or trade.closed:
                continue

            trail = trade.trailing_stop_price
            hard_sl = trade.stop_loss

            if trade.side == "long":
                effective_stop = max(trail or 0, hard_sl)
                if current_price <= effective_stop:
                    logger.info(
                        "Stop triggered: %s long @ %.4f (stop=%.4f)",
                        symbol, current_price, effective_stop,
                    )
                    return trade

            elif trade.side == "short":
                effective_stop = min(trail or float("inf"), hard_sl)
                if current_price >= effective_stop:
                    logger.info(
                        "Stop triggered: %s short @ %.4f (stop=%.4f)",
                        symbol, current_price, effective_stop,
                    )
                    return trade

        return None

    def should_exit_by_tp(self, symbol: str, current_price: float) -> Optional[TradeRecord]:
        for trade in self._open_trades.values():
            if trade.symbol != symbol or trade.closed:
                continue
            if trade.side == "long" and current_price >= trade.take_profit:
                logger.info("Take-profit hit: %s long @ %.4f", symbol, current_price)
                return trade
            if trade.side == "short" and current_price <= trade.take_profit:
                logger.info("Take-profit hit: %s short @ %.4f", symbol, current_price)
                return trade
        return None

    def record_trade_close(self, trade: TradeRecord, exit_price: float, exit_time: datetime):
        """
        Update capital on close. PnL is estimated from entry/exit prices.
        Note: for small accounts the PnL in USD depends on contract_value
        which varies by instrument. The risk manager uses a simplified estimate.
        """
        # Simplified PnL (in USD): price_change × size × contract_value_per_lot
        # For now we use size as a raw count; a more accurate version would
        # multiply by contract_value fetched from the product info.
        price_change = (
            exit_price - trade.entry_price
            if trade.side == "long"
            else trade.entry_price - exit_price
        )
        # Rough fee estimate (taker both legs)
        fee_est = exit_price * self.cfg.taker_fee

        # Estimate net PnL in USD (assumes 1 USD per price tick per lot — adjust
        # contract_value if needed)
        net_pnl = price_change * trade.size - fee_est * trade.size

        trade.realised_pnl = net_pnl
        trade.exit_price = exit_price
        trade.exit_time = exit_time
        trade.closed = True

        self.current_capital += net_pnl
        self.peak_capital = max(self.peak_capital, self.current_capital)

        key = trade.order_id or trade.symbol
        self._open_trades.pop(key, None)

        drawdown = (self.peak_capital - self.current_capital) / self.peak_capital
        logger.info(
            "Trade closed: %s %s pnl≈%.2f capital=%.2f drawdown=%.2f%%",
            trade.symbol, trade.side, net_pnl, self.current_capital, drawdown * 100,
        )

    # ── Stats ─────────────────────────────────

    @property
    def current_drawdown(self) -> float:
        return (self.peak_capital - self.current_capital) / max(self.peak_capital, 1)

    @property
    def daily_pnl_pct(self) -> float:
        return (self.current_capital - self._daily_start_capital) / max(self._daily_start_capital, 1)

    def status_dict(self) -> Dict:
        return {
            "capital": self.current_capital,
            "drawdown_pct": round(self.current_drawdown * 100, 2),
            "daily_pnl_pct": round(self.daily_pnl_pct * 100, 2),
            "open_trades": len(self._open_trades),
            "halted": self.trading_halted,
            "daily_limit_hit": self.daily_limit_hit,
        }
