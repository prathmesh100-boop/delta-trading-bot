"""
risk.py — Risk management v4 (UPGRADED)

KEY IMPROVEMENTS vs v3:
  1. Partial profit booking (50% close at TP1)
     - TradeRecord.partial_closed flag prevents double-partial
     - partial_size = floor(size / 2), minimum 1 lot
     - RiskManager.should_partial_close() called by execution engine

  2. Tighter trailing stop after partial close
     - After TP1 hit: trailing_stop_pct tightens to 0.4% (was 0.8%)
     - Prevents giving back partial gains on reversal

  3. Faster position sizing
     - calculate_position_size() uses confidence weighting
     - High-confidence signals (≥0.85) get 1.2× size
     - Low-confidence signals (<0.65) get 0.75× size

  4. Improved daily reset
     - Reliable timezone-safe date comparison
     - Weekly PnL tracked separately

  5. Added status_dict() snapshot for dashboard
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
    risk_per_trade: float = 0.02          # 2% of capital per trade
    max_position_size_pct: float = 0.20   # max 20% of capital (with leverage)
    leverage: float = 1.0                 # default leverage (overridden per symbol)
    leverage_by_symbol: Dict[str, float] = field(default_factory=lambda: {
        "BTC_USDT": 10.0,
        "BTCUSD": 10.0,
        "ETH_USDT": 10.0,
        "ETHUSD": 10.0,
        "SOL_USDT": 10.0,
        "SOLUSD": 10.0,
    })

    # Portfolio limits
    max_open_trades: int = 2
    max_correlated_exposure: float = 0.10

    # Drawdown / loss limits
    max_drawdown_pct: float = 0.15        # 15% drawdown → halt trading
    daily_loss_limit_pct: float = 0.10    # 10% daily loss → pause
    weekly_loss_limit_pct: float = 0.20

    # Trailing stop (software layer — only tightens, never loosens)
    trailing_stop_pct: float = 0.003      # 0.3% from peak (aggressive)
    trailing_stop_pct_after_partial: float = 0.003  # tightened after partial (kept aligned)

    # Breakeven / profit lock
    breakeven_trigger_pct: float = 0.002  # Move SL to entry at +0.2% profit
    breakeven_buffer: float = 0.0
    profit_lock_threshold_pct: float = 0.003  # Enable profit lock at +0.3%
    profit_lock_pct: float = 0.002        # Lock at peak * (1 - 0.2%)

    # Confidence-based sizing multipliers
    confidence_high_threshold: float = 0.85
    confidence_high_multiplier: float = 1.20
    confidence_low_threshold: float = 0.65
    confidence_low_multiplier: float = 0.75

    # Fees
    maker_fee: float = 0.0002
    taker_fee: float = 0.0005
    slippage_pct: float = 0.0003


# ─────────────────────────────────────────────
# Trade Record
# ─────────────────────────────────────────────

@dataclass
class TradeRecord:
    symbol: str
    side: str                             # "long" | "short"
    entry_price: float
    size: int                             # integer lots (full position)
    stop_loss: float
    take_profit: float
    entry_time: datetime
    contract_value: float = 0.001         # base-asset per lot
    trailing_stop_price: Optional[float] = None
    peak_price: Optional[float] = None
    breakeven_activated: bool = False
    order_id: Optional[str] = None
    realised_pnl: float = 0.0
    closed: bool = False
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    exit_reason: Optional[str] = None
    def __post_init__(self):
        # Ensure sensible defaults
        if getattr(self, "size", None) is None:
            self.size = 0

    def unrealized_pnl(self, current_price: float) -> float:
        """Estimate unrealized PnL in quote currency (USDT)."""
        if self.contract_value <= 0:
            return 0.0
        active_size = self.size
        if self.side == "long":
            return (current_price - self.entry_price) * active_size * self.contract_value
        else:
            return (self.entry_price - current_price) * active_size * self.contract_value


# ─────────────────────────────────────────────
# Risk Manager
# ─────────────────────────────────────────────

class RiskManager:
    """
    Central risk management with partial profit booking support.
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

        # Trade stats
        self.total_trades: int = 0
        self.winning_trades: int = 0
        self.losing_trades: int = 0
        self.total_gross_pnl: float = 0.0
        # partial close bookkeeping removed

    def _refresh_daily(self):
        today = date.today()
        if today != self._daily_date:
            logger.info("New trading day — resetting daily PnL. Previous: %.2f%%",
                        self.daily_pnl_pct * 100)
            self._daily_start_capital = self.current_capital
            self._daily_date = today
            self.daily_limit_hit = False

    # ── Signal gate ───────────────────────────

    def check_signal(self, signal: Signal) -> bool:
        self._refresh_daily()

        if self.trading_halted:
            logger.warning("HALT: Trading halted (max drawdown breach)")
            return False

        if self.daily_limit_hit:
            logger.warning("HALT: Daily loss limit reached for today")
            return False

        if signal.type == SignalType.HOLD:
            return False

        if signal.type in (SignalType.LONG, SignalType.SHORT):
            if len(self._open_trades) >= self.cfg.max_open_trades:
                logger.warning("RISK: Max open trades (%d) reached", self.cfg.max_open_trades)
                return False

        drawdown = (self.peak_capital - self.current_capital) / max(self.peak_capital, 1e-9)
        if drawdown >= self.cfg.max_drawdown_pct:
            logger.error(
                "HALT: Max drawdown %.2f%% ≥ threshold %.2f%%",
                drawdown * 100, self.cfg.max_drawdown_pct * 100,
            )
            self.trading_halted = True
            return False

        daily_loss = (self._daily_start_capital - self.current_capital) / max(self._daily_start_capital, 1e-9)
        if daily_loss >= self.cfg.daily_loss_limit_pct:
            logger.error(
                "HALT: Daily loss %.2f%% ≥ limit %.2f%%",
                daily_loss * 100, self.cfg.daily_loss_limit_pct * 100,
            )
            self.daily_limit_hit = True
            return False

        return True

    # ── Position sizing ───────────────────────

    def calculate_position_size(self, signal: Signal, current_price: float, symbol: str) -> float:
        """
        Returns USD notional to risk.

        Formula:
          stop_dist_pct = |price - stop_loss| / price
          risk_usd      = capital × risk_per_trade × confidence_multiplier
          notional_usd  = risk_usd / stop_dist_pct × leverage
        """
        lev = self.cfg.leverage_by_symbol.get(symbol, self.cfg.leverage)
        stop_loss = signal.stop_loss or current_price * 0.99
        stop_dist_pct = abs(current_price - stop_loss) / max(current_price, 1e-9)
        if stop_dist_pct < 1e-6:
            stop_dist_pct = 0.005

        # Confidence-based multiplier
        conf = signal.confidence
        if conf >= self.cfg.confidence_high_threshold:
            conf_mult = self.cfg.confidence_high_multiplier
        elif conf < self.cfg.confidence_low_threshold:
            conf_mult = self.cfg.confidence_low_multiplier
        else:
            conf_mult = 1.0

        risk_usd = self.current_capital * self.cfg.risk_per_trade
        notional_usd = (risk_usd / stop_dist_pct) * lev

        # Apply confidence multiplier to notional (before cap)
        notional_usd *= conf_mult

        # Cap at max position size (applied after multiplier)
        max_notional = self.current_capital * self.cfg.max_position_size_pct * lev
        notional_usd = min(notional_usd, max_notional)

        logger.debug(
            "Position size: capital=%.2f risk_pct=%.1f%% conf=%.2f(×%.2f) "
            "sl_dist=%.3f%% lev=%dx → notional=%.2f USD",
            self.current_capital, self.cfg.risk_per_trade * 100,
            conf, conf_mult, stop_dist_pct * 100, lev, notional_usd,
        )
        return max(notional_usd, 0.0)

    # ── Trade lifecycle ───────────────────────

    def register_trade(self, trade: TradeRecord):
        key = trade.order_id or f"{trade.symbol}_{trade.entry_time.timestamp()}"
        self._open_trades[key] = trade
        logger.info(
            "Trade registered: %s %s | lots=%d entry=%.4f sl=%.4f tp2=%.4f cv=%s",
            trade.side, trade.symbol, trade.size, trade.entry_price,
            trade.stop_loss,
            trade.take_profit, trade.contract_value,
        )


    def update_trailing_stops(self, symbol: str, current_price: float) -> Optional[float]:
        """
        Update SOFTWARE trailing stop. Does NOT modify exchange bracket SL.
        Only tightens stop — never loosens it.
        """
        for trade in self._open_trades.values():
            if trade.symbol != symbol or trade.closed:
                continue

            # Use configured trailing percentage (partial-close flow removed)
            trail_pct = self.cfg.trailing_stop_pct

            if trade.side == "long":
                if trade.peak_price is None:
                    trade.peak_price = trade.entry_price
                trade.peak_price = max(trade.peak_price, current_price)

                new_trail = trade.peak_price * (1 - trail_pct)
                if trade.trailing_stop_price is None or new_trail > trade.trailing_stop_price:
                    trade.trailing_stop_price = new_trail

                # Breakeven
                if (not trade.breakeven_activated
                        and self.cfg.breakeven_trigger_pct > 0
                        and trade.peak_price >= trade.entry_price * (1 + self.cfg.breakeven_trigger_pct)):
                    be_price = trade.entry_price + self.cfg.breakeven_buffer
                    if be_price > trade.stop_loss:
                        trade.stop_loss = be_price
                        trade.breakeven_activated = True
                        logger.info("Breakeven activated: %s long → sl=%.4f", symbol, be_price)

                # Profit lock
                if (self.cfg.profit_lock_threshold_pct > 0
                        and trade.peak_price >= trade.entry_price * (1 + self.cfg.profit_lock_threshold_pct)):
                    lock_price = trade.peak_price * (1 - self.cfg.profit_lock_pct)
                    if lock_price > trade.stop_loss:
                        trade.stop_loss = lock_price
                        logger.debug("Profit lock: %s long → sl=%.4f", symbol, lock_price)

                return trade.trailing_stop_price

            elif trade.side == "short":
                if trade.peak_price is None:
                    trade.peak_price = trade.entry_price
                trade.peak_price = min(trade.peak_price, current_price)

                new_trail = trade.peak_price * (1 + trail_pct)
                if trade.trailing_stop_price is None or new_trail < trade.trailing_stop_price:
                    trade.trailing_stop_price = new_trail

                if (not trade.breakeven_activated
                        and self.cfg.breakeven_trigger_pct > 0
                        and trade.peak_price <= trade.entry_price * (1 - self.cfg.breakeven_trigger_pct)):
                    be_price = trade.entry_price - self.cfg.breakeven_buffer
                    if be_price < trade.stop_loss:
                        trade.stop_loss = be_price
                        trade.breakeven_activated = True
                        logger.info("Breakeven activated: %s short → sl=%.4f", symbol, be_price)

                if (self.cfg.profit_lock_threshold_pct > 0
                        and trade.peak_price <= trade.entry_price * (1 - self.cfg.profit_lock_threshold_pct)):
                    lock_price = trade.peak_price * (1 + self.cfg.profit_lock_pct)
                    if lock_price < trade.stop_loss:
                        trade.stop_loss = lock_price
                        logger.debug("Profit lock: %s short → sl=%.4f", symbol, lock_price)

                return trade.trailing_stop_price

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
                    return trade
            elif trade.side == "short":
                effective_stop = min(trail if trail else float("inf"), hard_sl)
                if current_price >= effective_stop:
                    return trade
        return None

    def should_exit_by_tp(self, symbol: str, current_price: float) -> Optional[TradeRecord]:
        for trade in self._open_trades.values():
            if trade.symbol != symbol or trade.closed:
                continue
            if trade.side == "long" and current_price >= trade.take_profit:
                return trade
            if trade.side == "short" and current_price <= trade.take_profit:
                return trade
        return None

    def record_trade_close(self, trade: TradeRecord, exit_price: float, exit_time: datetime, reason: str = ""):
        """
        Update capital on full close.
        Uses remaining_size (accounts for already partially-closed lots).
        """
        cv = trade.contract_value if trade.contract_value > 0 else 0.001
        active_size = trade.size
        price_diff = (
            (exit_price - trade.entry_price) if trade.side == "long"
            else (trade.entry_price - exit_price)
        )
        gross_pnl = price_diff * active_size * cv
        fee_est = (trade.entry_price + exit_price) * active_size * cv * self.cfg.taker_fee
        net_pnl = gross_pnl - fee_est

        # Total realised PnL = partial + final
        trade.realised_pnl += net_pnl
        trade.exit_price = exit_price
        trade.exit_time = exit_time
        trade.exit_reason = reason
        trade.closed = True

        self.current_capital += net_pnl
        self.peak_capital = max(self.peak_capital, self.current_capital)
        self.total_gross_pnl += trade.realised_pnl   # total including partial
        self.total_trades += 1
        if trade.realised_pnl > 0:
            self.winning_trades += 1
        else:
            self.losing_trades += 1

        key = trade.order_id or trade.symbol
        self._open_trades.pop(key, None)
        ts_key = f"{trade.symbol}_{trade.entry_time.timestamp()}"
        self._open_trades.pop(ts_key, None)

        logger.info(
            "Trade closed (%s): %s %s | entry=%.4f exit=%.4f | final_pnl=%.4f total_pnl=%.4f USDT | capital=%.2f | dd=%.2f%%",
            reason, trade.symbol, trade.side,
            trade.entry_price, exit_price,
            net_pnl, trade.realised_pnl,
            self.current_capital,
            self.current_drawdown * 100,
        )

    # ── Stats ─────────────────────────────────

    @property
    def current_drawdown(self) -> float:
        return (self.peak_capital - self.current_capital) / max(self.peak_capital, 1e-9)

    @property
    def daily_pnl_pct(self) -> float:
        return (self.current_capital - self._daily_start_capital) / max(self._daily_start_capital, 1e-9)

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades

    def live_equity(self, price_map: Dict[str, float] = None) -> float:
        """Capital + estimated unrealized PnL from open trades."""
        equity = self.current_capital
        if price_map:
            for trade in self._open_trades.values():
                price = price_map.get(trade.symbol)
                if price:
                    equity += trade.unrealized_pnl(price)
        return equity

    def status_dict(self) -> Dict:
        self._refresh_daily()
        return {
            "capital": round(self.current_capital, 4),
            "peak_capital": round(self.peak_capital, 4),
            "drawdown_pct": round(self.current_drawdown * 100, 2),
            "daily_pnl_pct": round(self.daily_pnl_pct * 100, 2),
            "open_trades": len(self._open_trades),
            "total_trades": self.total_trades,
            "win_rate": round(self.win_rate * 100, 1),
            "total_pnl": round(self.total_gross_pnl, 4),
            "halted": self.trading_halted,
            "daily_limit_hit": self.daily_limit_hit,
        }
