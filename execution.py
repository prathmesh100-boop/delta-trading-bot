"""
execution.py — Real-time execution engine.
Wires together API client, strategy, and risk manager.
Handles signal → order placement → position tracking loop.
"""

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd

from api import (
    DeltaRESTClient, DeltaWSClient,
    Order, OrderSide, OrderStatus, OrderType, OHLCV
)
from risk import RiskConfig, RiskManager, TradeRecord
from strategy import BaseStrategy, Signal, SignalType

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Candle buffer — maintains a rolling OHLCV window
# ─────────────────────────────────────────────

class CandleBuffer:
    def __init__(self, maxlen: int = 500):
        self.maxlen = maxlen
        self._data: List[OHLCV] = []

    def push(self, candle: OHLCV):
        self._data.append(candle)
        if len(self._data) > self.maxlen:
            self._data.pop(0)

    def to_dataframe(self) -> Optional[pd.DataFrame]:
        if not self._data:
            return None
        rows = [
            {"timestamp": c.timestamp, "open": c.open, "high": c.high,
             "low": c.low, "close": c.close, "volume": c.volume}
            for c in self._data
        ]
        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df = df.set_index("timestamp").sort_index()
        return df

    def __len__(self):
        return len(self._data)


# ─────────────────────────────────────────────
# Trade logger (CSV-based audit trail)
# ─────────────────────────────────────────────

class TradeLogger:
    def __init__(self, filepath: str = "trade_history.csv"):
        self.filepath = filepath
        self._records: List[Dict] = []

    def log(self, trade: TradeRecord):
        self._records.append({
            "symbol": trade.symbol,
            "side": trade.side,
            "entry_time": trade.entry_time,
            "exit_time": trade.exit_time,
            "entry_price": trade.entry_price,
            "exit_price": trade.exit_price,
            "size": trade.size,
            "pnl": trade.realised_pnl,
            "order_id": trade.order_id,
        })
        self._flush()

    def _flush(self):
        pd.DataFrame(self._records).to_csv(self.filepath, index=False)
        logger.debug("Trade log saved to %s", self.filepath)


# ─────────────────────────────────────────────
# Execution Engine
# ─────────────────────────────────────────────

