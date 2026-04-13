"""
main.py — Delta Exchange Algo Trading Bot
Institutional Confluence Strategy: MTF Trend + Structure + Momentum

Usage:
    # Live trading
    python main.py trade --symbol ETH_USDT --capital 500 --leverage 5

    # Backtest on synthetic data
    python main.py backtest --symbol ETH_USDT

    # Check account status
    python main.py status

    # Check product info
    python main.py info --symbol ETH_USDT

REQUIRED (.env file):
    DELTA_API_KEY=your_key
    DELTA_API_SECRET=your_secret

OPTIONAL (.env file):
    TELEGRAM_BOT_TOKEN=your_token
    TELEGRAM_CHAT_ID=your_chat_id
"""

from dotenv import load_dotenv
load_dotenv()

import argparse
import asyncio
import logging
import os
import sys

import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")

from api import DeltaRESTClient
from backtest import Backtester, BacktestConfig
from execution import ExecutionEngine
from risk import RiskConfig, RiskManager
from strategy import ConfluenceStrategy, load_strategy


def load_keys():
    key    = os.getenv("DELTA_API_KEY", "").strip()
    secret = os.getenv("DELTA_API_SECRET", "").strip()
    if not key or not secret:
        logger.error("❌ DELTA_API_KEY / DELTA_API_SECRET not set in .env")
    return key, secret


# ─── Commands ─────────────────────────────────────────────────────────────────

async def cmd_trade(args):
    key, secret = load_keys()
    if not key:
        return

    strategy = ConfluenceStrategy({
        "fast_ema":      args.fast_ema,
        "mid_ema":       args.mid_ema,
        "slow_ema":      args.slow_ema,
        "trend_ema":     200,
        "rsi_period":    14,
        "atr_period":    14,
        "adx_threshold": args.adx_threshold,
        "vol_factor":    args.vol_factor,
        "rsi_long_min":  40,
        "rsi_long_max":  60,
        "rsi_short_min": 40,
        "rsi_short_max": 60,
        "max_ema_distance_pct": 0.025,
        "funding_long_max": 0.02,
        "funding_short_min": -0.02,
        "sl_atr_mult":   args.sl_atr_mult,
        "tp_rr":         args.tp_rr,
        "swing_lookback": 4,
        "bb_std":        2.0,
        "breakout_lookback": 20,
        "breakout_buffer_atr": 0.12,
    })

    risk_cfg = RiskConfig(
        risk_per_trade        = args.risk_per_trade,
        max_open_trades       = 3,
        max_drawdown_pct      = args.max_drawdown,
        daily_loss_limit_pct  = args.daily_loss_limit,
        leverage              = float(args.leverage),
        max_position_size_pct = 0.25,
        breakeven_trigger_pct = 0.005,
        breakeven_buffer_pct  = 0.001,
        profit_lock_trigger_pct = 0.010,
        profit_lock_trail_pct   = 0.004,
        min_confidence        = args.min_confidence,
    )
    risk_cfg.leverage_by_symbol[args.symbol] = float(args.leverage)

    risk_mgr = RiskManager(risk_cfg, initial_capital=args.capital)

    async with DeltaRESTClient(key, secret) as rest:
        # Find product
        products = await rest.get_products()
        product  = next((p for p in products if p.get("symbol") == args.symbol), None)

        if not product:
            print(f"\n❌ Symbol '{args.symbol}' not found on Delta Exchange.")
            print("\nAvailable USDT perpetuals:")
            for p in products:
                sym = p.get("symbol", "")
                if "USDT" in sym or "USD" in sym:
                    print(f"  {sym}")
            return

        product_id = product["id"]
        account_asset = DeltaRESTClient.infer_account_asset(product, args.symbol)

        print(f"\n{'='*65}")
        print(f"  🤖 DELTA ALGO BOT — Confluence Strategy")
        print(f"{'='*65}")
        print(f"  Symbol       : {args.symbol}")
        print(f"  Product ID   : {product_id}")
        print(f"  Contract Val : {product.get('contract_value')}")
        print(f"  Account Asset: {account_asset}")
        print(f"  Capital      : ${args.capital:.2f} {account_asset}")
        print(f"  Leverage     : {args.leverage}x")
        print(f"  Risk/Trade   : {args.risk_per_trade*100:.1f}%")
        print(f"  Max DD Halt  : {args.max_drawdown*100:.0f}%")
        print(f"  Daily Loss   : {args.daily_loss_limit*100:.0f}%")
        print(f"  Candle Res   : {args.resolution}m")
        print(f"  Min Conf     : {args.min_confidence}")
        print(f"  ADX Thresh   : {args.adx_threshold}")
        print(f"  TP RR        : {args.tp_rr}R")
        print(f"{'='*65}\n")

        engine = ExecutionEngine(
            rest_client        = rest,
            strategy           = strategy,
            risk_manager       = risk_mgr,
            symbol             = args.symbol,
            product_id         = product_id,
            resolution_minutes = args.resolution,
            api_key            = key,
            api_secret         = secret,
            min_confidence     = args.min_confidence,
            trailing_enabled   = True,
            cooldown_minutes   = args.resolution,  # one trade per candle
            account_asset      = account_asset,
        )

        logger.info("🚀 Starting live trading: %s | cap=%.2f | %dx leverage | %dm candles",
                    args.symbol, args.capital, args.leverage, args.resolution)

        try:
            await engine.run()
        except KeyboardInterrupt:
            logger.info("Bot stopped by user.")


