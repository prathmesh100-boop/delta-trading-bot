"""
main.py — Entry point for Delta Exchange algorithmic trading system (v3)

Usage:
    python main.py trade --strategy smart_money --symbol ETH_USDT --capital 100 --leverage 10
    python main.py backtest --strategy ema_crossover --symbol BTCUSD
    python main.py optimize --strategy ema_crossover --symbol BTCUSD

REQUIRED — .env file:
    DELTA_API_KEY=your_key
    DELTA_API_SECRET=your_secret

OPTIONAL — .env:
    TELEGRAM_BOT_TOKEN=...
    TELEGRAM_CHAT_ID=...
    DASHBOARD_TOKEN=secret123
"""

from dotenv import load_dotenv
load_dotenv()

import argparse
import asyncio
import logging
import os
import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("trading_bot.log")],
)
logger = logging.getLogger("main")

from api import DeltaRESTClient
from backtest import Backtester, BacktestConfig
from execution import ExecutionEngine
from risk import RiskConfig, RiskManager
from strategy import EMACrossoverStrategy, BollingerMeanReversionStrategy, SmartMoneyStrategy, load_strategy


def load_env_keys():
    key = os.getenv("DELTA_API_KEY", "").strip()
    secret = os.getenv("DELTA_API_SECRET", "").strip()
    if not key or not secret:
        logger.warning("DELTA_API_KEY / DELTA_API_SECRET not set.")
    return key, secret


def generate_synthetic_ohlcv(n=2000, seed=42):
    rng = np.random.default_rng(seed)
    dt = 1 / (24 * 365)
    mu, sigma_base = 0.15, 0.80
    prices = [30_000.0]
    vols = [sigma_base]
    for _ in range(n - 1):
        v = 0.9 * vols[-1] + 0.1 * sigma_base + 0.05 * abs(rng.standard_normal())
        vols.append(v)
        ret = (mu - 0.5 * v**2) * dt + v * np.sqrt(dt) * rng.standard_normal()
        prices.append(prices[-1] * np.exp(ret))
    prices = np.array(prices)
    hi = prices * (1 + rng.uniform(0.001, 0.015, n))
    lo = prices * (1 - rng.uniform(0.001, 0.015, n))
    op = np.roll(prices, 1); op[0] = prices[0]
    vol = rng.lognormal(mean=10, sigma=1, size=n)
    idx = pd.date_range(start="2023-01-01", periods=n, freq="1h")
    return pd.DataFrame({"open": op, "high": hi, "low": lo, "close": prices, "volume": vol}, index=idx)


async def cmd_trade(args):
    key, secret = load_env_keys()
    if not key:
        print("ERROR: Set DELTA_API_KEY and DELTA_API_SECRET in .env")
        return

    strategy = load_strategy(args.strategy)
    risk_cfg = RiskConfig(
        risk_per_trade=0.02,
        max_drawdown_pct=0.15,
        max_open_trades=2,
        daily_loss_limit_pct=0.10,
        leverage=float(args.leverage),
        max_position_size_pct=0.30,
        breakeven_trigger_pct=float(getattr(args, "breakeven_trigger_pct", 0.005)),
        breakeven_buffer=float(getattr(args, "breakeven_buffer", 0.0)),
        profit_lock_threshold_pct=float(getattr(args, "profit_lock_threshold_pct", 0.01)),
        profit_lock_pct=float(getattr(args, "profit_lock_pct", 0.005)),
    )
    risk_mgr = RiskManager(risk_cfg, initial_capital=args.capital)

    async with DeltaRESTClient(key, secret) as rest:
        products = await rest.get_products()
        product = next((p for p in products if p.get("symbol") == args.symbol), None)
        if not product:
            print(f"\nSymbol '{args.symbol}' not found. Available USDT perpetuals:")
            for p in [p for p in products if "USDT" in p.get("symbol", "")][:20]:
                print(f"  {p['symbol']:20s}  contract_value={p.get('contract_value')}  min_size={p.get('min_size')}")
            return

        product_id = product["id"]
        print(f"\n{'='*60}")
        print(f"  Symbol      : {args.symbol}")
        print(f"  Product ID  : {product_id}")
        print(f"  Lot size    : {product.get('contract_value')} (contract_value)")
        print(f"  Min lots    : {product.get('min_size')}")
        print(f"  Capital     : ${args.capital:.2f}")
        print(f"  Leverage    : {args.leverage}x")
        print(f"  Strategy    : {args.strategy}")
        print(f"  Candles     : {args.resolution}m")
        print(f"{'='*60}\n")

        engine = ExecutionEngine(
            rest_client=rest,
            strategy=strategy,
            risk_manager=risk_mgr,
            symbol=args.symbol,
            product_id=product_id,
            resolution_minutes=args.resolution,
            api_key=key,
            api_secret=secret,
        )

        logger.info("Starting: %s | %s | capital=%.2f | leverage=%dx",
                    args.symbol, args.strategy, args.capital, args.leverage)
        await engine.run_polling(interval_seconds=args.resolution * 60)


