"""
main.py — Entry point for the Delta Exchange algorithmic trading system.

Usage:
    # Live trading
    python main.py trade --strategy ema_crossover --symbol BTCUSD --capital 10000

    # Backtest with sample data
    python main.py backtest --strategy bollinger_mean_reversion --symbol BTCUSD

    # Parameter optimisation
    python main.py optimize --strategy ema_crossover --symbol BTCUSD
"""

import argparse
import asyncio
import logging
import os
import time
from datetime import datetime

import numpy as np
import pandas as pd

# ── Logging setup ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("trading_bot.log"),
    ],
)
logger = logging.getLogger("main")


# ── Local imports ─────────────────────────────────────────────────────────────
from api import DeltaRESTClient
from backtest import Backtester, BacktestConfig
from execution import ExecutionEngine
from risk import RiskConfig, RiskManager
from strategy import EMACrossoverStrategy, BollingerMeanReversionStrategy, load_strategy


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def load_env_keys() -> tuple:
    """Load API credentials from environment variables."""
    api_key = os.getenv("DELTA_API_KEY", "")
    api_secret = os.getenv("DELTA_API_SECRET", "")
    if not api_key or not api_secret:
        logger.warning(
            "DELTA_API_KEY / DELTA_API_SECRET not set. "
            "Live trading will fail — fine for backtesting."
        )
    return api_key, api_secret


def generate_synthetic_ohlcv(n: int = 2000, seed: int = 42) -> pd.DataFrame:
    """
    Generate realistic-looking synthetic BTC/USD price data for demo purposes.
    Uses geometric Brownian motion + volatility clustering.
    """
    rng = np.random.default_rng(seed)
    dt = 1 / (24 * 365)                  # 1 hour expressed as fraction of year
    mu = 0.15                            # 15% annualised drift
    sigma_base = 0.80                    # 80% annualised volatility (crypto)

    prices = [30_000.0]
    vols = [sigma_base]
    for _ in range(n - 1):
        # GARCH-lite: vol reverts to mean with some randomness
        v = 0.9 * vols[-1] + 0.1 * sigma_base + 0.05 * abs(rng.standard_normal())
        vols.append(v)
        ret = (mu - 0.5 * v ** 2) * dt + v * np.sqrt(dt) * rng.standard_normal()
        prices.append(prices[-1] * np.exp(ret))

    prices = np.array(prices)
    # Build OHLCV from close prices
    hi = prices * (1 + rng.uniform(0.001, 0.015, n))
    lo = prices * (1 - rng.uniform(0.001, 0.015, n))
    op = np.roll(prices, 1)
    op[0] = prices[0]
    vol = rng.lognormal(mean=10, sigma=1, size=n)

    idx = pd.date_range(start="2023-01-01", periods=n, freq="1h")
    return pd.DataFrame(
        {"open": op, "high": hi, "low": lo, "close": prices, "volume": vol},
        index=idx,
    )


# ─────────────────────────────────────────────
# CLI Commands
# ─────────────────────────────────────────────

async def cmd_trade(args):
    """Start live trading."""
    api_key, api_secret = load_env_keys()
    if not api_key:
        print("ERROR: Set DELTA_API_KEY and DELTA_API_SECRET environment variables.")
        return

    strategy = load_strategy(args.strategy)
    risk_cfg = RiskConfig(
        risk_per_trade=0.01,
        max_drawdown_pct=0.10,
        daily_loss_limit_pct=0.03,
    )
    risk_mgr = RiskManager(risk_cfg, initial_capital=args.capital)

    async with DeltaRESTClient(api_key, api_secret) as rest:
        # Resolve product_id from symbol
        products = await rest.get_products()
        product = next((p for p in products if p.get("symbol") == args.symbol), None)
        if not product:
            print(f"Symbol {args.symbol} not found. Available:")
            for p in products[:10]:
                print(f"  {p['symbol']} (id={p['id']})")
            return
        product_id = product["id"]

        engine = ExecutionEngine(
            rest_client=rest,
            strategy=strategy,
            risk_manager=risk_mgr,
            symbol=args.symbol,
            product_id=product_id,
            resolution_minutes=args.resolution,
            api_key=api_key,
            api_secret=api_secret,
        )
        logger.info(
            "🚀 Starting live trading: %s | strategy=%s | capital=%.2f",
            args.symbol, args.strategy, args.capital,
        )
        await engine.run_polling(interval_seconds=args.resolution * 60)