def cmd_backtest(args):
    print(f"\n{'='*65}")
    print(f"  📊 BACKTEST: Confluence Strategy on {args.symbol}")
    print(f"{'='*65}")

    strategy = ConfluenceStrategy()
    config   = BacktestConfig(
        initial_capital = args.capital,
        risk_per_trade  = 0.01,
        taker_fee       = 0.0005,
        slippage_pct    = 0.0003,
        leverage        = float(args.leverage),
        min_confidence  = 0.55,
    )

    if getattr(args, "data_file", None) and os.path.exists(args.data_file):
        df = pd.read_csv(args.data_file, index_col=0, parse_dates=True)
        df.columns = [c.lower() for c in df.columns]
        print(f"  Loaded {len(df)} bars from {args.data_file}")
    else:
        print("  Generating synthetic BTC data (3000 bars)…")
        df = _gen_synthetic(n=3000)

    # Walk-forward: 70% train, 30% test
    split   = int(len(df) * 0.70)
    train_df = df.iloc[:split]
    test_df  = df.iloc[split:]

    bt = Backtester(config)

    print(f"\n  TRAIN SET ({len(train_df)} bars):")
    train_result = bt.run(train_df, strategy, symbol=args.symbol)
    print(train_result.summary())

    print(f"\n  TEST SET / OUT-OF-SAMPLE ({len(test_df)} bars):")
    test_result = bt.run(test_df, strategy, symbol=args.symbol)
    print(test_result.summary())

    if test_result.trades:
        print("\n  Last 10 trades (test set):")
        for t in test_result.trades[-10:]:
            sign = "+" if t.net_pnl >= 0 else ""
            print(f"  {str(t.entry_time)[:10]} {t.side:5s} → {t.exit_reason:15s}  {sign}${t.net_pnl:.2f}")

    test_result.equity_curve.to_csv("equity_curve.csv")
    print(f"\n  ✅ Equity curve saved → equity_curve.csv")


async def cmd_status(args):
    key, secret = load_keys()
    if not key:
        return

    async with DeltaRESTClient(key, secret) as rest:
        balance   = await rest.get_wallet_balance("USDT")
        positions = await rest.get_positions()
        orders    = await rest.get_open_orders()

        print(f"\n{'='*55}")
        print(f"  📋 Account Status")
        print(f"{'='*55}")
        print(f"  USDT Balance  : ${balance:.4f}")
        print(f"\n  Open Positions ({len(positions)}):")
        if positions:
            for p in positions:
                pnl_s = f"{p.unrealized_pnl:+.4f}"
                print(f"    {p.symbol:15s} {p.side:5s} size={p.size} entry={p.entry_price:.4f} upnl={pnl_s}")
        else:
            print("    None")
        print(f"\n  Open Orders ({len(orders)}):")
        if orders:
            for o in orders[:10]:
                print(f"    id={o.get('id')} {o.get('side')} {o.get('size')} @ {o.get('limit_price') or 'MKT'}")
        else:
            print("    None")


async def cmd_info(args):
    key, secret = load_keys()
    async with DeltaRESTClient(key, secret) as rest:
        product = await rest.get_product(args.symbol)
        ticker  = await rest.get_ticker(args.symbol)
        ob      = await rest.get_orderbook(args.symbol, depth=5)

        if not product:
            print(f"Symbol {args.symbol} not found")
            return

        print(f"\n{'='*55}")
        print(f"  {args.symbol} Product Info")
        print(f"{'='*55}")
        print(f"  Product ID   : {product.get('id')}")
        print(f"  Contract Val : {product.get('contract_value')}")
        print(f"  Min Lot Size : {product.get('min_size')}")
        print(f"  Last Price   : {ticker.last_price:.4f}")
        print(f"  Mark Price   : {ticker.mark_price:.4f}")
        print(f"  Bid          : {ticker.bid:.4f}")
        print(f"  Ask          : {ticker.ask:.4f}")
        print(f"  Spread       : {ob.spread():.6f}")
        print(f"  Funding Rate : {ticker.funding_rate:.6f}")
        print(f"  OI (USD)     : {ticker.open_interest:,.0f}")
        print(f"  OB Imbalance : {ob.imbalance():.3f} (>0=bullish)")


