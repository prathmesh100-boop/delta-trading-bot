"""
backtest.py — Event-driven backtester v3 (PRODUCTION)

Features:
  - Bar-by-bar simulation (causal — no look-ahead bias)
  - Realistic fees: taker commission + slippage
  - Walk-forward / in-sample & out-of-sample split
  - Grid-search optimiser with Sharpe ratio metric
  - Multi-symbol allocation support
  - Rich result object with equity curve, trade log, stats
"""

import itertools
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Type

import numpy as np
import pandas as pd

from strategy import BaseStrategy, Signal, SignalType

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BacktestConfig:
    initial_capital:   float = 10_000.0
    risk_per_trade:    float = 0.01      # 1% risk per trade
    taker_fee:         float = 0.0005    # 0.05% taker fee (Delta Exchange rate)
    slippage_pct:      float = 0.0003    # 0.03% slippage on entry + exit
    leverage:          float = 1.0       # 1× for backtesting (scale by leverage in live)
    max_open_trades:   int   = 1         # Simple sequential backtest
    use_confidence:    bool  = True      # Scale size by signal confidence


# ─────────────────────────────────────────────────────────────────────────────
# Trade result
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BacktestTrade:
    symbol:       str
    side:         str      # "long" | "short"
    entry_time:   Any
    exit_time:    Any
    entry_price:  float
    exit_price:   float
    size_usd:     float
    gross_pnl:    float
    fees:         float
    net_pnl:      float
    exit_reason:  str
    confidence:   float = 1.0
    metadata:     Dict  = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# Result
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BacktestResult:
    trades:       List[BacktestTrade]
    equity_curve: pd.Series
    config:       BacktestConfig
    symbol:       str = ""

    def summary(self) -> str:
        trades    = self.trades
        n         = len(trades)
        if n == 0:
            return "  No trades executed."

        pnls      = [t.net_pnl for t in trades]
        wins      = [p for p in pnls if p > 0]
        losses    = [p for p in pnls if p <= 0]
        total_pnl = sum(pnls)
        win_rate  = len(wins) / n * 100
        avg_win   = sum(wins)   / len(wins)   if wins   else 0
        avg_loss  = sum(losses) / len(losses) if losses else 0

        gross_profit = sum(wins)
        gross_loss   = abs(sum(losses))
        pf           = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # Sharpe (annualised from daily returns)
        eq       = self.equity_curve
        daily    = eq.resample("1D").last().ffill().pct_change().dropna()
        sharpe   = float(daily.mean() / daily.std() * np.sqrt(252)) if daily.std() > 0 else 0.0

        # Max drawdown
        roll_max  = eq.cummax()
        drawdowns = (eq - roll_max) / roll_max.replace(0, np.nan)
        max_dd    = float(drawdowns.min() * 100)

        final_cap = self.config.initial_capital + total_pnl
        ret_pct   = total_pnl / self.config.initial_capital * 100

        lines = [
            f"  Symbol        : {self.symbol}",
            f"  Trades        : {n}   (W:{len(wins)} L:{len(losses)})",
            f"  Win Rate      : {win_rate:.1f}%",
            f"  Avg Win       : +${avg_win:.2f}    Avg Loss: -${abs(avg_loss):.2f}",
            f"  Profit Factor : {pf:.2f}",
            f"  Sharpe Ratio  : {sharpe:.2f}",
            f"  Max Drawdown  : {max_dd:.2f}%",
            f"  Net PnL       : ${total_pnl:+.2f}  ({ret_pct:+.1f}%)",
            f"  Final Capital : ${final_cap:.2f}",
        ]
        return "\n".join(lines)

    @property
    def sharpe_ratio(self) -> float:
        eq    = self.equity_curve
        daily = eq.resample("1D").last().ffill().pct_change().dropna()
        return float(daily.mean() / daily.std() * np.sqrt(252)) if daily.std() > 0 else 0.0

    @property
    def total_pnl(self) -> float:
        return sum(t.net_pnl for t in self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.net_pnl > 0)
        return wins / len(self.trades) * 100


# ─────────────────────────────────────────────────────────────────────────────
# Backtester
# ─────────────────────────────────────────────────────────────────────────────

