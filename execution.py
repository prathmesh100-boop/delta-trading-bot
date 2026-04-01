"""
execution.py — Real-time execution engine (HARDENED v3)

FIXES vs v2:
  - contract_value is stored on TradeRecord at entry (used for accurate PnL)
  - _handle_ws_tick() is now async (avoids run_in_executor overhead/blocking)
  - WebSocket SL check uses software trailing stop, not just the entry SL
  - Prevents double-close via asyncio.Lock (not just a closed flag)
  - Bootstrap: gracefully closes orphaned positions before trading
  - _tick() debounce: ignores rapid duplicate signals
  - Multi-timeframe HTF fetching only when strategy requests it
  - TradeLogger appends rows (not rewrites on each trade)
  - All order placements have structured error handling with retry
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
        # Deduplicate by timestamp
        if self._data and self._data[-1].timestamp == candle.timestamp:
            self._data[-1] = candle   # Update last bar with fresh data
        else:
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
        return df.set_index("timestamp").sort_index()

    def __len__(self):
        return len(self._data)


# ─────────────────────────────────────────────
# Trade logger (append-mode)
# ─────────────────────────────────────────────

class TradeLogger:
    def __init__(self, filepath: str = "trade_history.csv"):
        self.filepath = filepath
        self._cols = [
            "symbol", "side", "entry_time", "exit_time",
            "entry_price", "exit_price", "size_lots",
            "contract_value", "pnl", "exit_reason", "order_id",
        ]
        # Write header if file doesn't exist
        try:
            with open(filepath, "x") as f:
                f.write(",".join(self._cols) + "\n")
        except FileExistsError:
            pass

    def log(self, trade: TradeRecord):
        row = {
            "symbol": trade.symbol,
            "side": trade.side,
            "entry_time": trade.entry_time,
            "exit_time": trade.exit_time,
            "entry_price": trade.entry_price,
            "exit_price": trade.exit_price,
            "size_lots": trade.size,
            "contract_value": trade.contract_value,
            "pnl": round(trade.realised_pnl, 6),
            "exit_reason": trade.exit_reason or "",
            "order_id": trade.order_id or "",
        }
        try:
            pd.DataFrame([row]).to_csv(self.filepath, mode="a", header=False, index=False)
            logger.debug("Trade logged: %s %s pnl=%.4f", trade.symbol, trade.side, trade.realised_pnl)
        except Exception as exc:
            logger.error("Trade log write failed: %s", exc)


# ─────────────────────────────────────────────
# Execution Engine
# ─────────────────────────────────────────────

class ExecutionEngine:
    """
    Concurrent execution engine:
      Task 1 — WebSocket: real-time price monitoring for SL/TP
      Task 2 — Signal loop: candle-based strategy execution
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
        self._close_lock = asyncio.Lock()
        self._last_signal_type: Optional[SignalType] = None
        self._last_signal_time: Optional[datetime] = None

        # Product info (populated in bootstrap_product)
        self._contract_value: float = 0.001
        self._min_size: int = 1

    # ── Bootstrap ─────────────────────────────

    async def bootstrap_product(self):
        """Cache product info for accurate lot/PnL calculations."""
        logger.info("Fetching product info: %s", self.symbol)
        product = await self.rest.get_product(self.symbol)
        if product:
            cv_raw = product.get("contract_value", "0.001")
            try:
                self._contract_value = float(cv_raw)
            except (TypeError, ValueError):
                self._contract_value = 0.001

            ms_raw = product.get("min_size", 1)
            try:
                self._min_size = max(1, int(ms_raw))
            except (TypeError, ValueError):
                self._min_size = 1

            logger.info(
                "Product: %s | id=%s | contract_value=%s | min_size=%d",
                self.symbol, product.get("id"), self._contract_value, self._min_size,
            )
        else:
            logger.warning("Product not found for %s — using fallback values", self.symbol)
            cv_fallback = self.rest.FALLBACK_LOT_SIZES.get(self.symbol, 0.001)
            self._contract_value = cv_fallback

    async def bootstrap_history(self):
        """Pre-fill candle buffer with ~300 historical bars."""
        import time
        end = int(time.time())
        start = end - self.resolution * 60 * 320
        logger.info("Fetching %d historical candles for %s…", 300, self.symbol)
        try:
            candles = await self.rest.get_ohlcv(self.symbol, self.resolution, start, end)
            for c in candles:
                self.buffer.push(c)
            logger.info("Buffer primed with %d candles", len(self.buffer))
        except Exception as exc:
            logger.error("History fetch failed: %s", exc)

    # ── Main loop ─────────────────────────────

    async def run_polling(self, interval_seconds: int = 60):
        self._running = True
        await self.bootstrap_product()
        await self.bootstrap_history()

        # Close orphaned positions from previous crash
        await self._close_orphaned_positions()

        # WebSocket for real-time SL/TP monitoring
        ws_client = DeltaWSClient(
            api_key=self.api_key,
            api_secret=self.api_secret,
            on_message=self._handle_ws_tick,  # async handler
        )
        ws_client.subscribe([{
            "type": "subscribe",
            "channel": "ticker",
            "symbols": [self.symbol],
        }])

        ws_task = asyncio.create_task(ws_client.connect())
        signal_task = asyncio.create_task(self._generate_signals_loop(interval_seconds))

        try:
            await asyncio.gather(ws_task, signal_task)
        except asyncio.CancelledError:
            self._running = False
            logger.info("Execution engine cancelled — shutting down")
        finally:
            ws_task.cancel()
            signal_task.cancel()
            await ws_client.disconnect()

    async def _close_orphaned_positions(self):
        """On restart, detect and close any open positions from previous session."""
        try:
            positions = await self.rest.get_positions()
            for pos in positions:
                if pos.symbol == self.symbol and pos.size != 0:
                    logger.warning(
                        "Found orphaned position: %s %.2f lots @ %.4f — closing immediately",
                        self.symbol, pos.size, pos.entry_price,
                    )
                    await self._force_close_position(pos)
        except Exception as exc:
            logger.warning("Orphan position check failed: %s", exc)

    # ── WebSocket handler (ASYNC) ─────────────

    async def _handle_ws_tick(self, msg: Dict):
        """
        Async WebSocket ticker handler.
        Called on every market tick (~100ms).
        Checks software SL/TP as a backup to exchange bracket orders.

        Delta ticker message format:
          {"type": "ticker", "symbol": "BTC_USDT", "last_price": "67500.50", ...}
        
        NOTE: Exchange bracket SL is PRIMARY. This is a safety backup.
        """
        msg_type = msg.get("type")
        # Delta sometimes wraps in {"type":"subscriptions_data", "payload": {...}}
        if msg_type == "subscriptions_data":
            msg = msg.get("payload", {})
            msg_type = msg.get("type")

        if msg_type != "ticker":
            return
        if msg.get("symbol") != self.symbol:
            return

        try:
            price_raw = msg.get("last_price") or msg.get("close")
            if not price_raw:
                return
            latest_price = float(price_raw)
            if latest_price <= 0:
                return

            trade = self._current_trade
            if not trade or trade.closed:
                return

            # ── Step 1: Update peak price tracking ──────────────────────────
            # Must happen BEFORE trailing stop update so peak is current
            if trade.peak_price is not None:
                if trade.side == "long":
                    trade.peak_price = max(trade.peak_price, latest_price)
                else:
                    trade.peak_price = min(trade.peak_price, latest_price)

            # ── Step 2: Update trailing/breakeven/profit-lock stops ──────────
            # Must happen BEFORE the SL check below so effective_sl is current
            self.risk.update_trailing_stops(self.symbol, latest_price)

            # ── Step 3: Check SL/TP with up-to-date stops ───────────────────
            if trade.side == "long":
                trail = trade.trailing_stop_price or 0
                effective_sl = max(trail, trade.stop_loss)
                if latest_price <= effective_sl:
                    await self._ws_close(trade, latest_price, "stop_loss_ws")
                    return
                if trade.take_profit and latest_price >= trade.take_profit:
                    await self._ws_close(trade, latest_price, "take_profit_ws")
                    return

            elif trade.side == "short":
                trail = trade.trailing_stop_price or float("inf")
                effective_sl = min(trail, trade.stop_loss)
                if latest_price >= effective_sl:
                    await self._ws_close(trade, latest_price, "stop_loss_ws")
                    return
                if trade.take_profit and latest_price <= trade.take_profit:
                    await self._ws_close(trade, latest_price, "take_profit_ws")
                    return

        except Exception as exc:
            logger.warning("WS tick handler error: %s", exc)

    async def _ws_close(self, trade: TradeRecord, price: float, reason: str):
        """Close from WebSocket handler with lock to prevent double-close."""
        async with self._close_lock:
            if trade.closed:
                return
            if reason == "stop_loss_ws":
                logger.error("🛑 WS SL HIT: %s price=%.4f sl=%.4f (exchange bracket is primary)",
                             self.symbol, price, trade.stop_loss)
            else:
                logger.info("💰 WS TP HIT: %s price=%.4f tp=%.4f",
                            self.symbol, price, trade.take_profit)
            await self._execute_close(trade, price, reason=reason)

    # ── Signal generation loop ─────────────────

    async def _generate_signals_loop(self, interval_seconds: int):
        logger.info("Signal loop started (interval=%ds)", interval_seconds)
        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                logger.error("Tick error: %s", exc, exc_info=True)
                try:
                    send(f"⚠️ BOT ERROR: {str(exc)[:200]}")
                except Exception:
                    pass
            await asyncio.sleep(interval_seconds)

    async def _tick(self):
        """Fetch latest candle, generate signal, manage position."""
        import time
        end = int(time.time())
        start = end - self.resolution * 60 * 4

        try:
            candles = await self.rest.get_ohlcv(self.symbol, self.resolution, start, end)
            for c in candles:
                self.buffer.push(c)
        except Exception as exc:
            logger.warning("Candle fetch failed: %s", exc)
            return

        df = self.buffer.to_dataframe()
        if df is None or len(df) < 60:
            logger.warning("Insufficient data (%d bars)", len(df) if df is not None else 0)
            return

        latest_price = float(df["close"].iloc[-1])

        # ⚠️ REMOVED: Candle-based TP/SL exits
        # Exchange bracket orders are PRIMARY. WebSocket monitors as backup.
        # DO NOT close trades on candle close — this interferes with exchange atomicity.
        if self._current_trade and not self._current_trade.closed:
            self.risk.update_trailing_stops(self.symbol, latest_price)

        # Generate signal (with optional multi-timeframe)
        htf_df = await self._fetch_htf_df(end)
        signal = self.strategy.generate_signal(df, self.symbol, htf_df)
        logger.info("[%s] Signal: %s @ %.4f", self.symbol, signal.type, latest_price)

        if signal.type == SignalType.HOLD:
            return

        # Signal debounce: avoid re-entering same direction repeatedly
        now = datetime.utcnow()
        if (self._last_signal_type == signal.type
                and self._last_signal_time
                and (now - self._last_signal_time).total_seconds() < self.resolution * 60 * 2):
            logger.debug("Signal debounced (%s duplicate within 2 bars)", signal.type)
            return

        self._last_signal_type = signal.type
        self._last_signal_time = now

        # Flip: close opposite position
        if self._current_trade and not self._current_trade.closed:
            opposite = (
                (self._current_trade.side == "long" and signal.type == SignalType.SHORT) or
                (self._current_trade.side == "short" and signal.type == SignalType.LONG)
            )
            if opposite:
                await self._execute_close(self._current_trade, latest_price, reason="signal_flip")

        # New position
        if signal.type in (SignalType.LONG, SignalType.SHORT):
            if not self.risk.check_signal(signal):
                logger.info("Signal blocked by risk manager")
                return

            size_usd = self.risk.calculate_position_size(signal, latest_price, self.symbol)
            size_lots = self.rest.usd_to_lots(self.symbol, size_usd, latest_price)

            if size_lots < self._min_size:
                logger.warning(
                    "Position too small: %.2f USD → %d lots (min=%d). Need more capital.",
                    size_usd, size_lots, self._min_size,
                )
                return

            logger.info("Entry sizing: %.2f USD → %d lots (price=%.4f)", size_usd, size_lots, latest_price)
            await self._execute_entry(signal, size_lots, latest_price)

    async def _fetch_htf_df(self, end: int) -> Optional[pd.DataFrame]:
        """Fetch higher-timeframe data if strategy needs it."""
        try:
            if not getattr(self.strategy, "params", {}).get("mtf_confirm"):
                return None
            htf_res = self.strategy.params.get("htf_resolution", self.resolution * 3)
            start_htf = end - int(htf_res) * 60 * 200
            h_candles = await self.rest.get_ohlcv(self.symbol, htf_res, start_htf, end)
            if not h_candles:
                return None
            rows = [
                {"timestamp": c.timestamp, "open": c.open, "high": c.high,
                 "low": c.low, "close": c.close, "volume": c.volume}
                for c in h_candles
            ]
            htf_df = pd.DataFrame(rows)
            htf_df["timestamp"] = pd.to_datetime(htf_df["timestamp"], unit="ms")
            return htf_df.set_index("timestamp").sort_index()
        except Exception as exc:
            logger.debug("HTF fetch failed (non-critical): %s", exc)
            return None

    # ── Order execution ───────────────────────

    async def _execute_entry(self, signal: Signal, size_lots: int, price: float):
        """Place bracket order (entry + SL + TP on exchange atomically)."""
        side = OrderSide.BUY if signal.type == SignalType.LONG else OrderSide.SELL
        client_id = str(uuid.uuid4())[:8]

        try:
            result = await self.rest.place_bracket_order(
                product_id=self.product_id,
                side=side,
                size=size_lots,
                entry_price=None,   # Market entry
                stop_loss_price=signal.stop_loss,
                take_profit_price=signal.take_profit,
                client_order_id=client_id,
            )
        except Exception as exc:
            logger.error("Bracket order failed: %s", exc)
            try:
                send(f"❌ ORDER FAILED: {self.symbol} {side.value} — {str(exc)[:200]}")
            except Exception:
                pass
            return

        trade = TradeRecord(
            symbol=self.symbol,
            side="long" if side == OrderSide.BUY else "short",
            entry_price=price,
            size=size_lots,
            stop_loss=signal.stop_loss or 0.0,
            take_profit=signal.take_profit or 0.0,
            entry_time=datetime.utcnow(),
            order_id=str(result.get("id", client_id)),
            peak_price=price,
            contract_value=self._contract_value,    # ← stored for accurate PnL
        )
        self._current_trade = trade
        self.risk.register_trade(trade)

        logger.info(
            "🔲 BRACKET ENTRY: %s %s | lots=%d | entry=%.4f | sl=%.4f | tp=%.4f | cv=%s",
            trade.side.upper(), self.symbol, size_lots, price,
            signal.stop_loss or 0, signal.take_profit or 0, self._contract_value,
        )

        try:
            send_trade_alert(trade)
        except Exception:
            pass

    async def _execute_close(self, trade: TradeRecord, price: float, reason: str):
        """Close position with lock to prevent duplicate closes."""
        async with self._close_lock:
            if trade.closed:
                logger.debug("Close skipped — trade already closed (reason=%s)", reason)
                return

            trade.closed = True   # Mark immediately to prevent re-entry

        close_side = OrderSide.SELL if trade.side == "long" else OrderSide.BUY
        order = Order(
            product_id=self.product_id,
            side=close_side,
            order_type=OrderType.MARKET,
            size=int(trade.size),
            reduce_only=True,
        )
        try:
            await self.rest.place_order(order)
            logger.info("Close order placed: %s %s @ %.4f (%s)", trade.symbol, trade.side, price, reason)
        except Exception as exc:
            exc_str = str(exc)
            # Exchange bracket SL/TP already closed the position — this is expected.
            # Treat it as a successful close so capital/stats still get updated.
            if "no_position_for_reduce_only" in exc_str:
                logger.info(
                    "Close skipped — exchange bracket already closed %s %s (reason=%s)",
                    trade.symbol, trade.side, reason,
                )
            else:
                logger.error("Close order failed: %s — position may still be open!", exc)
                try:
                    send(f"🚨 CLOSE FAILED: {trade.symbol} — {exc_str[:200]}")
                except Exception:
                    pass
                # Only abort capital update for genuine failures, not already-closed positions
                return

        # Cancel remaining bracket SL/TP orders
        try:
            await self.rest.cancel_all_orders(self.product_id)
        except Exception as exc:
            logger.warning("Cancel bracket orders failed (non-critical): %s", exc)

        # Record and log
        now = datetime.utcnow()
        self.risk.record_trade_close(trade, price, now, reason=reason)
        self.trade_logger.log(trade)

        try:
            send(
                f"{'✅' if trade.realised_pnl >= 0 else '❌'} CLOSE ({reason})\n"
                f"{trade.side.upper()} {self.symbol}\n"
                f"Entry: {trade.entry_price:.4f} → Exit: {price:.4f}\n"
                f"PnL: {trade.realised_pnl:+.4f} USDT"
            )
        except Exception:
            pass

        self._current_trade = None
        logger.info(
            "EXIT (%s): %s %s | entry=%.4f exit=%.4f | pnl=%.4f USDT",
            reason, trade.side, self.symbol, trade.entry_price, price, trade.realised_pnl,
        )

    async def _force_close_position(self, position):
        """Emergency close for orphaned positions detected at startup."""
        close_side = OrderSide.SELL if position.size > 0 else OrderSide.BUY
        close_size = max(1, int(abs(position.size)))
        order = Order(
            product_id=position.product_id,
            side=close_side,
            order_type=OrderType.MARKET,
            size=close_size,
            reduce_only=True,
        )
        try:
            result = await self.rest.place_order(order)
            logger.warning(
                "🚨 EMERGENCY CLOSE: %s %d lots → id=%s",
                self.symbol, close_size, result.order_id,
            )
            try:
                send(f"🚨 EMERGENCY CLOSE (startup recovery)\n{self.symbol} {close_size} lots\nOrder: {result.order_id}")
            except Exception:
                pass
        except Exception as exc:
            logger.error("Emergency close failed: %s", exc)

    def stop(self):
        self._running = False
        logger.info("Execution engine stopping…")