# ─── Synthetic Data ────────────────────────────────────────────────────────────

def _gen_synthetic(n: int = 3000, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dt  = 1 / (24 * 365)
    mu, sigma_base = 0.20, 0.75
    prices, vols = [30_000.0], [sigma_base]
    for _ in range(n - 1):
        v = 0.9 * vols[-1] + 0.1 * sigma_base + 0.05 * abs(rng.standard_normal())
        vols.append(v)
        ret = (mu - 0.5 * v**2) * dt + v * np.sqrt(dt) * rng.standard_normal()
        prices.append(prices[-1] * np.exp(ret))
    prices = np.array(prices)
    hi  = prices * (1 + rng.uniform(0.001, 0.012, n))
    lo  = prices * (1 - rng.uniform(0.001, 0.012, n))
    op  = np.roll(prices, 1); op[0] = prices[0]
    vol = rng.lognormal(mean=10, sigma=1, size=n)
    idx = pd.date_range(start="2024-01-01", periods=n, freq="1h")
    return pd.DataFrame({"open": op, "high": hi, "low": lo, "close": prices, "volume": vol}, index=idx)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def build_parser():
    p = argparse.ArgumentParser(
        description="Delta Exchange Algo Bot — Institutional Confluence Strategy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py trade --symbol ETH_USDT --capital 500 --leverage 5
  python main.py trade --symbol BTC_USDT --capital 1000 --leverage 3 --resolution 60
  python main.py backtest --symbol BTC_USDT --capital 10000
  python main.py status
  python main.py info --symbol BTC_USDT
        """,
    )
    sub = p.add_subparsers(dest="command", required=True)

    # trade
    t = sub.add_parser("trade", help="Run live trading bot")
    t.add_argument("--symbol",           default="ETH_USDT")
    t.add_argument("--capital",          type=float, default=100.0, help="Starting capital in USDT")
    t.add_argument("--leverage",         type=int,   default=5,     help="Leverage (recommended: 3-10)")
    t.add_argument("--resolution",       type=int,   default=15,    help="Candle timeframe in minutes")
    t.add_argument("--risk-per-trade",   type=float, default=0.01,  dest="risk_per_trade", help="Risk per trade (0.01=1%%)")
    t.add_argument("--max-drawdown",     type=float, default=0.15,  dest="max_drawdown",   help="Max drawdown to halt (0.15=15%%)")
    t.add_argument("--daily-loss-limit", type=float, default=0.08,  dest="daily_loss_limit")
    t.add_argument("--min-confidence",   type=float, default=0.50,  dest="min_confidence")
    t.add_argument("--adx-threshold",    type=float, default=18.0,  dest="adx_threshold")
    t.add_argument("--vol-factor",       type=float, default=1.05,  dest="vol_factor")
    t.add_argument("--sl-atr-mult",      type=float, default=1.2,   dest="sl_atr_mult")
    t.add_argument("--tp-rr",            type=float, default=2.0,   dest="tp_rr",         help="Risk-reward ratio for TP")
    t.add_argument("--fast-ema",         type=int,   default=9,     dest="fast_ema")
    t.add_argument("--mid-ema",          type=int,   default=21,    dest="mid_ema")
    t.add_argument("--slow-ema",         type=int,   default=50,    dest="slow_ema")

    # backtest
    b = sub.add_parser("backtest", help="Run backtest on historical or synthetic data")
    b.add_argument("--symbol",    default="BTC_USDT")
    b.add_argument("--capital",   type=float, default=10_000.0)
    b.add_argument("--leverage",  type=float, default=5.0)
    b.add_argument("--data-file", default=None, dest="data_file", help="Path to CSV (OHLCV) data")

    # status
    sub.add_parser("status", help="Show account status, positions, orders")

    # info
    i = sub.add_parser("info", help="Show product info and ticker")
    i.add_argument("--symbol", default="ETH_USDT")

    return p


if __name__ == "__main__":
    args = build_parser().parse_args()
    if args.command == "trade":
        asyncio.run(cmd_trade(args))
    elif args.command == "backtest":
        cmd_backtest(args)
    elif args.command == "status":
        asyncio.run(cmd_status(args))
    elif args.command == "info":
        asyncio.run(cmd_info(args))
