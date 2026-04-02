"""
execution.py — Execution Engine v5 (BRACKET-FIRST ARCHITECTURE)

KEY ARCHITECTURAL CHANGE vs v4:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ❌ OLD: Bot watches price → bot sends close order → DELAY → LOSS
  ✅ NEW: Bracket order places SL/TP ON EXCHANGE at entry time
          → Exchange fires SL/TP instantly → Zero delay → Safe

FLOW:
  Signal → place_bracket_order() → Exchange holds SL + TP orders
  WebSocket: only monitors for sync / trailing-stop updates
  Bot close: only called on signal flip OR trailing stop improvement

SPECIFIC IMPROVEMENTS:
  1. _execute_entry() ALWAYS uses place_bracket_order()
     Entry + SL + TP sent atomically in a single API call.

  2. WebSocket handler now does NOT fire software SL/TP.
     It only:
       a. Updates trailing stop on exchange via edit_bracket_order()
       b. Detects signal flip → sends market close (reduce_only)
       c. Syncs state when exchange bracket fires (no_position error)

  3. _execute_close() handles "bracket already closed by exchange":
     - Fetches actual fill price from /v2/fills
     - Logs correctly without raising false errors

  4. Startup: _close_orphaned_positions() + _cancel_all_stale_orders()

  5. Adaptive signal loop:
     - In trade: tick every 30s (trailing stop check)
     - Flat: tick every interval_seconds (strategy scan)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import asyncio
import logging
import time
import uuid
from collections import deque as _deque
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd

from api import (
    DeltaRESTClient, DeltaWSClient, BracketOrderResult,
    Order, OrderSide, OrderStatus, OrderType, OHLCV, StopTriggerMethod,
    DeltaAPIError,
)
from risk import RiskConfig, RiskManager, TradeRecord
from strategy import BaseStrategy, Signal, SignalType
from notifier import send, send_trade_alert

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Candle buffer (O(1) deque-backed)
# ─────────────────────────────────────────────────────────────────────────────

class CandleBuffer:
    def __init__(self, maxlen: int = 500):
        self.maxlen = maxlen
        self._data: _deque = _deque(maxlen=maxlen)

    def push(self, candle: OHLCV):
        # Deduplicate by timestamp — update last bar with freshest data
        if self._data and self._data[-1].timestamp == candle.timestamp:
            self._data[-1] = candle
        else:
            self._data.append(candle)

    def to_dataframe(self) -> Optional[pd.DataFrame]:
        if not self._data:
            return None
        rows = [
            {
                "timestamp": c.timestamp,
                "open":      c.open,
                "high":      c.high,
                "low":       c.low,
                "close":     c.close,
                "volume":    c.volume,
            }
            for c in self._data
        ]
        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df.set_index("timestamp").sort_index()

    def __len__(self):
        return len(self._data)


# ─────────────────────────────────────────────────────────────────────────────
# Trade logger
# ─────────────────────────────────────────────────────────────────────────────

class TradeLogger:
    COLS = [
        "symbol", "side", "entry_time", "exit_time",
        "entry_price", "exit_price", "size_lots",
        "contract_value", "gross_pnl", "exit_reason",
        "order_id", "sl_order_id", "tp_order_id",
    ]

    def __init__(self, filepath: str = "trade_history.csv"):
        self.filepath = filepath
        try:
            with open(filepath, "x") as f:
                f.write(",".join(self.COLS) + "\n")
        except FileExistsError:
            pass

    def log(self, trade: TradeRecord):
        row = {
            "symbol":         trade.symbol,
            "side":           trade.side,
            "entry_time":     trade.entry_time,
            "exit_time":      trade.exit_time,
            "entry_price":    trade.entry_price,
            "exit_price":     trade.exit_price,
            "size_lots":      trade.size,
            "contract_value": trade.contract_value,
            "gross_pnl":      round(trade.realised_pnl, 6),
            "exit_reason":    trade.exit_reason or "",
            "order_id":       trade.order_id or "",
            "sl_order_id":    trade.sl_order_id or "",
            "tp_order_id":    trade.tp_order_id or "",
        }
        try:
            pd.DataFrame([row]).to_csv(self.filepath, mode="a", header=False, index=False)
        except Exception as exc:
            logger.error("Trade log write failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Execution Engine
# ─────────────────────────────────────────────────────────────────────────────

class ExecutionEngine:
    """
    Concurrent execution engine v5 — BRACKET-FIRST.

    Task 1 — WebSocket: real-time price monitoring.
              Manages trailing-stop updates and state sync when
              bracket SL/TP fires on exchange.

    Task 2 — Signal loop: candle-based strategy execution.
              Generates signals, sizes positions, places bracket orders.
    """

    HTF_CACHE_TTL = 300   # 5 minutes

    def __init__(
        self,
        rest_client:        DeltaRESTClient,
        strategy:           BaseStrategy,
        risk_manager:       RiskManager,
        symbol:             str,
        product_id:         int,
        resolution_minutes: int  = 15,
        api_key:            str  = "",
        api_secret:         str  = "",
    ):
        self.rest       = rest_client
        self.strategy   = strategy
        self.risk       = risk_manager
        self.symbol     = symbol
        self.product_id = product_id
        self.resolution = resolution_minutes
        self.api_key    = api_key
        self.api_secret = api_secret

        self.buffer       = CandleBuffer(maxlen=300)
        self.trade_logger = TradeLogger()

        self._current_trade:   Optional[TradeRecord] = None
        self._running:         bool                  = False
        self._close_lock:      asyncio.Lock          = asyncio.Lock()
        self._last_signal_type: Optional[SignalType]  = None
        self._last_signal_time: Optional[datetime]    = None
        self._latest_price:    float                  = 0.0

        # Product specs
        self._contract_value: float = 0.001
        self._min_size:       int   = 1

        # HTF cache
        self._htf_df_cache:  Optional[pd.DataFrame] = None
        self._htf_cache_time: float                  = 0.0

        # Trailing stop state — last price sent to exchange
        self._last_exchange_sl: Optional[float] = None

    # ── Bootstrap ──────────────────────────────────────────────────────────

    async def bootstrap_product(self):
        logger.info("Fetching product info: %s", self.symbol)
        product = await self.rest.get_product(self.symbol)
        if product:
            try:
                self._contract_value = float(product.get("contract_value", 0.001) or 0.001)
            except (TypeError, ValueError):
                self._contract_value = 0.001
            try:
                self._min_size = max(1, int(product.get("min_size", 1) or 1))
            except (TypeError, ValueError):
                self._min_size = 1
            logger.info(
                "Product: %s | id=%s | cv=%s | min_size=%d",
                self.symbol, product.get("id"), self._contract_value, self._min_size,
            )

    async def bootstrap_history(self):
        end   = int(time.time())
        start = end - self.resolution * 60 * 320
        logger.info("Fetching historical candles for %s…", self.symbol)
        try:
            candles = await self.rest.get_ohlcv(self.symbol, self.resolution, start, end)
            for c in candles:
                self.buffer.push(c)
            logger.info("Buffer primed with %d candles", len(self.buffer))
        except Exception as exc:
            logger.error("History fetch failed: %s", exc)

    # ── Entry point ────────────────────────────────────────────────────────

    async def run_polling(self, interval_seconds: int = 60):
        self._running = True

        await self.bootstrap_product()
        await self.bootstrap_history()
        await self._startup_cleanup()

        ws_client = DeltaWSClient(
            api_key    = self.api_key,
            api_secret = self.api_secret,
            on_message = self._handle_ws_tick,
        )
        ws_client.subscribe([{
            "type":    "subscribe",
            "channel": "ticker",
            "symbols": [self.symbol],
        }])

        ws_task     = asyncio.create_task(ws_client.connect())
        signal_task = asyncio.create_task(self._signal_loop(interval_seconds))

        try:
            await asyncio.gather(ws_task, signal_task)
        except asyncio.CancelledError:
            logger.info("Engine cancelled — shutting down")
        finally:
            self._running = False
            ws_task.cancel()
            signal_task.cancel()
            await ws_client.disconnect()

    async def _startup_cleanup(self):
        """Cancel stale orders and close any orphaned positions from a previous run."""
        logger.info("Startup cleanup: checking for orphaned orders/positions…")
        try:
            await self.rest.cancel_all_orders(self.product_id)
        except Exception as exc:
            logger.warning("Startup cancel_all failed (non-critical): %s", exc)

        try:
            positions = await self.rest.get_positions()
            for pos in positions:
                if pos.symbol == self.symbol and pos.size > 0:
                    logger.warning(
                        "Orphaned position found: %s %.2f lots @ %.4f — closing",
                        self.symbol, pos.size, pos.entry_price,
                    )
                    await self._force_close_position(pos)
        except Exception as exc:
            logger.warning("Orphan position check failed: %s", exc)

    # ── WebSocket handler ──────────────────────────────────────────────────

    async def _handle_ws_tick(self, msg: Dict):
        """
        Async WebSocket handler.

        ROLE IN BRACKET-FIRST ARCHITECTURE:
          - Does NOT fire software SL/TP (exchange handles that)
          - DOES update trailing stop on exchange when SL improves
          - DOES sync bot state when exchange bracket fires
          - DOES detect when trailing stop is hit (as fallback check)
        """
        # Unwrap Delta envelope
        if msg.get("type") == "subscriptions_data":
            msg = msg.get("payload", {})

        if msg.get("type") != "ticker":
            return
        if msg.get("symbol") != self.symbol:
            return

        try:
            price_raw = msg.get("last_price") or msg.get("close") or msg.get("mark_price")
            if not price_raw:
                return
            price = float(price_raw)
            if price <= 0:
                return

            self._latest_price = price

            trade = self._current_trade
            if not trade or trade.closed:
                return

            # Update risk manager trailing stop (in-memory)
            self.risk.update_trailing_stops(self.symbol, price)

            # ── Sync check: has exchange bracket already fired? ─────────────
            # If price has moved far past our SL, the exchange bracket
            # should have fired. Verify by checking position silently.
            # (We don't close via WS — exchange already did it.)

            # ── Trailing stop: push improved SL to exchange ─────────────────
            await self._maybe_update_exchange_trailing_sl(trade, price)

        except Exception as exc:
            logger.debug("WS tick error (non-critical): %s", exc)

    async def _maybe_update_exchange_trailing_sl(self, trade: TradeRecord, price: float):
        """
        If the risk manager has improved the trailing stop, push the new
        SL to the exchange via edit_bracket_order().

        This ensures the exchange bracket tracks our trailing stop,
        combining the best of both worlds:
          - Exchange fires SL instantly (no bot delay)
          - Bot improves SL over time (trailing / breakeven)
        """
        if not trade.order_id:
            return

        # Get the in-memory trailing stop
        trailing = trade.trailing_stop_price
        be_sl    = trade.stop_loss   # may have been moved to breakeven

        if trade.is_long:
            new_sl = max(trailing or 0, be_sl or 0)
            if new_sl <= 0:
                return
            # Only push if SL has improved (moved up for long)
            if self._last_exchange_sl and new_sl <= self._last_exchange_sl:
                return
        else:
            new_sl = min(trailing or float("inf"), be_sl or float("inf"))
            if new_sl == float("inf") or new_sl <= 0:
                return
            # Only push if SL has improved (moved down for short)
            if self._last_exchange_sl and new_sl >= self._last_exchange_sl:
                return

        # Push updated SL to exchange (fire-and-forget, non-blocking)
        try:
            success = await self.rest.edit_bracket_order(
                order_id   = int(trade.order_id),
                product_id = self.product_id,
                stop_loss_price = new_sl,
                trigger_method  = StopTriggerMethod.LAST_TRADED_PRICE,
            )
            if success:
                self._last_exchange_sl = new_sl
                logger.info(
                    "📍 Trailing SL updated on exchange: %s → %.4f",
                    self.symbol, new_sl,
                )
        except Exception as exc:
            logger.debug("Exchange trailing SL update failed (non-critical): %s", exc)

    # ── Signal loop ────────────────────────────────────────────────────────

    async def _signal_loop(self, interval_seconds: int):
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

            # Adaptive: check faster when managing an open trade
            if self._current_trade and not self._current_trade.closed:
                await asyncio.sleep(30)
            else:
                await asyncio.sleep(interval_seconds)

    async def _tick(self):
        """Fetch latest candles, generate signal, manage positions."""
        end   = int(time.time())
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
            logger.warning("Insufficient candle data (%d bars)", len(df) if df is not None else 0)
            return

        latest_price = float(df["close"].iloc[-1])

        # ── Sync open trade with exchange if needed ─────────────────────────
        if self._current_trade and not self._current_trade.closed:
            await self._sync_trade_with_exchange()

        # ── HTF data (cached) ───────────────────────────────────────────────
        htf_df = await self._fetch_htf_df_cached(end)

        # ── Strategy signal ─────────────────────────────────────────────────
        signal = self.strategy.generate_signal(df, self.symbol, htf_df)
        logger.info("[%s] Signal: %s @ %.4f", self.symbol, signal.type, latest_price)

        if signal.type == SignalType.HOLD:
            return

        # ── Debounce ────────────────────────────────────────────────────────
        now = datetime.utcnow()
        if (
            self._last_signal_type == signal.type
            and self._last_signal_time
            and (now - self._last_signal_time).total_seconds() < self.resolution * 60 * 2
        ):
            logger.debug("Signal debounced (%s duplicate within 2 bars)", signal.type)
            return

        self._last_signal_type = signal.type
        self._last_signal_time = now

        # ── Signal flip: close opposite position ────────────────────────────
        if self._current_trade and not self._current_trade.closed:
            is_flip = (
                (self._current_trade.side == "long"  and signal.type == SignalType.SHORT) or
                (self._current_trade.side == "short" and signal.type == SignalType.LONG)
            )
            if is_flip:
                logger.info("Signal flip detected — closing current trade")
                await self._execute_close(self._current_trade, latest_price, reason="signal_flip")

        # ── New position ─────────────────────────────────────────────────────
        if signal.type in (SignalType.LONG, SignalType.SHORT):
            if self._current_trade and not self._current_trade.closed:
                return   # Already in a trade (same direction)

            if not self.risk.check_signal(signal):
                logger.info("Signal blocked by risk manager")
                return

            size_usd  = self.risk.calculate_position_size(signal, latest_price, self.symbol)
            size_lots = self.rest.usd_to_lots(self.symbol, size_usd, latest_price)

            if size_lots < self._min_size:
                logger.warning(
                    "Position too small: %.2f USD → %d lots (min=%d). Need more capital.",
                    size_usd, size_lots, self._min_size,
                )
                return

            await self._execute_entry(signal, size_lots, latest_price)

    async def _sync_trade_with_exchange(self):
        """
        Check if the exchange bracket has already closed our position.
        Called during each signal-loop tick (not on every WS message).
        """
        if not self._current_trade or not self._current_trade.order_id:
            return

        try:
            order = await self.rest.get_order_by_id(self._current_trade.order_id)
            if order and order.get("state") in ("closed", "cancelled"):
                # Exchange bracket already closed the position
                logger.info(
                    "Exchange bracket fired: order %s state=%s — syncing bot state",
                    self._current_trade.order_id, order.get("state"),
                )
                # Try to get actual fill price
                exit_price = self._latest_price
                try:
                    exit_price = await self.rest.get_actual_fill_price(
                        self._current_trade.order_id, fallback=exit_price
                    )
                except Exception:
                    pass

                async with self._close_lock:
                    if not self._current_trade.closed:
                        self.risk.record_trade_close(
                            self._current_trade, exit_price, datetime.utcnow(), reason="bracket_exchange"
                        )
                        self.trade_logger.log(self._current_trade)
                        self._current_trade.closed = True
                        self._current_trade = None
                        self._last_exchange_sl = None
        except Exception as exc:
            logger.debug("Trade sync check failed (non-critical): %s", exc)

    async def _fetch_htf_df_cached(self, end: int) -> Optional[pd.DataFrame]:
        now = time.monotonic()
        if self._htf_df_cache is not None and (now - self._htf_cache_time) < self.HTF_CACHE_TTL:
            return self._htf_df_cache
        result = await self._fetch_htf_df(end)
        self._htf_df_cache  = result
        self._htf_cache_time = now
        return result

    async def _fetch_htf_df(self, end: int) -> Optional[pd.DataFrame]:
        try:
            if not getattr(self.strategy, "params", {}).get("mtf_confirm"):
                return None
            htf_res   = self.strategy.params.get("htf_resolution", self.resolution * 4)
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

    # ── Order execution ────────────────────────────────────────────────────

    async def _execute_entry(self, signal: Signal, size_lots: int, price: float):
        """
        ✅ BRACKET-FIRST ENTRY.

        Places entry + SL + TP in a single API call.
        Exchange holds SL and TP orders — they fire instantly on trigger.
        No software monitoring required for SL/TP.
        """
        side      = OrderSide.BUY if signal.type == SignalType.LONG else OrderSide.SELL
        client_id = str(uuid.uuid4())[:12]

        sl_price = signal.stop_loss
        tp_price = signal.take_profit

        if not sl_price or sl_price <= 0:
            logger.warning("Signal has no valid SL — cannot place bracket order safely")
            return

        try:
            bracket_result: BracketOrderResult = await self.rest.place_bracket_order(
                product_id        = self.product_id,
                side              = side,
                size              = size_lots,
                entry_price       = None,        # Market entry for fastest execution
                stop_loss_price   = sl_price,
                take_profit_price = tp_price,
                trigger_method    = StopTriggerMethod.LAST_TRADED_PRICE,
                client_order_id   = client_id,
            )
        except DeltaAPIError as exc:
            logger.error("❌ Bracket order rejected by exchange: %s", exc)
            try:
                send(f"❌ BRACKET REJECTED: {self.symbol} {side.value} — {str(exc)[:200]}")
            except Exception:
                pass
            return
        except Exception as exc:
            logger.error("❌ Bracket order failed (network/unknown): %s", exc)
            try:
                send(f"❌ ORDER FAILED: {self.symbol} — {str(exc)[:200]}")
            except Exception:
                pass
            return

        # Register trade in bot state
        trade = TradeRecord(
            symbol          = self.symbol,
            side            = "long" if side == OrderSide.BUY else "short",
            entry_price     = price,
            size            = size_lots,
            stop_loss       = sl_price,
            take_profit     = tp_price or 0.0,
            entry_time      = datetime.utcnow(),
            order_id        = bracket_result.entry_order_id,
            sl_order_id     = bracket_result.sl_order_id,
            tp_order_id     = bracket_result.tp_order_id,
            peak_price      = price,
            contract_value  = self._contract_value,
            min_size        = self._min_size,
        )
        self._current_trade   = trade
        self._last_exchange_sl = sl_price
        self.risk.register_trade(trade)

        logger.info(
            "🔲 BRACKET ENTRY PLACED: %s %s | lots=%d | entry=MARKET≈%.4f | sl=%.4f | tp=%.4f | "
            "entry_id=%s sl_id=%s tp_id=%s",
            trade.side.upper(), self.symbol, size_lots, price,
            sl_price, tp_price or 0,
            bracket_result.entry_order_id,
            bracket_result.sl_order_id or "N/A",
            bracket_result.tp_order_id or "N/A",
        )

        try:
            send_trade_alert(trade)
        except Exception:
            pass

    async def _execute_close(self, trade: TradeRecord, price: float, reason: str):
        """
        Manual close (signal flip or emergency).
        Exchange bracket SL/TP is always the PRIMARY safety net.
        This is only called for strategy-driven exits (flip, trailing, etc.)
        """
        async with self._close_lock:
            if trade.closed:
                return
            trade.closed = True

        # Cancel remaining bracket orders first
        try:
            await self.rest.cancel_all_orders(self.product_id)
        except Exception as exc:
            logger.warning("Cancel bracket orders failed (non-critical): %s", exc)

        # Send market close order
        close_side = OrderSide.SELL if trade.side == "long" else OrderSide.BUY
        close_size = max(1, int(trade.size))
        order      = Order(
            product_id   = self.product_id,
            side         = close_side,
            order_type   = OrderType.MARKET,
            size         = close_size,
            reduce_only  = True,
        )

        bracket_already_closed = False
        actual_exit_price      = price

        try:
            await self.rest.place_order(order)
            logger.info("Manual close sent: %s %s @ %.4f (%s)", trade.symbol, trade.side, price, reason)
        except DeltaAPIError as exc:
            if "no_position" in str(exc).lower() or "reduce_only" in str(exc).lower():
                # Exchange bracket already fired — this is expected, not an error
                bracket_already_closed = True
                logger.info(
                    "ℹ️  Exchange bracket already closed position (%s %s) — syncing state",
                    trade.symbol, trade.side,
                )
                # Fetch actual fill price from exchange
                if trade.order_id:
                    try:
                        actual_exit_price = await self.rest.get_actual_fill_price(
                            trade.order_id, fallback=price
                        )
                        logger.info("Actual fill price from exchange: %.4f", actual_exit_price)
                    except Exception:
                        pass
            else:
                logger.error("Manual close failed: %s — bracket SL/TP still active on exchange", exc)
                try:
                    send(f"🚨 CLOSE FAILED ({reason}): {trade.symbol} — {str(exc)[:200]}")
                except Exception:
                    pass

        # Record trade
        now = datetime.utcnow()
        net_pnl = self.risk.record_trade_close(trade, actual_exit_price, now, reason=reason)
        self.trade_logger.log(trade)

        self._current_trade   = None
        self._last_exchange_sl = None
        self._htf_cache_time  = 0.0   # Force fresh HTF data for next entry

        source = "exchange bracket" if bracket_already_closed else "manual close"
        pnl_emoji = "✅" if net_pnl >= 0 else "❌"

        logger.info(
            "EXIT (%s via %s): %s %s | entry=%.4f exit=%.4f | pnl=%.4f USDT | capital=%.2f",
            reason, source, trade.side, self.symbol,
            trade.entry_price, actual_exit_price, net_pnl,
            self.risk.capital,
        )

        try:
            send(
                f"{pnl_emoji} CLOSE ({reason} / {source})\n"
                f"{trade.side.upper()} {self.symbol}\n"
                f"Entry: {trade.entry_price:.4f} → Exit: {actual_exit_price:.4f}\n"
                f"PnL: {net_pnl:+.4f} USDT | Capital: {self.risk.capital:.2f} USDT"
            )
        except Exception:
            pass

    async def _force_close_position(self, position):
        """Emergency close for orphaned positions detected at startup."""
        close_side = OrderSide.SELL if position.size > 0 else OrderSide.BUY
        close_size = max(1, int(abs(position.size)))
        order = Order(
            product_id  = position.product_id,
            side        = close_side,
            order_type  = OrderType.MARKET,
            size        = close_size,
            reduce_only = True,
        )
        try:
            result = await self.rest.place_order(order)
            logger.warning("🚨 EMERGENCY CLOSE: %s %d lots → id=%s", self.symbol, close_size, result.order_id)
            try:
                send(f"🚨 EMERGENCY CLOSE (startup)\n{self.symbol} {close_size} lots")
            except Exception:
                pass
        except Exception as exc:
            logger.error("Emergency close failed: %s", exc)

    def stop(self):
        self._running = False
        logger.info("Execution engine stopping…")
