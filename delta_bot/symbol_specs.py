from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class SymbolSpec:
    leverage: float
    fallback_lot_size: float
    margin_per_lot_usd: float


SYMBOL_SPECS: Dict[str, SymbolSpec] = {
    "BTCUSD": SymbolSpec(leverage=10.0, fallback_lot_size=0.001, margin_per_lot_usd=7.56),
    "ETHUSD": SymbolSpec(leverage=20.0, fallback_lot_size=0.01, margin_per_lot_usd=1.19),
    "SOLUSD": SymbolSpec(leverage=15.0, fallback_lot_size=1.0, margin_per_lot_usd=5.97),
    "BNBUSD": SymbolSpec(leverage=10.0, fallback_lot_size=0.1, margin_per_lot_usd=6.37),
    "XRPUSD": SymbolSpec(leverage=10.0, fallback_lot_size=1.0, margin_per_lot_usd=0.14),
    "BTC_USDT": SymbolSpec(leverage=10.0, fallback_lot_size=0.001, margin_per_lot_usd=7.56),
    "ETH_USDT": SymbolSpec(leverage=20.0, fallback_lot_size=0.01, margin_per_lot_usd=1.19),
    "SOL_USDT": SymbolSpec(leverage=15.0, fallback_lot_size=1.0, margin_per_lot_usd=5.97),
    "BNB_USDT": SymbolSpec(leverage=10.0, fallback_lot_size=0.1, margin_per_lot_usd=6.37),
    "XRP_USDT": SymbolSpec(leverage=10.0, fallback_lot_size=1.0, margin_per_lot_usd=0.14),
}


def get_symbol_spec(symbol: str) -> Optional[SymbolSpec]:
    return SYMBOL_SPECS.get(str(symbol or "").strip().upper())

