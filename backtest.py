"""
backtest.py — Event-driven backtester with full performance metrics.
Supports fee/slippage simulation and parameter optimization.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from itertools import product as iterproduct
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from strategy import BaseStrategy, Signal, SignalType

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Backtest Config
# ─────────────────────────────────────────────

@dataclass
class BacktestConfig:
    initial_capital: float = 10_000.0
    risk_per_trade: float = 0.01          # 1%
    max_position_size_pct: float = 0.10
    maker_fee: float = 0.0002
    taker_fee: float = 0.0005
    slippage_pct: float = 0.0003
    leverage: float = 1.0


# ─────────────────────────────────────────────
# Trade log entry
# ─────────────────────────────────────────────

@dataclass
class BacktestTrade:
    entry_time: datetime
    exit_time: Optional[datetime]
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    size_usd: float
    gross_pnl: float
    net_pnl: float
    exit_reason: str                       # "sl", "tp", "signal", "eod"
    holding_bars: int


# ─────────────────────────────────────────────
# Backtester
# ─────────────────────────────────────────────

class Backtester:
    """
    Vectorized-style backtester that processes bar by bar.
    One position at a time per symbol for simplicity.
    """

    def __init__(self, config: BacktestConfig = None):
        self.cfg = config or BacktestConfig()

    def run(
        self,
        df: pd.DataFrame,
        strategy: BaseStrategy,
        symbol: str = "BTCUSD",
        warmup_bars: int = 50,
    ) -> "BacktestResult":
        """
        Run a single-pass backtest.

        :param df: OHLCV DataFrame, datetime-indexed, ascending.
        :param strategy: Instantiated strategy.
        :param symbol: Symbol name for logging.
        :param warmup_bars: Bars to skip before trading (let indicators stabilise).
        :return: BacktestResult with equity curve and metrics.
        """
        capital = self.cfg.initial_capital
        equity_curve = []
        trades: List[BacktestTrade] = []

        # Current open position state
        in_trade = False
        side = None
        entry_price = 0.0
        stop_loss = 0.0
        take_profit = 0.0
        entry_idx = 0
        entry_time = None
        size_usd = 0.0

        n = len(df)
        logger.info("Backtesting %s on %d bars with strategy=%s", symbol, n, strategy.name)

        for i in range(warmup_bars, n):
            bar = df.iloc[i]
            window = df.iloc[:i + 1]       # data available up to and including this bar
            ts = df.index[i]
            close = bar["close"]
            high = bar["high"]
            low = bar["low"]

            equity_curve.append({"time": ts, "equity": capital})

            if in_trade:
                # ── Check exits intra-bar ──────────────
                # Long stop-loss (low touched stop)
                if side == "long" and low <= stop_loss:
                    exit_price = self._fill_price(stop_loss, is_sl=True)
                    trade = self._close_trade(
                        entry_time, ts, symbol, side, entry_price, exit_price,
                        size_usd, i - entry_idx, "sl", capital
                    )
                    capital += trade.net_pnl
                    trades.append(trade)
                    in_trade = False

                # Long take-profit
                elif side == "long" and high >= take_profit:
                    exit_price = self._fill_price(take_profit, is_sl=False)
                    trade = self._close_trade(
                        entry_time, ts, symbol, side, entry_price, exit_price,
                        size_usd, i - entry_idx, "tp", capital
                    )
                    capital += trade.net_pnl
                    trades.append(trade)
                    in_trade = False

                # Short stop-loss
                elif side == "short" and high >= stop_loss:
                    exit_price = self._fill_price(stop_loss, is_sl=True)
                    trade = self._close_trade(
                        entry_time, ts, symbol, side, entry_price, exit_price,
                        size_usd, i - entry_idx, "sl", capital
                    )
                    capital += trade.net_pnl
                    trades.append(trade)
                    in_trade = False

                # Short take-profit
                elif side == "short" and low <= take_profit:
                    exit_price = self._fill_price(take_profit, is_sl=False)
                    trade = self._close_trade(
                        entry_time, ts, symbol, side, entry_price, exit_price,
                        size_usd, i - entry_idx, "tp", capital
                    )
                    capital += trade.net_pnl
                    trades.append(trade)
                    in_trade = False

                # Opposite signal closes position
                else:
                    signal = strategy.generate_signal(window, symbol)
                    if (side == "long" and signal.type == SignalType.SHORT) or \
                       (side == "short" and signal.type == SignalType.LONG):
                        exit_price = self._fill_price(close, is_sl=False)
                        trade = self._close_trade(
                            entry_time, ts, symbol, side, entry_price, exit_price,
                            size_usd, i - entry_idx, "signal", capital
                        )
                        capital += trade.net_pnl
                        trades.append(trade)
                        in_trade = False

            if not in_trade:
                # ── Look for entry signal ─────────────
                signal = strategy.generate_signal(window, symbol)

                if signal.type in (SignalType.LONG, SignalType.SHORT) and \
                   signal.stop_loss is not None:

                    stop_dist_pct = abs(close - signal.stop_loss) / close
                    if stop_dist_pct < 1e-6:
                        continue

                    risk_usd = capital * self.cfg.risk_per_trade * signal.confidence
                    size_usd = min(
                        risk_usd / stop_dist_pct * self.cfg.leverage,
                        capital * self.cfg.max_position_size_pct * self.cfg.leverage,
                    )

                    entry_price = self._fill_price(close, is_sl=False)
                    side = "long" if signal.type == SignalType.LONG else "short"
                    stop_loss = signal.stop_loss
                    take_profit = signal.take_profit or (
                        close + 2 * abs(close - signal.stop_loss) if side == "long"
                        else close - 2 * abs(signal.stop_loss - close)
                    )
                    entry_time = ts
                    entry_idx = i
                    in_trade = True

        # Close any remaining open position at last bar
        if in_trade:
            close = df["close"].iloc[-1]
            exit_price = self._fill_price(close, is_sl=False)
            trade = self._close_trade(
                entry_time, df.index[-1], symbol, side, entry_price, exit_price,
                size_usd, n - entry_idx, "eod", capital
            )
            capital += trade.net_pnl
            trades.append(trade)

        equity_df = pd.DataFrame(equity_curve).set_index("time")
        return BacktestResult(
            strategy_name=strategy.name,
            symbol=symbol,
            trades=trades,
            equity_curve=equity_df,
            initial_capital=self.cfg.initial_capital,
            final_capital=capital,
        )

    # ─────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────

    def _fill_price(self, price: float, is_sl: bool) -> float:
        """Apply slippage. SL fills are slightly worse."""
        slip = self.cfg.slippage_pct * (1.5 if is_sl else 1.0)
        return price * (1 + slip)

    def _close_trade(
        self, entry_time, exit_time, symbol, side,
        entry_price, exit_price, size_usd, holding_bars, reason, capital
    ) -> BacktestTrade:
        size_base = size_usd / entry_price
        if side == "long":
            gross_pnl = (exit_price - entry_price) * size_base
        else:
            gross_pnl = (entry_price - exit_price) * size_base
        fee = (entry_price + exit_price) * size_base * self.cfg.taker_fee
        net_pnl = gross_pnl - fee
        return BacktestTrade(
            entry_time=entry_time,
            exit_time=exit_time,
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            exit_price=exit_price,
            size_usd=size_usd,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            exit_reason=reason,
            holding_bars=holding_bars,
        )

    # ─────────────────────────────────────────
    # Grid-search parameter optimiser
    # ─────────────────────────────────────────

    def optimize(
        self,
        df: pd.DataFrame,
        strategy_class,
        param_grid: Dict[str, List],
        symbol: str = "BTCUSD",
        metric: str = "sharpe_ratio",
    ) -> Tuple[Dict, "BacktestResult"]:
        """
        Brute-force grid search over param_grid.
        WARNING: prone to overfitting — validate on out-of-sample data.

        :param param_grid: e.g. {"fast_ema": [5, 9, 13], "slow_ema": [21, 34]}
        :param metric: "sharpe_ratio" | "profit_factor" | "net_pnl" | "win_rate"
        :return: (best_params, best_result)
        """
        keys = list(param_grid.keys())
        values = list(param_grid.values())
        combos = list(iterproduct(*values))
        logger.info("Optimising %d parameter combinations…", len(combos))

        best_score = -np.inf
        best_params = {}
        best_result = None

        for combo in combos:
            params = dict(zip(keys, combo))
            strat = strategy_class(params)
            result = self.run(df, strat, symbol)
            score = getattr(result.metrics(), metric, -np.inf)
            if score > best_score:
                best_score = score
                best_params = params
                best_result = result

        logger.info("Best params: %s → %s=%.4f", best_params, metric, best_score)
        return best_params, best_result


# ─────────────────────────────────────────────
# Result + Metrics
# ─────────────────────────────────────────────

@dataclass
class BacktestResult:
    strategy_name: str
    symbol: str
    trades: List[BacktestTrade]
    equity_curve: pd.DataFrame
    initial_capital: float
    final_capital: float

    def metrics(self) -> "PerformanceMetrics":
        return PerformanceMetrics.compute(self)

    def summary(self) -> str:
        m = self.metrics()
        lines = [
            f"{'═'*50}",
            f"  Strategy : {self.strategy_name}  |  Symbol: {self.symbol}",
            f"{'─'*50}",
            f"  Trades          : {m.total_trades}",
            f"  Win Rate        : {m.win_rate:.1%}",
            f"  Net PnL         : ${m.net_pnl:,.2f}  ({m.return_pct:.2%})",
            f"  Profit Factor   : {m.profit_factor:.2f}",
            f"  Sharpe Ratio    : {m.sharpe_ratio:.2f}",
            f"  Sortino Ratio   : {m.sortino_ratio:.2f}",
            f"  Max Drawdown    : {m.max_drawdown:.2%}",
            f"  Avg Win         : ${m.avg_win:.2f}",
            f"  Avg Loss        : ${m.avg_loss:.2f}",
            f"  Avg Hold (bars) : {m.avg_hold_bars:.1f}",
            f"{'═'*50}",
        ]
        return "\n".join(lines)


@dataclass
class PerformanceMetrics:
    total_trades: int
    win_rate: float
    net_pnl: float
    return_pct: float
    profit_factor: float
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown: float
    avg_win: float
    avg_loss: float
    avg_hold_bars: float

    @staticmethod
    def compute(result: BacktestResult) -> "PerformanceMetrics":
        trades = result.trades
        if not trades:
            return PerformanceMetrics(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

        pnls = np.array([t.net_pnl for t in trades])
        wins = pnls[pnls > 0]
        losses = pnls[pnls <= 0]

        total_gross_profit = wins.sum() if len(wins) else 0
        total_gross_loss = abs(losses.sum()) if len(losses) else 1e-9

        profit_factor = total_gross_profit / total_gross_loss if total_gross_loss else np.inf

        # Sharpe (annualised, assuming 252 trading days × 24h, adjust to hourly)
        equity = result.equity_curve["equity"]
        returns = equity.pct_change().dropna()
        periods_per_year = 252 * 24       # for hourly bars; adjust as needed
        if returns.std() > 0:
            sharpe = (returns.mean() / returns.std()) * np.sqrt(periods_per_year)
        else:
            sharpe = 0.0

        # Sortino (downside deviation only)
        downside = returns[returns < 0]
        if downside.std() > 0:
            sortino = (returns.mean() / downside.std()) * np.sqrt(periods_per_year)
        else:
            sortino = 0.0

        # Max drawdown
        roll_max = equity.cummax()
        drawdown = (equity - roll_max) / roll_max
        max_dd = drawdown.min()

        return PerformanceMetrics(
            total_trades=len(trades),
            win_rate=len(wins) / len(trades),
            net_pnl=pnls.sum(),
            return_pct=pnls.sum() / result.initial_capital,
            profit_factor=profit_factor,
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            max_drawdown=abs(max_dd),
            avg_win=wins.mean() if len(wins) else 0,
            avg_loss=losses.mean() if len(losses) else 0,
            avg_hold_bars=np.mean([t.holding_bars for t in trades]),
        )