def cmd_backtest(args):
    """Run backtest on synthetic or loaded data."""
    print(f"\n{'═'*55}")
    print(f"  Backtest: {args.strategy} on {args.symbol}")
    print(f"{'═'*55}")

    if args.data_file:
        df = pd.read_csv(args.data_file, index_col=0, parse_dates=True)
        df.columns = [c.lower() for c in df.columns]
        print(f"  Loaded {len(df)} bars from {args.data_file}")
    else:
        print("  Using synthetic BTC price data (2 000 hourly bars)…")
        df = generate_synthetic_ohlcv(n=2000)

    strategy = load_strategy(args.strategy)
    config = BacktestConfig(
        initial_capital=args.capital,
        risk_per_trade=0.01,
        taker_fee=0.0005,
        slippage_pct=0.0003,
    )
    backtester = Backtester(config)
    result = backtester.run(df, strategy, symbol=args.symbol)

    print(result.summary())
    print("\n  Per-trade breakdown (last 10 trades):")
    for t in result.trades[-10:]:
        pnl_str = f"+${t.net_pnl:.2f}" if t.net_pnl >= 0 else f"-${abs(t.net_pnl):.2f}"
        print(f"  {t.entry_time.date()} {t.side:5s} → {t.exit_reason:6s}  {pnl_str:>10}")

    # Save equity curve
    result.equity_curve.to_csv("equity_curve.csv")
    print(f"\n  Equity curve saved to equity_curve.csv")


def cmd_optimize(args):
    """Grid-search parameter optimisation."""
    print(f"\n{'═'*55}")
    print(f"  Optimising {args.strategy} on {args.symbol}")
    print(f"  WARNING: Validate results on out-of-sample data!")
    print(f"{'═'*55}\n")

    df = generate_synthetic_ohlcv(n=3000)
    # 70/30 split: train on first 70%
    split = int(len(df) * 0.7)
    train_df = df.iloc[:split]
    test_df = df.iloc[split:]

    if args.strategy == "ema_crossover":
        strategy_class = EMACrossoverStrategy
        param_grid = {
            "fast_ema": [5, 9, 13],
            "slow_ema": [21, 34, 55],
            "rsi_long_min": [45, 50, 55],
            "atr_sl_multiplier": [1.2, 1.5, 2.0],
        }
    else:
        strategy_class = BollingerMeanReversionStrategy
        param_grid = {
            "bb_period": [15, 20, 25],
            "bb_std": [1.5, 2.0, 2.5],
            "rsi_oversold": [30, 35, 40],
            "atr_sl_multiplier": [1.2, 1.5, 2.0],
        }

    backtester = Backtester(BacktestConfig(initial_capital=args.capital))
    best_params, train_result = backtester.optimize(
        train_df, strategy_class, param_grid, symbol=args.symbol, metric="sharpe_ratio"
    )

    print("  TRAIN SET results:")
    print(train_result.summary())

    # Validate on held-out data
    best_strat = strategy_class(best_params)
    test_result = backtester.run(test_df, best_strat, symbol=args.symbol)
    print("\n  TEST SET (out-of-sample) results:")
    print(test_result.summary())
    print(f"\n  Best parameters: {best_params}")


# ─────────────────────────────────────────────
# Argument parser
# ─────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Delta Exchange Algorithmic Trading System"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # trade
    p_trade = sub.add_parser("trade", help="Run live trading bot")
    p_trade.add_argument("--strategy", default="ema_crossover",
                         choices=["ema_crossover", "bollinger_mean_reversion"])
    p_trade.add_argument("--symbol", default="BTCUSD")
    p_trade.add_argument("--capital", type=float, default=1000.0)
    p_trade.add_argument("--resolution", type=int, default=15,
                         help="Candle resolution in minutes")

    # backtest
    p_bt = sub.add_parser("backtest", help="Backtest a strategy")
    p_bt.add_argument("--strategy", default="ema_crossover",
                      choices=["ema_crossover", "bollinger_mean_reversion"])
    p_bt.add_argument("--symbol", default="BTCUSD")
    p_bt.add_argument("--capital", type=float, default=10_000.0)
    p_bt.add_argument("--data-file", default=None, help="Path to CSV OHLCV data")

    # optimize
    p_opt = sub.add_parser("optimize", help="Grid-search parameter optimisation")
    p_opt.add_argument("--strategy", default="ema_crossover",
                       choices=["ema_crossover", "bollinger_mean_reversion"])
    p_opt.add_argument("--symbol", default="BTCUSD")
    p_opt.add_argument("--capital", type=float, default=10_000.0)

    return parser


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "trade":
        asyncio.run(cmd_trade(args))
    elif args.command == "backtest":
        cmd_backtest(args)
    elif args.command == "optimize":
        cmd_optimize(args)
