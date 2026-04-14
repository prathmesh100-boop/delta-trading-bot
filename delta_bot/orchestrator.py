from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Iterable, List

from risk import RiskConfig
from strategy import ConfluenceStrategy


@dataclass(frozen=True)
class SymbolRuntimeConfig:
    symbol: str
    product_id: int
    account_asset: str


def parse_symbols_arg(raw: str | Iterable[str]) -> List[str]:
    if isinstance(raw, str):
        items = raw.split(",")
    else:
        items = list(raw)
    seen = set()
    symbols: List[str] = []
    for item in items:
        symbol = str(item).strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    return symbols


def build_strategy_from_args(args) -> ConfluenceStrategy:
    return ConfluenceStrategy({
        "fast_ema": args.fast_ema,
        "mid_ema": args.mid_ema,
        "slow_ema": args.slow_ema,
        "trend_ema": 200,
        "rsi_period": 14,
        "atr_period": 14,
        "adx_threshold": args.adx_threshold,
        "vol_factor": args.vol_factor,
        "rsi_long_min": 35,
        "rsi_long_max": 52,
        "rsi_short_min": 48,
        "rsi_short_max": 65,
        "ema_touch_lookback": 3,
        "extension_atr_mult": 1.5,
        "max_ema_distance_pct": 0.03,
        "funding_long_max": 0.015,
        "funding_short_min": -0.015,
        "sl_atr_mult": args.sl_atr_mult,
        "tp_rr": args.tp_rr,
        "swing_lookback": 5,
        "bb_std": 2.0,
    })


def build_risk_config_from_args(args, symbol: str) -> RiskConfig:
    cfg = RiskConfig(
        risk_per_trade=args.risk_per_trade,
        max_open_trades=3,
        max_drawdown_pct=args.max_drawdown,
        daily_loss_limit_pct=args.daily_loss_limit,
        leverage=float(args.leverage),
        max_position_size_pct=0.25,
        breakeven_trigger_pct=0.010,
        breakeven_buffer_pct=0.002,
        profit_lock_trigger_pct=0.018,
        profit_lock_trail_pct=0.008,
        min_confidence=args.min_confidence,
    )
    cfg.leverage_by_symbol[symbol] = float(args.leverage)
    return cfg


async def resolve_symbol_configs(rest, symbols: Iterable[str]) -> List[SymbolRuntimeConfig]:
    from api import DeltaRESTClient

    products = await rest.get_products()
    by_symbol = {str(product.get("symbol", "")).upper(): product for product in products}
    configs: List[SymbolRuntimeConfig] = []
    for symbol in symbols:
        product = by_symbol.get(symbol.upper())
        if not product:
            raise ValueError(f"Symbol '{symbol}' not found on Delta Exchange")
        configs.append(
            SymbolRuntimeConfig(
                symbol=symbol.upper(),
                product_id=int(product["id"]),
                account_asset=DeltaRESTClient.infer_account_asset(product, symbol),
            )
        )
    return configs


async def run_multi_symbol(args, api_key: str, api_secret: str, logger) -> None:
    from api import DeltaRESTClient
    from delta_bot.config import StorageConfig
    from delta_bot.runtime import build_audit_store, build_portfolio_risk_manager
    from execution import ExecutionEngine
    from risk import RiskManager

    symbols = parse_symbols_arg(args.symbols)
    if not symbols:
        raise ValueError("No symbols provided")

    audit_store = build_audit_store(StorageConfig())
    portfolio_risk = build_portfolio_risk_manager(initial_capital=args.capital, store=audit_store)

    async with DeltaRESTClient(api_key, api_secret) as discovery_rest:
        symbol_configs = await resolve_symbol_configs(discovery_rest, symbols)

    audit_store.record_event(
        "system",
        "portfolio_runtime_started",
        {
            "symbols": [config.symbol for config in symbol_configs],
            "capital": args.capital,
            "leverage": args.leverage,
            "resolution": args.resolution,
        },
    )

    clients = [DeltaRESTClient(api_key, api_secret) for _ in symbol_configs]
    entered_clients = []
    try:
        for client in clients:
            entered_clients.append(await client.__aenter__())

        engines = []
        for client, config in zip(entered_clients, symbol_configs):
            strategy = build_strategy_from_args(args)
            risk_mgr = RiskManager(build_risk_config_from_args(args, config.symbol), initial_capital=args.capital)
            engines.append(
                ExecutionEngine(
                    rest_client=client,
                    strategy=strategy,
                    risk_manager=risk_mgr,
                    symbol=config.symbol,
                    product_id=config.product_id,
                    resolution_minutes=args.resolution,
                    api_key=api_key,
                    api_secret=api_secret,
                    min_confidence=args.min_confidence,
                    trailing_enabled=True,
                    cooldown_minutes=args.resolution,
                    account_asset=config.account_asset,
                    audit_store=audit_store,
                    portfolio_risk=portfolio_risk,
                )
            )

        logger.info(
            "Starting shared-portfolio runtime for %s | capital=%.2f | leverage=%sx | resolution=%sm",
            ", ".join(config.symbol for config in symbol_configs),
            args.capital,
            args.leverage,
            args.resolution,
        )
        await asyncio.gather(*(engine.run() for engine in engines))
    finally:
        for client in reversed(entered_clients):
            await client.__aexit__(None, None, None)