class ExecutionEngine:
    """
    Async execution loop:
      1. Fetch / receive OHLCV candles
      2. Run strategy → Signal
      3. Risk check
      4. Place order via REST
      5. Track position and manage exits
    """

    def __init__(
        self,
        rest_client: DeltaRESTClient,
        strategy: BaseStrategy,
        risk_manager: RiskManager,
        symbol: str,
        product_id: int,
        resolution_minutes: int = 15,
        api_key: str = "",
        api_secret: str = "",
    ):
        self.rest = rest_client
        self.strategy = strategy
        self.risk = risk_manager
        self.symbol = symbol
        self.product_id = product_id
        self.resolution = resolution_minutes
        self.api_key = api_key
        self.api_secret = api_secret

        self.buffer = CandleBuffer(maxlen=300)
        self.trade_logger = TradeLogger()
        self._current_trade: Optional[TradeRecord] = None
        self._running = False

    # ─────────────────────────────────────────
    # Bootstrap
    # ─────────────────────────────────────────

    async def bootstrap_history(self):
        """Pre-fill candle buffer with recent historical data."""
        import time
        end = int(time.time())
        start = end - self.resolution * 60 * 300     # ~300 bars back
        logger.info("Fetching historical candles for %s…", self.symbol)
        candles = await self.rest.get_ohlcv(self.symbol, self.resolution, start, end)
        for c in candles:
            self.buffer.push(c)
        logger.info("Buffer primed with %d candles", len(self.buffer))

    # ─────────────────────────────────────────
    # Main loop (polling mode)
    # ─────────────────────────────────────────

    async def run_polling(self, interval_seconds: int = 60):
        """
        Polling execution loop — fetches latest candle every `interval_seconds`.
        Use run_websocket() for lower latency.
        """
        self._running = True
        await self.bootstrap_history()

        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                logger.error("Tick error: %s", exc, exc_info=True)
            await asyncio.sleep(interval_seconds)

    async def _tick(self):
        """Process one bar: fetch candle → signal → risk → order."""
        import time
        end = int(time.time())
        start = end - self.resolution * 60 * 3
        candles = await self.rest.get_ohlcv(self.symbol, self.resolution, start, end)
        for c in candles:
            self.buffer.push(c)

        df = self.buffer.to_dataframe()
        if df is None or len(df) < 60:
            logger.warning("Insufficient data in buffer (%d bars)", len(df or []))
            return

        latest_price = df["close"].iloc[-1]

        # ── Check trailing stop / TP / SL ────
        if self._current_trade:
            self.risk.update_trailing_stops(self.symbol, latest_price)

            exit_trade = self.risk.should_exit_by_stop(self.symbol, latest_price) or \
                         self.risk.should_exit_by_tp(self.symbol, latest_price)

            if exit_trade:
                await self._execute_close(exit_trade, latest_price, reason="risk_mgr")
                return

        # ── Generate signal ───────────────────
        signal = self.strategy.generate_signal(df, self.symbol)
        logger.info("[%s] Signal: %s @ %.4f", self.symbol, signal.type, latest_price)

        if signal.type == SignalType.HOLD:
            return

        # ── Close existing opposite position ─
        if self._current_trade:
            opposite = (
                (self._current_trade.side == "long" and signal.type == SignalType.SHORT) or
                (self._current_trade.side == "short" and signal.type == SignalType.LONG)
            )
            if opposite:
                await self._execute_close(self._current_trade, latest_price, reason="signal_flip")

        # ── Open new position ─────────────────
        if signal.type in (SignalType.LONG, SignalType.SHORT):
            if not self.risk.check_signal(signal):
                logger.info("Signal blocked by risk manager")
                return
            size_usd = self.risk.calculate_position_size(signal, latest_price)
            size_contracts = size_usd / latest_price
            await self._execute_entry(signal, size_contracts, latest_price)

    # ─────────────────────────────────────────
    # Order placement helpers
    # ─────────────────────────────────────────

    async def _execute_entry(self, signal: Signal, size: float, price: float):
        side = OrderSide.BUY if signal.type == SignalType.LONG else OrderSide.SELL
        client_id = str(uuid.uuid4())[:8]

        order = Order(
            product_id=self.product_id,
            side=side,
            order_type=OrderType.MARKET,
            size=round(size, 4),
            client_order_id=client_id,
        )

        try:
            order = await self.rest.place_order(order)
        except Exception as exc:
            logger.error("Order placement failed: %s", exc)
            return

        trade = TradeRecord(
            symbol=self.symbol,
            side="long" if side == OrderSide.BUY else "short",
            entry_price=price,
            size=size,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            entry_time=datetime.utcnow(),
            order_id=order.order_id,
            peak_price=price,
        )
        self._current_trade = trade
        self.risk.register_trade(trade)

        # Place stop-loss order on exchange
        if signal.stop_loss:
            await self._place_stop_order(signal, size, price)

        logger.info(
            "ENTRY: %s %s size=%.4f entry=%.4f sl=%.4f tp=%.4f",
            trade.side, self.symbol, size, price, signal.stop_loss, signal.take_profit,
        )

    async def _place_stop_order(self, signal: Signal, size: float, entry_price: float):
        """Place a stop-market on the exchange as backup SL."""
        is_long = signal.type == SignalType.LONG
        sl_side = OrderSide.SELL if is_long else OrderSide.BUY
        sl_order = Order(
            product_id=self.product_id,
            side=sl_side,
            order_type=OrderType.STOP_MARKET,
            size=round(size, 4),
            stop_price=round(signal.stop_loss, 2),
            reduce_only=True,
        )
        try:
            await self.rest.place_order(sl_order)
            logger.info("Stop-loss order placed @ %.4f", signal.stop_loss)
        except Exception as exc:
            logger.warning("Stop order failed (will manage in software): %s", exc)

    async def _execute_close(self, trade: TradeRecord, price: float, reason: str):
        close_side = OrderSide.SELL if trade.side == "long" else OrderSide.BUY
        order = Order(
            product_id=self.product_id,
            side=close_side,
            order_type=OrderType.MARKET,
            size=round(trade.size, 4),
            reduce_only=True,
        )
        try:
            await self.rest.place_order(order)
        except Exception as exc:
            logger.error("Close order failed: %s", exc)
            return

        # Cancel any resting SL orders
        await self.rest.cancel_all_orders(self.product_id)

        now = datetime.utcnow()
        self.risk.record_trade_close(trade, price, now)
        self.trade_logger.log(trade)
        self._current_trade = None

        logger.info(
            "EXIT (%s): %s %s @ %.4f pnl=%.2f",
            reason, trade.side, self.symbol, price, trade.realised_pnl,
        )

    def stop(self):
        self._running = False
        logger.info("Execution engine stopping…")
