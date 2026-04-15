"""
main.py — Delta Exchange Algo Trading Bot V8
Multi-coin architecture + per-trade analytics + true dip buying

Usage:
    # Single bot (all capital)
    python main.py trade --symbol BTCUSD --capital 92 --leverage 3

    # Multi-coin: split capital correctly across bots
    # Run each in a separate terminal / systemd service:
    python main.py trade --symbol BTCUSD --capital 30 --leverage 3
    python main.py trade --symbol ETHUSD --capital 30 --leverage 3
    python main.py trade --symbol SOLUSD --capital 30 --leverage 3

    # Backtest
    python main.py backtest --symbol BTCUSD
    python main.py analytics          ← NEW: show per-setup profitability
    python main.py status
    python main.py info --symbol ETHUSD

V8 Key Changes:
  - MULTI-COIN FIX: --capital now means "this bot's allocated slice"
    Each bot only risks from its own slice. No more overlapping capital.
  - Per-trade analytics: setup_type, entry_grade, quality_score in all CSVs
  - True dip buying: EMA21 must be touched in last 3 bars (not just "near")
  - Extension guard: blocks entries when price is extended from EMA21
  - analytics command: shows profitability by setup type and entry grade
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

from delta_bot.config import StorageConfig
from delta_bot.orchestrator import build_risk_config_from_args, build_strategy_from_args, run_multi_symbol

def load_keys():
    key    = os.getenv("DELTA_API_KEY", "").strip()
    secret = os.getenv("DELTA_API_SECRET", "").strip()
    if not key or not secret:
        logger.error("❌ DELTA_API_KEY / DELTA_API_SECRET not set in .env")
    return key, secret


# ─── Commands ─────────────────────────────────────────────────────────────────

async def cmd_trade(args):
    from api import DeltaRESTClient
    from delta_bot.runtime import build_audit_store, build_portfolio_risk_manager
    from execution import ExecutionEngine
    from risk import RiskManager

    key, secret = load_keys()
    if not key:
        return

    strategy = build_strategy_from_args(args)
    risk_cfg = build_risk_config_from_args(args, args.symbol)

    # V8: risk manager is initialised with the ALLOCATED capital slice
    # This means each bot's drawdown/risk is calculated from its own slice,
    # not the total account balance.
    risk_mgr = RiskManager(risk_cfg, initial_capital=args.capital)
    audit_store = build_audit_store(StorageConfig())
    portfolio_risk = build_portfolio_risk_manager(initial_capital=args.capital, store=audit_store)
    audit_store.record_event(
        "system",
        "trade_command_started",
        {
            "symbol": args.symbol,
            "capital": args.capital,
            "leverage": args.leverage,
            "resolution": args.resolution,
        },
        symbol=args.symbol,
    )

    async with DeltaRESTClient(key, secret) as rest:
        products = await rest.get_products()
        product  = next((p for p in products if p.get("symbol") == args.symbol), None)

        if not product:
            print(f"\n❌ Symbol '{args.symbol}' not found on Delta Exchange.")
            print("\nAvailable USD/USDT perpetuals:")
            for p in products:
                sym = p.get("symbol", "")
                if "USDT" in sym or "USD" in sym:
                    print(f"  {sym}")
            return

        product_id    = product["id"]
        account_asset = DeltaRESTClient.infer_account_asset(product, args.symbol)

        print(f"\n{'='*68}")
        print(f"  🤖 DELTA ALGO BOT V8 — True Dip Buying + Analytics")
        print(f"{'='*68}")
        print(f"  Symbol          : {args.symbol}")
        print(f"  Product ID      : {product_id}")
        print(f"  Contract Val    : {product.get('contract_value')}")
        print(f"  Account Asset   : {account_asset}")
        print(f"  Allocated Capital: ${args.capital:.2f} {account_asset}  ← this bot's slice only")
        print(f"  Leverage        : {args.leverage}x")
        print(f"  Risk/Trade      : {args.risk_per_trade*100:.1f}%")
        print(f"  Max DD Halt     : {args.max_drawdown*100:.0f}%")
        print(f"  Daily Loss Halt : {args.daily_loss_limit*100:.0f}%")
        print(f"  Candle Res      : {args.resolution}m")
        print(f"  Min Confidence  : {args.min_confidence}")
        print(f"  ADX Threshold   : {args.adx_threshold}")
        print(f"  TP RR           : {args.tp_rr}R")
        print(f"  RSI Long        : 35–52  (pullback zone)")
        print(f"  RSI Short       : 48–65  (pullback zone)")
        print(f"  EMA Touch LB    : 3 bars (true dip buying)")
        print(f"  Extension Guard : 1.5× ATR max distance from EMA21")
        print(f"  BE Trigger      : +1.0%  (V7 fix, kept)")
        print(f"  Trail Width     : 0.8%   (V7 fix, kept)")
        print(f"{'='*68}")
        print(f"")
        print(f"  ⚠️  MULTI-COIN SETUP: if running multiple bots, each must")
        print(f"     use --capital set to its OWN slice (e.g. 30 each for 3 bots)")
        print(f"     Total should not exceed your actual account balance.")
        print(f"{'='*68}\n")

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
            cooldown_minutes   = args.resolution,
            account_asset      = account_asset,
            audit_store        = audit_store,
            portfolio_risk     = portfolio_risk,
        )

        logger.info("🚀 Starting V8: %s | allocated=%.2f | %dx leverage | %dm candles",
                    args.symbol, args.capital, args.leverage, args.resolution)

        try:
            await engine.run()
        except KeyboardInterrupt:
            logger.info("Bot stopped by user.")


async def cmd_trade_portfolio(args):
    key, secret = load_keys()
    if not key:
        return
    await run_multi_symbol(args, key, secret, logger)


def cmd_backtest(args):
    from backtest import Backtester, BacktestConfig
    from strategy import ConfluenceStrategy

    print(f"\n{'='*65}")
    print(f"  📊 BACKTEST V8: {args.symbol}")
    print(f"{'='*65}")

    strategy = ConfluenceStrategy()
    config   = BacktestConfig(
        initial_capital = args.capital,
        risk_per_trade  = 0.01,
        taker_fee       = 0.0005,
        slippage_pct    = 0.0003,
        leverage        = float(args.leverage),
        min_confidence  = 0.58,
    )

    if getattr(args, "data_file", None) and os.path.exists(args.data_file):
        df = pd.read_csv(args.data_file, index_col=0, parse_dates=True)
        df.columns = [c.lower() for c in df.columns]
        print(f"  Loaded {len(df)} bars from {args.data_file}")
    else:
        print("  Generating synthetic BTC data (3000 bars)…")
        df = _gen_synthetic(n=3000)

    split    = int(len(df) * 0.70)
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
            print(f"  {str(t.entry_time)[:10]} {t.side:5s} → {t.exit_reason:20s}  {sign}${t.net_pnl:.2f}")

    test_result.equity_curve.to_csv("equity_curve.csv")
    print(f"\n  ✅ Equity curve saved → equity_curve.csv")


def cmd_analytics(args):
    """Show per-setup and per-grade profitability from trade_history.csv."""
    from pathlib import Path

    def _safe_label(value, fallback: str = "unknown") -> str:
        if pd.isna(value):
            return fallback
        text = str(value).strip()
        return text if text else fallback

    def _expectancy_stats(group: pd.DataFrame) -> dict:
        pnl = group["pnl"].astype(float)
        wins = pnl[pnl > 0]
        losses = pnl[pnl <= 0]
        win_rate = len(wins) / len(pnl) if len(pnl) else 0.0
        loss_rate = len(losses) / len(pnl) if len(pnl) else 0.0
        avg_win = wins.mean() if len(wins) else 0.0
        avg_loss_abs = abs(losses.mean()) if len(losses) else 0.0
        expectancy = avg_win * win_rate - avg_loss_abs * loss_rate
        return {
            "trades": len(group),
            "win_rate": win_rate,
            "total_pnl": pnl.sum(),
            "avg_pnl": pnl.mean() if len(pnl) else 0.0,
            "avg_win": avg_win,
            "avg_loss": -avg_loss_abs,
            "expectancy": expectancy,
        }

    def _print_expectancy_table(df: pd.DataFrame, group_col: str, title: str, width: int = 18) -> None:
        if group_col not in df.columns or "pnl" not in df.columns:
            return

        working = df[[group_col, "pnl"]].copy()
        working[group_col] = working[group_col].apply(_safe_label)

        rows = []
        for label, group in working.groupby(group_col):
            stats = _expectancy_stats(group)
            rows.append((label, stats))

        if not rows:
            return

        rows.sort(key=lambda item: (item[1]["expectancy"], item[1]["total_pnl"]), reverse=True)
        print(f"\n  {title}")
        print(f"  {'Group':<{width}} {'Trades':>6} {'Win%':>6} {'Exp':>9} {'AvgWin':>9} {'AvgLoss':>9} {'Total':>10}")
        print(f"  {'-' * (width + 52)}")
        for label, stats in rows:
            marker = "✅" if stats["expectancy"] > 0 else "❌"
            print(
                f"  {marker} {label:<{width-2}} {stats['trades']:>6} "
                f"{stats['win_rate']*100:>5.1f}% {stats['expectancy']:>+9.4f} "
                f"{stats['avg_win']:>+9.4f} {stats['avg_loss']:>+9.4f} {stats['total_pnl']:>+10.4f}"
            )

    def _recommendations(df: pd.DataFrame) -> list[str]:
        recs: list[str] = []
        if "pnl" not in df.columns:
            return recs

        if "setup_type" in df.columns:
            setup_stats = df.groupby("setup_type")["pnl"].agg(["count", "mean", "sum"])
            for setup, row in setup_stats.sort_values("mean").iterrows():
                label = _safe_label(setup)
                if int(row["count"]) >= 5 and float(row["mean"]) < 0:
                    recs.append(
                        f"{label} expectancy is negative over {int(row['count'])} trades; consider disabling or tightening it."
                    )

        if "entry_grade" in df.columns:
            grade_stats = df.groupby("entry_grade")["pnl"].agg(["count", "mean"])
            for grade, row in grade_stats.sort_values("mean").iterrows():
                label = _safe_label(grade, "?")
                if int(row["count"]) >= 5 and float(row["mean"]) < 0:
                    recs.append(
                        f"Grade {label} averages negative PnL over {int(row['count'])} trades; consider filtering it out."
                    )

        if "regime" in df.columns:
            regime_stats = df.groupby("regime")["pnl"].agg(["count", "mean"])
            for regime, row in regime_stats.sort_values("mean").iterrows():
                label = _safe_label(regime)
                if int(row["count"]) >= 5 and float(row["mean"]) < 0:
                    recs.append(
                        f"{label} regime underperforms over {int(row['count'])} trades; treat it as a risk-on filter, not a default."
                    )

        if "symbol" in df.columns:
            symbol_stats = df.groupby("symbol")["pnl"].agg(["count", "mean"])
            for symbol, row in symbol_stats.sort_values("mean").iterrows():
                label = _safe_label(symbol)
                if int(row["count"]) >= 5 and float(row["mean"]) < 0:
                    recs.append(
                        f"{label} is negative over {int(row['count'])} trades; reduce focus there before expanding symbol count."
                    )

        return recs[:6]

    history_path = Path("trade_history.csv")
    if not history_path.exists():
        print("\n❌ No trade_history.csv found. Run live trading first.")
        return

    df = pd.read_csv(history_path)
    if df.empty:
        print("\n❌ trade_history.csv is empty.")
        return

    print(f"\n{'='*65}")
    print(f"  📊 PER-TRADE ANALYTICS")
    print(f"{'='*65}")
    print(f"  Total trades: {len(df)}")

    if "pnl" in df.columns:
        total_pnl = df["pnl"].sum()
        wins = df[df["pnl"] > 0]
        losses = df[df["pnl"] <= 0]
        avg_win = wins["pnl"].mean() if len(wins) else 0.0
        avg_loss = losses["pnl"].mean() if len(losses) else 0.0
        expectancy = (len(wins) / len(df) * avg_win) + (len(losses) / len(df) * avg_loss) if len(df) else 0.0
        print(f"  Total PnL   : ${total_pnl:+.4f}")
        print(f"  Win rate    : {len(wins)/len(df)*100:.1f}%  ({len(wins)}W / {len(losses)}L)")
        print(f"  Expectancy  : {expectancy:+.4f} per trade")
        print(f"  Avg Win/Loss: {avg_win:+.4f} / {avg_loss:+.4f}")
        if len(losses) > 0 and losses["pnl"].sum() != 0:
            pf = abs(wins["pnl"].sum() / losses["pnl"].sum()) if len(losses) > 0 else 999
            print(f"  Profit factor: {pf:.2f}")

    # By setup type
    if "setup_type" in df.columns and "pnl" in df.columns:
        print(f"\n  ── By Setup Type ──────────────────────────────────")
        print(f"  {'Setup':<22} {'Trades':>6} {'Win%':>6} {'Total PnL':>10} {'Avg PnL':>8}")
        print(f"  {'-'*58}")
        for setup, group in df.groupby("setup_type"):
            n     = len(group)
            wins  = (group["pnl"] > 0).sum()
            wr    = wins / n * 100
            total = group["pnl"].sum()
            avg   = group["pnl"].mean()
            arrow = "✅" if total > 0 else "❌"
            print(f"  {arrow} {setup:<20} {n:>6} {wr:>5.1f}% {total:>+10.4f} {avg:>+8.4f}")

    _print_expectancy_table(df, "setup_type", "── Expectancy By Setup ─────────────────────────────", width=22)

    # By entry grade
    if "entry_grade" in df.columns and "pnl" in df.columns:
        print(f"\n  ── By Entry Grade ─────────────────────────────────")
        print(f"  {'Grade':<8} {'Trades':>6} {'Win%':>6} {'Total PnL':>10} {'Avg PnL':>8}")
        print(f"  {'-'*44}")
        for grade in ["A", "B", "C", "D", "?"]:
            group = df[df["entry_grade"] == grade]
            if len(group) == 0:
                continue
            n     = len(group)
            wins  = (group["pnl"] > 0).sum()
            wr    = wins / n * 100
            total = group["pnl"].sum()
            avg   = group["pnl"].mean()
            arrow = "✅" if total > 0 else "❌"
            print(f"  {arrow} {grade:<6}   {n:>6} {wr:>5.1f}% {total:>+10.4f} {avg:>+8.4f}")

    _print_expectancy_table(df, "entry_grade", "── Expectancy By Grade ─────────────────────────────", width=12)

    # By symbol
    if "symbol" in df.columns and "pnl" in df.columns and df["symbol"].nunique() > 1:
        print(f"\n  ── By Symbol ──────────────────────────────────────")
        print(f"  {'Symbol':<12} {'Trades':>6} {'Win%':>6} {'Total PnL':>10}")
        print(f"  {'-'*40}")
        for sym, group in df.groupby("symbol"):
            n    = len(group)
            wins = (group["pnl"] > 0).sum()
            wr   = wins / n * 100
            total = group["pnl"].sum()
            arrow = "✅" if total > 0 else "❌"
            print(f"  {arrow} {sym:<10}   {n:>6} {wr:>5.1f}% {total:>+10.4f}")

    _print_expectancy_table(df, "symbol", "── Expectancy By Symbol ────────────────────────────", width=14)
    _print_expectancy_table(df, "regime", "── Expectancy By Regime ────────────────────────────", width=14)

    # By exit reason
    if "exit_reason" in df.columns and "pnl" in df.columns:
        print(f"\n  ── By Exit Reason ─────────────────────────────────")
        for reason, group in df.groupby("exit_reason"):
            n    = len(group)
            total = group["pnl"].sum()
            avg  = group["pnl"].mean()
            print(f"  {reason:<25} {n:>4} trades  total={total:>+8.4f}  avg={avg:>+7.4f}")

    # Recommendation
    print(f"\n  ── Recommendation ─────────────────────────────────")
    recommendations = _recommendations(df)
    if recommendations:
        for rec in recommendations:
            print(f"  - {rec}")
    elif "entry_grade" in df.columns and "pnl" in df.columns:
        grade_pnl = df.groupby("entry_grade")["pnl"].mean()
        best_grade = grade_pnl.idxmax() if not grade_pnl.empty else "?"
        print(f"  Best entry grade: {best_grade} (avg {grade_pnl.get(best_grade, 0):+.4f} per trade)")
        print("  No negative bucket has enough sample size yet for a strong recommendation.")
    print(f"{'='*65}\n")


async def cmd_status(args):
    from api import DeltaRESTClient

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
    from api import DeltaRESTClient

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


def cmd_api_server(args):
    import uvicorn
    from backend_api import create_backend_app

    app = create_backend_app()
    logger.info("Starting control API on http://%s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


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
        description="Delta Exchange Algo Bot V8 — Multi-coin + Analytics",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Multi-coin setup (each in separate terminal):
  python main.py trade --symbol BTCUSD --capital 30 --leverage 3
  python main.py trade --symbol ETHUSD --capital 30 --leverage 3
  python main.py trade --symbol SOLUSD --capital 30 --leverage 3

Shared-portfolio multi-symbol runtime:
  python main.py trade-portfolio --symbols BTCUSD,ETHUSD,SOLUSD --capital 90 --leverage 3

Single coin (all capital):
  python main.py trade --symbol BTCUSD --capital 90 --leverage 3

1H candles (less noise, recommended for small capital):
  python main.py trade --symbol BTCUSD --capital 90 --leverage 3 --resolution 60

Analytics (after trades have run):
  python main.py analytics

Backtest:
  python main.py backtest --symbol BTCUSD --capital 10000
        """,
    )
    sub = p.add_subparsers(dest="command", required=True)

    # trade
    t = sub.add_parser("trade", help="Run live trading bot")
    t.add_argument("--symbol",           default="ETHUSD")
    t.add_argument("--capital",          type=float, default=30.0,
                   help="This bot's allocated capital slice (NOT total account)")
    t.add_argument("--leverage",         type=int,   default=3)
    t.add_argument("--resolution",       type=int,   default=15,
                   help="Candle timeframe in minutes (15 or 60 recommended)")
    t.add_argument("--risk-per-trade",   type=float, default=0.01,  dest="risk_per_trade")
    t.add_argument("--max-drawdown",     type=float, default=0.15,  dest="max_drawdown")
    t.add_argument("--daily-loss-limit", type=float, default=0.08,  dest="daily_loss_limit")
    t.add_argument("--min-confidence",   type=float, default=0.58,  dest="min_confidence")
    t.add_argument("--adx-threshold",    type=float, default=20.0,  dest="adx_threshold")
    t.add_argument("--vol-factor",       type=float, default=1.1,   dest="vol_factor")
    t.add_argument("--sl-atr-mult",      type=float, default=1.5,   dest="sl_atr_mult")
    t.add_argument("--tp-rr",            type=float, default=2.2,   dest="tp_rr")
    t.add_argument("--fast-ema",         type=int,   default=9,     dest="fast_ema")
    t.add_argument("--mid-ema",          type=int,   default=21,    dest="mid_ema")
    t.add_argument("--slow-ema",         type=int,   default=50,    dest="slow_ema")

    tp = sub.add_parser("trade-portfolio", help="Run one shared-portfolio process across multiple symbols")
    tp.add_argument("--symbols",          default="BTCUSD,ETHUSD,SOLUSD",
                    help="Comma-separated symbol list, e.g. BTCUSD,ETHUSD,SOLUSD")
    tp.add_argument("--capital",          type=float, default=90.0,
                    help="Shared portfolio capital across all symbols in this process")
    tp.add_argument("--leverage",         type=int,   default=3)
    tp.add_argument("--resolution",       type=int,   default=15,
                    help="Candle timeframe in minutes (15 or 60 recommended)")
    tp.add_argument("--risk-per-trade",   type=float, default=0.01,  dest="risk_per_trade")
    tp.add_argument("--max-drawdown",     type=float, default=0.15,  dest="max_drawdown")
    tp.add_argument("--daily-loss-limit", type=float, default=0.08,  dest="daily_loss_limit")
    tp.add_argument("--min-confidence",   type=float, default=0.58,  dest="min_confidence")
    tp.add_argument("--adx-threshold",    type=float, default=20.0,  dest="adx_threshold")
    tp.add_argument("--vol-factor",       type=float, default=1.1,   dest="vol_factor")
    tp.add_argument("--sl-atr-mult",      type=float, default=1.5,   dest="sl_atr_mult")
    tp.add_argument("--tp-rr",            type=float, default=2.2,   dest="tp_rr")
    tp.add_argument("--fast-ema",         type=int,   default=9,     dest="fast_ema")
    tp.add_argument("--mid-ema",          type=int,   default=21,    dest="mid_ema")
    tp.add_argument("--slow-ema",         type=int,   default=50,    dest="slow_ema")

    # backtest
    b = sub.add_parser("backtest", help="Backtest on historical or synthetic data")
    b.add_argument("--symbol",    default="BTCUSD")
    b.add_argument("--capital",   type=float, default=10_000.0)
    b.add_argument("--leverage",  type=float, default=3.0)
    b.add_argument("--data-file", default=None, dest="data_file")

    # analytics (new in V8)
    sub.add_parser("analytics", help="Show per-setup and per-grade profitability")

    # status
    sub.add_parser("status", help="Show account status, positions, orders")

    # info
    i = sub.add_parser("info", help="Show product info and ticker")
    i.add_argument("--symbol", default="ETHUSD")

    # backend API
    api_server = sub.add_parser("api-server", help="Run the operational FastAPI backend")
    api_server.add_argument("--host", default="127.0.0.1")
    api_server.add_argument("--port", type=int, default=8000)

    return p


if __name__ == "__main__":
    args = build_parser().parse_args()
    if args.command == "trade":
        asyncio.run(cmd_trade(args))
    elif args.command == "trade-portfolio":
        asyncio.run(cmd_trade_portfolio(args))
    elif args.command == "backtest":
        cmd_backtest(args)
    elif args.command == "analytics":
        cmd_analytics(args)
    elif args.command == "status":
        asyncio.run(cmd_status(args))
    elif args.command == "info":
        asyncio.run(cmd_info(args))
    elif args.command == "api-server":
        cmd_api_server(args)