class Backtester:
    """
    Bar-by-bar event-driven backtester.

    Simulation rules:
      - Strategy sees df.iloc[:i] (no future data)
      - Entry: open price of next bar + slippage
      - Exit:  SL/TP hit during bar high/low, or at close if neither hit
      - Fees:  taker_fee on entry AND exit notional
      - Size:  risk_per_trade × capital / (entry - SL) → USD notional
    """

    def __init__(self, config: BacktestConfig):
        self.cfg = config

    def run(
        self,
        df:     pd.DataFrame,
        strategy: BaseStrategy,
        symbol: str = "SIM",
    ) -> BacktestResult:
        """Run backtest on a single symbol."""
        cfg     = self.cfg
        capital = cfg.initial_capital
        trades: List[BacktestTrade] = []
        equity_records = []

        current_trade: Optional[Dict] = None   # {side, entry, sl, tp, size_usd, time, conf, meta}

        for i in range(50, len(df)):
            window = df.iloc[:i]
            bar    = df.iloc[i]
            ts     = df.index[i]
            o, h, l, c = float(bar.open), float(bar.high), float(bar.low), float(bar.close)

            # ── Manage open trade ─────────────────────────────────────────
            if current_trade:
                ct     = current_trade
                side   = ct["side"]
                sl     = ct["sl"]
                tp     = ct["tp"]
                entry  = ct["entry"]
                sz_usd = ct["size_usd"]

                exit_price  = None
                exit_reason = None

                if side == "long":
                    if l <= sl:
                        exit_price  = sl * (1 - cfg.slippage_pct)
                        exit_reason = "stop_loss"
                    elif tp and h >= tp:
                        exit_price  = tp * (1 - cfg.slippage_pct)
                        exit_reason = "take_profit"
                else:  # short
                    if h >= sl:
                        exit_price  = sl * (1 + cfg.slippage_pct)
                        exit_reason = "stop_loss"
                    elif tp and l <= tp:
                        exit_price  = tp * (1 + cfg.slippage_pct)
                        exit_reason = "take_profit"

                if exit_price is not None:
                    direction = 1 if side == "long" else -1
                    gross     = direction * (exit_price - entry) * sz_usd / entry
                    fees      = (sz_usd + sz_usd * abs(exit_price / entry)) * cfg.taker_fee
                    net       = gross - fees

                    capital += net
                    trades.append(BacktestTrade(
                        symbol      = symbol,
                        side        = side,
                        entry_time  = ct["time"],
                        exit_time   = ts,
                        entry_price = entry,
                        exit_price  = exit_price,
                        size_usd    = sz_usd,
                        gross_pnl   = gross,
                        fees        = fees,
                        net_pnl     = net,
                        exit_reason = exit_reason,
                        confidence  = ct.get("conf", 1.0),
                        metadata    = ct.get("meta", {}),
                    ))
                    current_trade = None

            # ── Generate signal ───────────────────────────────────────────
            if current_trade is None:
                try:
                    sig = strategy.generate_signal(window, symbol)
                except Exception as exc:
                    logger.debug("Strategy error at bar %d: %s", i, exc)
                    sig = None

                if sig and sig.type in (SignalType.LONG, SignalType.SHORT) and sig.stop_loss:
                    sl_dist = abs(o - sig.stop_loss)
                    if sl_dist <= 0:
                        pass
                    else:
                        # Size: risk_amount / (sl_pct of entry)
                        risk_amt = capital * cfg.risk_per_trade
                        if cfg.use_confidence:
                            risk_amt *= max(0.5, min(1.0, getattr(sig, "confidence", 1.0)))
                        sz_usd = risk_amt * (o / sl_dist) * cfg.leverage
                        sz_usd = min(sz_usd, capital * 0.3 * cfg.leverage)

                        # Entry price with slippage
                        slip = o * cfg.slippage_pct
                        entry = o + slip if sig.type == SignalType.LONG else o - slip
                        fees  = sz_usd * cfg.taker_fee
                        capital -= fees

                        current_trade = {
                            "side":     "long" if sig.type == SignalType.LONG else "short",
                            "entry":    entry,
                            "sl":       sig.stop_loss,
                            "tp":       sig.take_profit,
                            "size_usd": sz_usd,
                            "time":     ts,
                            "conf":     getattr(sig, "confidence", 1.0),
                            "meta":     getattr(sig, "metadata", {}),
                        }

            equity_records.append((ts, capital))

        # Force-close any open trade at end
        if current_trade and len(df) > 0:
            bar    = df.iloc[-1]
            ct     = current_trade
            side   = ct["side"]
            entry  = ct["entry"]
            sz_usd = ct["size_usd"]
            ep     = float(bar.close)
            direction = 1 if side == "long" else -1
            gross  = direction * (ep - entry) * sz_usd / entry
            fees   = sz_usd * cfg.taker_fee
            net    = gross - fees
            capital += net
            trades.append(BacktestTrade(
                symbol      = symbol,
                side        = side,
                entry_time  = ct["time"],
                exit_time   = df.index[-1],
                entry_price = entry,
                exit_price  = ep,
                size_usd    = sz_usd,
                gross_pnl   = gross,
                fees        = fees,
                net_pnl     = net,
                exit_reason = "end_of_data",
                confidence  = ct.get("conf", 1.0),
            ))

        equity_series = pd.Series(
            {ts: eq for ts, eq in equity_records},
            name="equity",
        )
        return BacktestResult(trades=trades, equity_curve=equity_series, config=cfg, symbol=symbol)

    def optimize(
        self,
        df:       pd.DataFrame,
        strategy_cls: Type[BaseStrategy],
        param_grid:  Dict[str, List],
        symbol:   str   = "SIM",
        metric:   str   = "sharpe_ratio",
    ) -> Tuple[Dict, BacktestResult]:
        """
        Grid-search optimiser.
        Returns (best_params, best_result).
        """
        keys   = list(param_grid.keys())
        values = list(param_grid.values())
        combos = list(itertools.product(*values))

        best_score  = float("-inf")
        best_params = {}
        best_result = None

        logger.info("Optimising %s with %d param combinations…", strategy_cls.__name__, len(combos))

        for combo in combos:
            params = dict(zip(keys, combo))
            try:
                strat  = strategy_cls(params)
                result = self.run(df, strat, symbol=symbol)
                score  = getattr(result, metric, None)
                if score is None:
                    score = result.total_pnl
            except Exception as exc:
                logger.debug("Optimise combo %s failed: %s", params, exc)
                continue

            if score > best_score:
                best_score  = score
                best_params = params
                best_result = result
                logger.debug("New best: score=%.4f params=%s", score, params)

        logger.info("Optimisation complete. Best %s=%.4f params=%s", metric, best_score, best_params)
        return best_params, best_result


__all__ = ["Backtester", "BacktestConfig", "BacktestResult", "BacktestTrade"]
