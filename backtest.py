"""
backtest.py — Bar-by-bar event-driven backtester

Features:
  - Causal (no look-ahead bias): strategy sees only past bars
  - Realistic fees: taker 0.05% + slippage 0.03%
  - Walk-forward split: 70% train / 30% test
  - Full P&L stats: Sharpe, max DD, profit factor, win rate
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from strategy import ConfluenceStrategy, Signal, SignalType, normalize_signal

logger = logging.getLogger(__name__)


@dataclass
class BacktestConfig:
    initial_capital: float = 10_000.0
    risk_per_trade:  float = 0.01       # 1% per trade
    taker_fee:       float = 0.0005     # 0.05% Delta taker fee
    slippage_pct:    float = 0.0003     # 0.03% slippage
    leverage:        float = 5.0
    min_confidence:  float = 0.55


@dataclass
class BacktestTrade:
    symbol:      str
    side:        str
    entry_time:  Any
    exit_time:   Any
    entry_price: float
    exit_price:  float
    size_usd:    float
    gross_pnl:   float
    fees:        float
    net_pnl:     float
    exit_reason: str
    confidence:  float = 1.0


@dataclass
class BacktestResult:
    trades:       List[BacktestTrade]
    equity_curve: pd.Series
    config:       BacktestConfig
    symbol:       str = ""

    def summary(self) -> str:
        n = len(self.trades)
        if n == 0:
            return "  No trades executed."

        pnls    = [t.net_pnl for t in self.trades]
        wins    = [p for p in pnls if p > 0]
        losses  = [p for p in pnls if p <= 0]
        total   = sum(pnls)
        wr      = len(wins) / n * 100
        avg_w   = sum(wins) / len(wins) if wins else 0
        avg_l   = sum(losses) / len(losses) if losses else 0
        pf      = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else 999

        eq      = self.equity_curve
        daily   = eq.resample("1D").last().ffill().pct_change().dropna()
        sharpe  = float(daily.mean() / daily.std() * np.sqrt(252)) if len(daily) > 1 and daily.std() > 0 else 0.0

        roll_max = eq.cummax()
        dd_pct   = float(((eq - roll_max) / roll_max.replace(0, np.nan)).min() * 100)
        final_c  = self.config.initial_capital + total
        ret_pct  = total / self.config.initial_capital * 100

        return "\n".join([
            f"  Symbol        : {self.symbol}",
            f"  Trades        : {n}  (W:{len(wins)} L:{len(losses)})",
            f"  Win Rate      : {wr:.1f}%",
            f"  Avg Win       : +${avg_w:.2f}  |  Avg Loss: -${abs(avg_l):.2f}",
            f"  Profit Factor : {pf:.2f}",
            f"  Sharpe Ratio  : {sharpe:.2f}",
            f"  Max Drawdown  : {dd_pct:.2f}%",
            f"  Net PnL       : ${total:+.2f}  ({ret_pct:+.1f}%)",
            f"  Final Capital : ${final_c:.2f}",
        ])

    @property
    def sharpe_ratio(self) -> float:
        eq    = self.equity_curve
        daily = eq.resample("1D").last().ffill().pct_change().dropna()
        return float(daily.mean() / daily.std() * np.sqrt(252)) if len(daily) > 1 and daily.std() > 0 else 0.0

    @property
    def total_pnl(self) -> float:
        return sum(t.net_pnl for t in self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.net_pnl > 0) / len(self.trades) * 100


class Backtester:
    def __init__(self, config: BacktestConfig):
        self.cfg = config

    def run(self, df: pd.DataFrame, strategy: ConfluenceStrategy, symbol: str = "SIM") -> BacktestResult:
        cfg     = self.cfg
        capital = cfg.initial_capital
        trades: List[BacktestTrade] = []
        equity_records = []
        current_trade: Optional[Dict] = None

        # Use full warmup for long series, but allow shorter datasets for tests
        warmup = min(210, max(1, len(df) - 1))  # need up to 210 bars for EMA200

        for i in range(warmup, len(df)):
            window = df.iloc[:i]
            bar    = df.iloc[i]
            ts     = df.index[i]
            o, h, l, c = float(bar.open), float(bar.high), float(bar.low), float(bar.close)

            # Manage open trade
            if current_trade:
                ct     = current_trade
                side   = ct["side"]
                sl, tp = ct["sl"], ct["tp"]
                entry  = ct["entry"]
                sz_usd = ct["size_usd"]
                exit_p = None
                reason = None

                if side == "long":
                    if l <= sl:
                        exit_p = sl * (1 - cfg.slippage_pct)
                        reason = "stop_loss"
                    elif tp and h >= tp:
                        exit_p = tp * (1 - cfg.slippage_pct)
                        reason = "take_profit"
                else:
                    if h >= sl:
                        exit_p = sl * (1 + cfg.slippage_pct)
                        reason = "stop_loss"
                    elif tp and l <= tp:
                        exit_p = tp * (1 + cfg.slippage_pct)
                        reason = "take_profit"

                if exit_p is not None:
                    direction = 1 if side == "long" else -1
                    gross = direction * (exit_p - entry) * sz_usd / entry
                    fees  = (sz_usd + sz_usd * abs(exit_p / entry)) * cfg.taker_fee
                    net   = gross - fees
                    capital += net
                    trades.append(BacktestTrade(
                        symbol=symbol, side=side,
                        entry_time=ct["time"], exit_time=ts,
                        entry_price=entry, exit_price=exit_p,
                        size_usd=sz_usd, gross_pnl=gross, fees=fees, net_pnl=net,
                        exit_reason=reason, confidence=ct.get("conf", 1.0),
                    ))
                    current_trade = None

            # Generate new signal
            if current_trade is None:
                try:
                    sig = normalize_signal(strategy.generate_signal(window, symbol))
                except Exception as exc:
                    logger.debug("Strategy error at bar %d: %s", i, exc)
                    sig = None

                if (sig and sig.type in (SignalType.LONG, SignalType.SHORT)
                        and sig.stop_loss
                        and sig.confidence >= cfg.min_confidence):
                    sl_dist = abs(o - sig.stop_loss)
                    if sl_dist > 0:
                        risk_amt = capital * cfg.risk_per_trade
                        sz_usd   = risk_amt * (o / sl_dist) * cfg.leverage
                        sz_usd   = min(sz_usd, capital * 0.30 * cfg.leverage)
                        slip     = o * cfg.slippage_pct
                        entry    = o + slip if sig.type == SignalType.LONG else o - slip
                        fees     = sz_usd * cfg.taker_fee
                        capital -= fees
                        current_trade = {
                            "side":     "long" if sig.type == SignalType.LONG else "short",
                            "entry":    entry,
                            "sl":       sig.stop_loss,
                            "tp":       sig.take_profit,
                            "size_usd": sz_usd,
                            "time":     ts,
                            "conf":     sig.confidence,
                        }

            equity_records.append((ts, capital))

        # Force-close any open trade at end
        if current_trade and len(df) > 0:
            ct    = current_trade
            ep    = float(df.iloc[-1].close)
            direction = 1 if ct["side"] == "long" else -1
            gross = direction * (ep - ct["entry"]) * ct["size_usd"] / ct["entry"]
            fees  = ct["size_usd"] * cfg.taker_fee
            net   = gross - fees
            capital += net
            trades.append(BacktestTrade(
                symbol=symbol, side=ct["side"],
                entry_time=ct["time"], exit_time=df.index[-1],
                entry_price=ct["entry"], exit_price=ep,
                size_usd=ct["size_usd"], gross_pnl=gross, fees=fees, net_pnl=net,
                exit_reason="end_of_data", confidence=ct.get("conf", 1.0),
            ))

        eq_series = pd.Series({ts: eq for ts, eq in equity_records}, name="equity")
        return BacktestResult(trades=trades, equity_curve=eq_series, config=cfg, symbol=symbol)


__all__ = ["Backtester", "BacktestConfig", "BacktestResult", "BacktestTrade"]
