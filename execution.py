"""
execution.py — Real-time execution engine.
Wires together API client, strategy, and risk manager.
Handles signal → order placement → position tracking loop.

FIXES:
  - Calls rest.usd_to_lots() to convert USD notional → integer lot count
  - Bootstraps product info before trading so lot conversion works
  - Guards against 0-lot orders (skips gracefully with a warning)
  - Passes symbol to usd_to_lots so contract_value is looked up correctly
"""

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd

from api import (
    DeltaRESTClient, DeltaWSClient,
    Order, OrderSide, OrderStatus, OrderType, OHLCV,
)
from risk import RiskConfig, RiskManager, TradeRecord
from strategy import BaseStrategy, Signal, SignalType
from notifier import send, send_trade_alert

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Candle buffer
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
            {
                "timestamp": c.timestamp,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in self._data
        ]
        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df = df.set_index("timestamp").sort_index()
        return df

    def __len__(self):
        return len(self._data)


# ─────────────────────────────────────────────
# Trade logger
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
            "size_lots": trade.size,
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
      1. Bootstrap product info + historical candles
      2. Poll for new candles every `interval_seconds`
      3. Run strategy → Signal
      4. Risk check
      5. Convert USD notional → integer lots
      6. Place order via REST
      7. Track position and manage exits
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

    # ── Bootstrap ─────────────────────────────

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

    async def bootstrap_product(self):
        """
        Fetch and cache product info so usd_to_lots() works correctly.
        Also logs contract_value and lot sizing for transparency.
        """
        logger.info("Fetching product info for %s…", self.symbol)
        product = await self.rest.get_product(self.symbol)
        if product:
            cv = product.get("contract_value", "?")
            min_size = product.get("min_size", "?")
            logger.info(
                "Product: %s | id=%s | contract_value=%s | min_size=%s",
                self.symbol,
                product.get("id"),
                cv,
                min_size,
            )
        else:
            logger.warning(
                "Product info not found for %s — lot calculation may be inaccurate",
                self.symbol,
            )

    # ── Main loop ─────────────────────────────

    async def run_polling(self, interval_seconds: int = 60):
        self._running = True
        await self.bootstrap_product()      # ← must come before bootstrap_history
        await self.bootstrap_history()

        # ── Emergency: close any orphaned positions from previous restart ──
        try:
            positions = await self.rest.get_positions()
            for pos in positions:
                if pos.symbol == self.symbol and pos.size != 0:
                    logger.warning(
                        "Found orphaned position on restart: %s %.2f lots. Closing immediately.",
                        self.symbol, pos.size,
                    )
                    await self._force_close_position(pos)
        except Exception as exc:
            logger.warning("Failed to check orphaned positions: %s", exc)

        # ── Set up WebSocket for real-time ticks (ZERO-LATENCY SL) ──
        ws_client = DeltaWSClient(
            api_key=self.api_key,
            api_secret=self.api_secret,
            on_message=self._handle_ws_tick,
        )
        # Subscribe to ticker channel for real-time price updates
        ws_client.subscribe([
            {
                "type": "subscribe",
                "channel": "ticker",
                "symbols": [self.symbol],
            }
        ])

        # ── Start concurrent tasks: WebSocket + signal generator ──
        ws_task = asyncio.create_task(ws_client.connect())
        signal_task = asyncio.create_task(self._generate_signals_loop(interval_seconds))
        
        try:
            await asyncio.gather(ws_task, signal_task)
        except asyncio.CancelledError:
            self._running = False
            await ws_client.disconnect()
            ws_task.cancel()
            signal_task.cancel()

    def _handle_ws_tick(self, msg: Dict):
        """
        WebSocket message handler for real-time ticker updates (ZERO-LATENCY).
        Called on every market tick (~100ms frequency on liquid markets).
        
        Delta sends: {
            "type": "ticker",
            "symbol": "BTC_USDT",
            "last_price": "43250.50",
            "mark_price": "43251.00",
            ...
        }
        """
        if msg.get("type") != "ticker":
            return
        
        if msg.get("symbol") != self.symbol:
            return
        
        try:
            latest_price = float(msg.get("last_price", 0))
            if latest_price <= 0 or not self._current_trade or self._current_trade.closed:
                return
            
            trade = self._current_trade
            
            # SL check (most critical — on every tick)
            if trade.stop_loss and latest_price <= trade.stop_loss:
                logger.error(
                    "🛑 WEBSOCKET SL HIT: %s price=%.4f sl=%.4f (immediate close)",
                    self.symbol, latest_price, trade.stop_loss,
                )
                if not trade.closed:
                    # Run close asynchronously without blocking WebSocket
                    asyncio.create_task(self._execute_close(trade, latest_price, reason="stop_loss_ws"))
                    trade.closed = True
                return
            
            # TP check (secondary — on every tick)
            if trade.take_profit and latest_price >= trade.take_profit:
                logger.info(
                    "💰 WEBSOCKET TP HIT: %s price=%.4f tp=%.4f (immediate close)",
                    self.symbol, latest_price, trade.take_profit,
                )
                if not trade.closed:
                    asyncio.create_task(self._execute_close(trade, latest_price, reason="take_profit_ws"))
                    trade.closed = True
                return
            
            # Update trailing stops (every tick for maximum precision)
            self.risk.update_trailing_stops(self.symbol, latest_price)
            
            # Update peak price (for drawdown calculation)
            if latest_price > trade.peak_price:
                trade.peak_price = latest_price
            
        except Exception as exc:
            logger.warning("WebSocket message handler error: %s", exc)

    # NOTE: Old _monitor_ticks_for_sl() method removed (REST polling)
    # Replaced by real-time WebSocket handler above (SUB-MILLISECOND latency)

    async def _generate_signals_loop(self, interval_seconds: int):
        """
        Signal generation loop (uses 5-min candles for strategy).
        Separate from SL monitoring to avoid conflicts.
        """
        logger.info("Starting signal generation loop (interval=%ds)", interval_seconds)
        
        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                logger.error("Tick error: %s", exc, exc_info=True)
                try:
                    send(f"⚠️ ERROR: {str(exc)}")
                except Exception:
                    pass
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
            logger.warning("Insufficient data in buffer (%d bars)", len(df) if df is not None else 0)
            return

        latest_price = float(df["close"].iloc[-1])

        # ── Check trailing stop / TP / SL ────
        if self._current_trade and not self._current_trade.closed:
            self.risk.update_trailing_stops(self.symbol, latest_price)

            exit_trade = (
                self.risk.should_exit_by_stop(self.symbol, latest_price) or
                self.risk.should_exit_by_tp(self.symbol, latest_price)
            )
            if exit_trade and not exit_trade.closed:
                if exit_trade.stop_loss and latest_price <= exit_trade.stop_loss:
                    logger.warning(
                        "🛑 STOP-LOSS HIT: %s price=%.4f sl=%.4f",
                        self.symbol, latest_price, exit_trade.stop_loss,
                    )
                    reason = "stop_loss"
                elif exit_trade.take_profit and latest_price >= exit_trade.take_profit:
                    logger.info(
                        "💰 TAKE-PROFIT HIT: %s price=%.4f tp=%.4f",
                        self.symbol, latest_price, exit_trade.take_profit,
                    )
                    reason = "take_profit"
                else:
                    reason = "risk_mgr"
                await self._execute_close(exit_trade, latest_price, reason=reason)
                return

        # ── Generate signal ───────────────────
        # If strategy requests multi-timeframe confirmation, fetch HTF candles
        htf_df = None
        try:
            if getattr(self.strategy, "params", {}).get("mtf_confirm"):
                htf_res = self.strategy.params.get("htf_resolution", self.resolution * 3)
                end_htf = end
                start_htf = end_htf - int(htf_res) * 60 * 200
                h_candles = await self.rest.get_ohlcv(self.symbol, htf_res, start_htf, end_htf)
                if h_candles:
                    rows = [
                        {
                            "timestamp": c.timestamp,
                            "open": c.open,
                            "high": c.high,
                            "low": c.low,
                            "close": c.close,
                            "volume": c.volume,
                        }
                        for c in h_candles
                    ]
                    htf_df = pd.DataFrame(rows)
                    htf_df["timestamp"] = pd.to_datetime(htf_df["timestamp"], unit="ms")
                    htf_df = htf_df.set_index("timestamp").sort_index()
        except Exception:
            htf_df = None

        signal = self.strategy.generate_signal(df, self.symbol, htf_df)
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
                await self._execute_close(
                    self._current_trade, latest_price, reason="signal_flip"
                )

        # ── Open new position ─────────────────
        if signal.type in (SignalType.LONG, SignalType.SHORT):
            if not self.risk.check_signal(signal):
                logger.info("Signal blocked by risk manager")
                return

            size_usd = self.risk.calculate_position_size(signal, latest_price, self.symbol)

            # Convert USD notional → integer lots
            size_lots = self.rest.usd_to_lots(self.symbol, size_usd, latest_price)

            if size_lots < 1:
                logger.warning(
                    "Position size too small to place (%.2f USD → %d lots). "
                    "Increase capital or reduce risk_per_trade.",
                    size_usd,
                    size_lots,
                )
                return

            logger.info(
                "Sizing: %.2f USD → %d lots (price=%.4f)",
                size_usd, size_lots, latest_price,
            )
            await self._execute_entry(signal, size_lots, latest_price)

    # ── Order helpers ─────────────────────────

    async def _execute_entry(self, signal: Signal, size_lots: int, price: float):
        """
        Place bracket order (entry + SL + TP on exchange).
        This is MUCH safer than software SL because:
        - No delay (sub-millisecond execution on exchange)
        - No slippage from waiting for bot checks
        - Native Delta support for bracket orders
        """
        side = OrderSide.BUY if signal.type == SignalType.LONG else OrderSide.SELL
        client_id = str(uuid.uuid4())[:8]

        try:
            # Use bracket order: entry + SL + TP simultaneously
            result = await self.rest.place_bracket_order(
                product_id=self.product_id,
                side=side,
                size=size_lots,
                entry_price=None,  # Market order
                stop_loss_price=signal.stop_loss or None,
                take_profit_price=signal.take_profit or None,
                client_order_id=client_id,
            )
        except Exception as exc:
            logger.error("Bracket order placement failed: %s", exc)
            return

        # Record the trade
        trade = TradeRecord(
            symbol=self.symbol,
            side="long" if side == OrderSide.BUY else "short",
            entry_price=price,
            size=size_lots,
            stop_loss=signal.stop_loss or 0.0,
            take_profit=signal.take_profit or 0.0,
            entry_time=datetime.utcnow(),
            order_id=result.get("id", ""),  # Main entry order ID
            peak_price=price,
        )
        self._current_trade = trade
        self.risk.register_trade(trade)

        logger.info(
            "🔲 BRACKET ENTRY: %s %s lots=%d entry=%.4f sl=%.4f tp=%.4f (exchange-managed)",
            trade.side, self.symbol, size_lots, price,
            signal.stop_loss or 0, signal.take_profit or 0,
        )
        try:
            # Send structured trade alert
            send_trade_alert(trade)
        except Exception:
            try:
                send(
                    f"🚀 ENTRY {trade.side.upper()} {self.symbol}\n"
                    f"Price: {price:.2f}\n"
                    f"Size: {size_lots} lots"
                )
            except Exception:
                pass

    async def _place_stop_order(self, signal: Signal, size_lots: int, entry_price: float):
        is_long = signal.type == SignalType.LONG
        sl_side = OrderSide.SELL if is_long else OrderSide.BUY
        sl_order = Order(
            product_id=self.product_id,
            side=sl_side,
            order_type=OrderType.STOP_MARKET,
            size=size_lots,              # ← integer lots
            stop_price=round(signal.stop_loss, 2),
            reduce_only=True,
        )
        try:
            await self.rest.place_order(sl_order)
            logger.info("Stop-loss order placed @ %.4f", signal.stop_loss)
        except Exception as exc:
            logger.warning("Stop order failed (will manage in software): %s", exc)

    async def _force_close_position(self, position: "Position"):
        """Emergency close for orphaned positions after restart."""
        close_side = OrderSide.SELL if position.size > 0 else OrderSide.BUY
        close_size = int(abs(position.size))
        order = Order(
            product_id=position.product_id,
            side=close_side,
            order_type=OrderType.MARKET,
            size=close_size,
            reduce_only=True,
        )
        try:
            result = await self.rest.place_order(order)
            logger.error(
                "🚨 EMERGENCY CLOSE: %s %d lots @ market (restart recovery) → order_id=%s",
                self.symbol, close_size, result.order_id,
            )
            try:
                send(
                    f"🚨 EMERGENCY CLOSE (restart recovery)\n"
                    f"Symbol: {self.symbol}\n"
                    f"Size: {close_size} lots\n"
                    f"Order ID: {result.order_id}"
                )
            except Exception:
                pass
        except Exception as exc:
            logger.error("Emergency close failed: %s", exc)

    async def _execute_close(self, trade: TradeRecord, price: float, reason: str):
        close_side = OrderSide.SELL if trade.side == "long" else OrderSide.BUY
        order = Order(
            product_id=self.product_id,
            side=close_side,
            order_type=OrderType.MARKET,
            size=int(trade.size),        # ← ensure integer
            reduce_only=True,
        )
        try:
            await self.rest.place_order(order)
        except Exception as exc:
            logger.error("Close order failed: %s", exc)
            return

        await self.rest.cancel_all_orders(self.product_id)

        now = datetime.utcnow()
        self.risk.record_trade_close(trade, price, now)
        self.trade_logger.log(trade)
        try:
            send(
                f"❌ EXIT {trade.side.upper()} {self.symbol}\n"
                f"Exit Price: {trade.exit_price:.2f}\n"
                f"PnL: {trade.realised_pnl:.2f}"
            )
        except Exception:
            pass
        self._current_trade = None

        logger.info(
            "EXIT (%s): %s %s @ %.4f pnl=%.2f",
            reason, trade.side, self.symbol, price, trade.realised_pnl,
        )

    def stop(self):
        self._running = False
        logger.info("Execution engine stopping…")