def cmd_backtest(args):
    print(f"\n{'='*55}\n  Backtest: {args.strategy} on {args.symbol}\n{'='*55}")
    if args.data_file:
        df = pd.read_csv(args.data_file, index_col=0, parse_dates=True)
        df.columns = [c.lower() for c in df.columns]
        print(f"  Loaded {len(df)} bars from {args.data_file}")
    else:
        print("  Generating synthetic BTC data (2000 hourly bars)...")
        df = generate_synthetic_ohlcv(n=2000)

    strategy = load_strategy(args.strategy)
    config = BacktestConfig(initial_capital=args.capital, risk_per_trade=0.01,
                            taker_fee=0.0005, slippage_pct=0.0003)
    result = Backtester(config).run(df, strategy, symbol=args.symbol)
    print(result.summary())
    print("\n  Per-trade breakdown (last 10 trades):")
    for t in result.trades[-10:]:
        pnl_str = f"+${t.net_pnl:.2f}" if t.net_pnl >= 0 else f"-${abs(t.net_pnl):.2f}"
        print(f"  {t.entry_time.date()} {t.side:5s} -> {t.exit_reason:6s}  {pnl_str:>10}")
    result.equity_curve.to_csv("equity_curve.csv")
    print(f"\n  Equity curve saved to equity_curve.csv")


def cmd_optimize(args):
    print(f"\n{'='*55}\n  Optimising {args.strategy} on {args.symbol}\n{'='*55}\n")
    df = generate_synthetic_ohlcv(n=3000)
    split = int(len(df) * 0.7)
    train_df, test_df = df.iloc[:split], df.iloc[split:]

    if args.strategy == "ema_crossover":
        cls = EMACrossoverStrategy
        grid = {"fast_ema": [5, 9, 13], "slow_ema": [21, 34, 55], "atr_sl_multiplier": [1.2, 1.5, 2.0]}
    elif args.strategy == "smart_money":
        cls = SmartMoneyStrategy
        grid = {"fast_ema": [3, 5, 8], "slow_ema": [15, 20, 25], "atr_sl_multiplier": [1.0, 1.2, 1.5]}
    else:
        cls = BollingerMeanReversionStrategy
        grid = {"bb_period": [15, 20, 25], "bb_std": [1.5, 2.0, 2.5], "rsi_oversold": [30, 35, 40]}

    bt = Backtester(BacktestConfig(initial_capital=args.capital))
    best_params, train_result = bt.optimize(train_df, cls, grid, symbol=args.symbol, metric="sharpe_ratio")
    print("TRAIN SET:")
    print(train_result.summary())
    print("\nTEST SET (out-of-sample):")
    print(bt.run(test_df, cls(best_params), symbol=args.symbol).summary())
    print(f"\nBest params: {best_params}")


def build_parser():
    parser = argparse.ArgumentParser(description="Delta Exchange Trading System v3")
    sub = parser.add_subparsers(dest="command", required=True)

    pt = sub.add_parser("trade")
    pt.add_argument("--strategy", default="smart_money", choices=["ema_crossover", "bollinger_mean_reversion", "smart_money"])
    pt.add_argument("--symbol", default="ETH_USDT")
    pt.add_argument("--capital", type=float, default=100.0)
    pt.add_argument("--leverage", type=int, default=10)
    pt.add_argument("--resolution", type=int, default=15)
    pt.add_argument("--breakeven-trigger-pct", type=float, default=0.005)
    pt.add_argument("--breakeven-buffer", type=float, default=0.0)
    pt.add_argument("--profit-lock-threshold-pct", type=float, default=0.01)
    pt.add_argument("--profit-lock-pct", type=float, default=0.005)

    pb = sub.add_parser("backtest")
    pb.add_argument("--strategy", default="ema_crossover", choices=["ema_crossover", "bollinger_mean_reversion", "smart_money"])
    pb.add_argument("--symbol", default="BTCUSD")
    pb.add_argument("--capital", type=float, default=10_000.0)
    pb.add_argument("--data-file", default=None)

    po = sub.add_parser("optimize")
    po.add_argument("--strategy", default="ema_crossover", choices=["ema_crossover", "bollinger_mean_reversion", "smart_money"])
    po.add_argument("--symbol", default="BTCUSD")
    po.add_argument("--capital", type=float, default=10_000.0)

    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()
    if args.command == "trade":
        asyncio.run(cmd_trade(args))
    elif args.command == "backtest":
        cmd_backtest(args)
    elif args.command == "optimize":
        cmd_optimize(args)
