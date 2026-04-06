"""
main.py — Delta Exchange Algorithmic Trading System v6

Usage:
    python main.py trade   --strategy smart_money --symbol ETH_USDT --capital 500 --leverage 10
    python main.py backtest --strategy ema_crossover --symbol BTCUSD --capital 10000
    python main.py optimize --strategy smart_money --symbol ETH_USDT
    python main.py info     --symbol BTC_USDT
    python main.py status

REQUIRED — .env file:
    DELTA_API_KEY=your_key
    DELTA_API_SECRET=your_secret

OPTIONAL:
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
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("trading_bot.log"),
    ],
)
logger = logging.getLogger("main")

from api import DeltaRESTClient
from backtest import Backtester, BacktestConfig
from execution import ExecutionEngine
from risk import RiskConfig, RiskManager
from strategy import load_strategy, STRATEGY_MAP


def load_env_keys():
    key    = os.getenv("DELTA_API_KEY", "").strip()
    secret = os.getenv("DELTA_API_SECRET", "").strip()
    if not key or not secret:
        logger.warning("⚠️ DELTA_API_KEY / DELTA_API_SECRET not set in .env")
    return key, secret


def generate_synthetic_ohlcv(n: int = 2000, seed: int = 42) -> pd.DataFrame:
    """Generate realistic synthetic OHLCV for backtesting demos."""
    rng = np.random.default_rng(seed)
    dt  = 1 / (24 * 365)
    mu, sigma_base = 0.15, 0.80
    prices, vols = [30_000.0], [sigma_base]

    for _ in range(n - 1):
        v = 0.9 * vols[-1] + 0.1 * sigma_base + 0.05 * abs(rng.standard_normal())
        vols.append(v)
        ret = (mu - 0.5 * v**2) * dt + v * np.sqrt(dt) * rng.standard_normal()
        prices.append(prices[-1] * np.exp(ret))

    prices = np.array(prices)
    hi  = prices * (1 + rng.uniform(0.001, 0.015, n))
    lo  = prices * (1 - rng.uniform(0.001, 0.015, n))
    op  = np.roll(prices, 1); op[0] = prices[0]
    vol = rng.lognormal(mean=10, sigma=1, size=n)
    idx = pd.date_range(start="2023-01-01", periods=n, freq="1h")
    return pd.DataFrame(
        {"open": op, "high": hi, "low": lo, "close": prices, "volume": vol},
        index=idx
    )


# ─────────────────────────────────────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_trade(args):
    key, secret = load_env_keys()
    if not key:
        print("❌ ERROR: Set DELTA_API_KEY and DELTA_API_SECRET in .env")
        return

    ai_filter = getattr(args, "ai_filter", False)
    strategy  = load_strategy(args.strategy, ai_filter=ai_filter)

    risk_cfg = RiskConfig(
        risk_per_trade         = getattr(args, "risk_per_trade", 0.01),
        max_drawdown_pct       = 0.15,
        max_open_trades        = getattr(args, "max_trades", 2),
        daily_loss_limit_pct   = 0.10,
        leverage               = float(args.leverage),
        max_position_size_pct  = 0.30,
        breakeven_trigger_pct  = getattr(args, "breakeven_trigger_pct", 0.003),
        breakeven_buffer       = getattr(args, "breakeven_buffer", 0.0002),
        profit_lock_threshold_pct = getattr(args, "profit_lock_threshold_pct", 0.005),
        profit_lock_pct        = getattr(args, "profit_lock_pct", 0.003),
    )
    risk_mgr = RiskManager(risk_cfg, initial_capital=args.capital)

    async with DeltaRESTClient(key, secret) as rest:
        # Resolve product ID from symbol
        products = await rest.get_products()
        product  = next((p for p in products if p.get("symbol") == args.symbol), None)

        if not product:
            print(f"\n❌ Symbol '{args.symbol}' not found.")
            print("Available USDT perpetuals:")
            usdt = [p for p in products if "USDT" in p.get("symbol", "")][:20]
            for p in usdt:
                print(f"  {p['symbol']:20s}  contract_value={p.get('contract_value')}  min_size={p.get('min_size')}")
            return

        product_id = product["id"]

        print(f"\n{'='*60}")
        print(f"  Symbol       : {args.symbol}")
        print(f"  Product ID   : {product_id}")
        print(f"  Contract Val : {product.get('contract_value')}")
        print(f"  Min Lots     : {product.get('min_size')}")
        print(f"  Capital      : ${args.capital:.2f}")
        print(f"  Leverage     : {args.leverage}x")
        print(f"  Strategy     : {args.strategy}" + (" [AI-filtered]" if ai_filter else ""))
        print(f"  Candle Res   : {args.resolution}m")
        print(f"  OB Filter    : {getattr(args, 'ob_filter', 0.0)}")
        print(f"{'='*60}\n")

        engine = ExecutionEngine(
            rest_client       = rest,
            strategy          = strategy,
            risk_manager      = risk_mgr,
            symbol            = args.symbol,
            product_id        = product_id,
            resolution_minutes= args.resolution,
            api_key           = key,
            api_secret        = secret,
            ob_imbalance_min  = getattr(args, "ob_filter", 0.0),
            confidence_min    = getattr(args, "confidence_min", 0.0),
            trailing_enabled  = not getattr(args, "no_trailing", False),
        )

        logger.info("Starting: %s | %s | capital=%.2f | leverage=%dx",
                    args.symbol, args.strategy, args.capital, args.leverage)
        await engine.run_polling(interval_seconds=args.resolution * 60)


def cmd_backtest(args):
    print(f"\n{'='*55}")
    print(f"  Backtest: {args.strategy} on {args.symbol}")
    print(f"{'='*55}")

    if getattr(args, "data_file", None):
        df = pd.read_csv(args.data_file, index_col=0, parse_dates=True)
        df.columns = [c.lower() for c in df.columns]
        print(f"  Loaded {len(df)} bars from {args.data_file}")
    else:
        print("  Generating synthetic data (2000 hourly bars)…")
        df = generate_synthetic_ohlcv(n=2000)

    strategy = load_strategy(args.strategy)
    config   = BacktestConfig(
        initial_capital = args.capital,
        risk_per_trade  = 0.01,
        taker_fee       = 0.0005,
        slippage_pct    = 0.0003,
        leverage        = float(getattr(args, "leverage", 1.0)),
    )
    result = Backtester(config).run(df, strategy, symbol=args.symbol)
    print(result.summary())

    if result.trades:
        print("\n  Last 10 trades:")
        for t in result.trades[-10:]:
            pnl_str = f"+${t.net_pnl:.2f}" if t.net_pnl >= 0 else f"-${abs(t.net_pnl):.2f}"
            print(f"  {str(t.entry_time)[:10]} {t.side:5s} → {t.exit_reason:12s}  {pnl_str:>10}")

    result.equity_curve.to_csv("equity_curve.csv")
    print(f"\n  Equity curve → equity_curve.csv")


def cmd_optimize(args):
    print(f"\n{'='*55}")
    print(f"  Optimising {args.strategy} on {args.symbol}")
    print(f"{'='*55}\n")

    df    = generate_synthetic_ohlcv(n=3000)
    split = int(len(df) * 0.70)
    train_df, test_df = df.iloc[:split], df.iloc[split:]

    from strategy import (
        EMACrossoverStrategy, BollingerMeanReversionStrategy,
        SmartMoneyStrategy, BreakoutStrategy,
    )
    GRIDS = {
        "ema_crossover":   {"fast_ema": [5, 9, 13], "slow_ema": [21, 34, 55], "atr_sl_multiplier": [1.2, 1.5, 2.0]},
        "bollinger_mean_reversion": {"bb_period": [15, 20, 25], "bb_std": [1.5, 2.0, 2.5], "rsi_oversold": [25, 30, 35]},
        "smart_money":     {"fast_ema": [5, 8, 13], "slow_ema": [15, 21, 30], "atr_sl_multiplier": [1.0, 1.2, 1.5]},
        "breakout":        {"lookback": [10, 20, 30], "vol_factor": [1.2, 1.5, 2.0], "rr": [1.5, 2.0, 2.5]},
    }
    CLASSES = {
        "ema_crossover":            EMACrossoverStrategy,
        "bollinger_mean_reversion": BollingerMeanReversionStrategy,
        "smart_money":              SmartMoneyStrategy,
        "breakout":                 BreakoutStrategy,
    }

    cls  = CLASSES.get(args.strategy, EMACrossoverStrategy)
    grid = GRIDS.get(args.strategy, GRIDS["ema_crossover"])

    bt = Backtester(BacktestConfig(initial_capital=args.capital))
    best_params, train_result = bt.optimize(train_df, cls, grid, symbol=args.symbol, metric="sharpe_ratio")

    print("TRAIN SET:")
    print(train_result.summary())
    test_result = bt.run(test_df, cls(best_params), symbol=args.symbol)
    print("\nTEST SET (out-of-sample):")
    print(test_result.summary())
    print(f"\nBest params: {best_params}")


async def cmd_info(args):
    """Show product info and current ticker."""
    key, secret = load_env_keys()
    async with DeltaRESTClient(key, secret) as rest:
        product = await rest.get_product(args.symbol)
        ticker  = await rest.get_ticker(args.symbol)
        ob      = await rest.get_orderbook(args.symbol, depth=5)

        if not product:
            print(f"Symbol {args.symbol} not found")
            return

        print(f"\n{'='*55}")
        print(f"  {args.symbol} Info")
        print(f"{'='*55}")
        print(f"  Product ID   : {product.get('id')}")
        print(f"  Contract Val : {product.get('contract_value')}")
        print(f"  Min Size     : {product.get('min_size')}")
        print(f"  Last Price   : {ticker.last_price:.4f}")
        print(f"  Mark Price   : {ticker.mark_price:.4f}")
        print(f"  Bid          : {ticker.bid:.4f}")
        print(f"  Ask          : {ticker.ask:.4f}")
        print(f"  Funding Rate : {ticker.funding_rate:.6f}")
        print(f"  OI (USD)     : {ticker.open_interest:,.0f}")
        print(f"  OB Imbalance : {ob.imbalance():.3f} (>0=bullish)")
        print(f"  Spread       : {ob.spread():.4f}")


async def cmd_status(args):
    """Show open positions and wallet balance."""
    key, secret = load_env_keys()
    if not key:
        print("❌ API keys not set")
        return

    async with DeltaRESTClient(key, secret) as rest:
        balance   = await rest.get_wallet_balance("USDT")
        positions = await rest.get_positions()
        orders    = await rest.get_open_orders()
        quota     = await rest.get_rate_limit_quota()

        print(f"\n{'='*55}")
        print(f"  Account Status")
        print(f"{'='*55}")
        print(f"  USDT Balance : ${balance:.4f}")
        print(f"\n  Open Positions ({len(positions)}):")
        if positions:
            for p in positions:
                pnl_str = f"+{p.unrealized_pnl:.4f}" if p.unrealized_pnl >= 0 else f"{p.unrealized_pnl:.4f}"
                print(f"    {p.symbol:15s} {p.side:5s} size={p.size} entry={p.entry_price:.4f} pnl={pnl_str}")
        else:
            print("    None")

        print(f"\n  Open Orders   ({len(orders)}):")
        if orders:
            for o in orders[:10]:
                print(f"    id={o.get('id')} {o.get('side')} {o.get('size')} @ {o.get('limit_price') or 'MKT'} [{o.get('state')}]")
        else:
            print("    None")

        if quota:
            print(f"\n  Rate Limit Quota: {quota}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

STRATEGY_CHOICES = list(STRATEGY_MAP.keys())


def build_parser():
    parser = argparse.ArgumentParser(
        description="Delta Exchange Algo Trading System v6",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── trade ─────────────────────────────────────────────────────────────
    pt = sub.add_parser("trade", help="Live trading")
    pt.add_argument("--strategy",    default="smart_money", choices=STRATEGY_CHOICES)
    pt.add_argument("--symbol",      default="ETH_USDT")
    pt.add_argument("--capital",     type=float, default=100.0, help="Starting equity in USDT")
    pt.add_argument("--leverage",    type=int,   default=10)
    pt.add_argument("--resolution",  type=int,   default=15,   help="Candle interval in minutes")
    pt.add_argument("--risk-per-trade",  type=float, default=0.01,  dest="risk_per_trade")
    pt.add_argument("--max-trades",  type=int,   default=2,    dest="max_trades")
    pt.add_argument("--ob-filter",   type=float, default=0.0,  dest="ob_filter",
                    help="Min orderbook imbalance (0=disabled)")
    pt.add_argument("--confidence",  type=float, default=0.0,  dest="confidence_min",
                    help="Min signal confidence (0=disabled)")
    pt.add_argument("--ai-filter",   action="store_true",      dest="ai_filter",
                    help="Enable AI/multi-factor signal filter")
    pt.add_argument("--no-trailing", action="store_true",      dest="no_trailing",
                    help="Disable trailing stop updates")
    pt.add_argument("--breakeven-trigger-pct",    type=float, default=0.003)
    pt.add_argument("--breakeven-buffer",         type=float, default=0.0002)
    pt.add_argument("--profit-lock-threshold-pct",type=float, default=0.005)
    pt.add_argument("--profit-lock-pct",          type=float, default=0.003)

    # ── backtest ───────────────────────────────────────────────────────────
    pb = sub.add_parser("backtest", help="Run backtest")
    pb.add_argument("--strategy",   default="ema_crossover", choices=STRATEGY_CHOICES)
    pb.add_argument("--symbol",     default="BTCUSD")
    pb.add_argument("--capital",    type=float, default=10_000.0)
    pb.add_argument("--leverage",   type=float, default=1.0)
    pb.add_argument("--data-file",  default=None,    dest="data_file")

    # ── optimize ───────────────────────────────────────────────────────────
    po = sub.add_parser("optimize", help="Grid-search strategy parameters")
    po.add_argument("--strategy",   default="ema_crossover", choices=STRATEGY_CHOICES)
    po.add_argument("--symbol",     default="BTCUSD")
    po.add_argument("--capital",    type=float, default=10_000.0)

    # ── info ───────────────────────────────────────────────────────────────
    pi = sub.add_parser("info", help="Show product + ticker info")
    pi.add_argument("--symbol", default="BTC_USDT")

    # ── status ─────────────────────────────────────────────────────────────
    sub.add_parser("status", help="Show positions, orders, wallet")

    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()
    if args.command == "trade":
        asyncio.run(cmd_trade(args))
    elif args.command == "backtest":
        cmd_backtest(args)
    elif args.command == "optimize":
        cmd_optimize(args)
    elif args.command == "info":
        asyncio.run(cmd_info(args))
    elif args.command == "status":
        asyncio.run(cmd_status(args))
