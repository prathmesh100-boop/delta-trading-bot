"""
execution.py — Production Execution Engine

Architecture:
  1. Bootstrap: fetch candle history, close orphaned positions, set leverage
  2. Concurrent tasks:
     a. WebSocket task: real-time price ticks, SL/TP monitoring, trailing stops
     b. Signal task: candle-based signal generation every N minutes
  3. Entry: bracket order (atomic SL/TP on exchange)
  4. Exit: exchange SL/TP (primary) + WebSocket backup + trailing stop
  5. Safety: double-close prevention, emergency recovery, circuit breaker
"""

import asyncio
import csv
import logging
import math
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

import pandas as pd

from api import (
    BracketOrderResult,
    DeltaAPIError,
    DeltaRESTClient,
    DeltaWSClient,
    L2OrderBook,
    OrderSide,
    OrderType,
)
from risk import RiskManager, TradeRecord
from state_store import StateStore
from strategy import ConfluenceStrategy, Signal, SignalType

logger = logging.getLogger(__name__)

DECISIONS_CSV = os.path.join(os.getcwd(), "decisions.csv")


def _log_csv(row: dict) -> None:
    write_header = not os.path.exists(DECISIONS_CSV)
    try:
        with open(DECISIONS_CSV, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(row.keys()))
            if write_header:
                writer.writeheader()
            writer.writerow(row)
    except Exception:
        pass


class ExecutionEngine:
    """
    Production execution engine with bracket orders + WebSocket SL/TP monitoring.
    """

    def __init__(
        self,
        rest_client:        DeltaRESTClient,
        strategy:           ConfluenceStrategy,
        risk_manager:       RiskManager,
        symbol:             str,
        product_id:         int,
        resolution_minutes: int = 15,
        api_key:            str = "",
        api_secret:         str = "",
        min_confidence:     float = 0.55,
        trailing_enabled:   bool = True,
        cooldown_minutes:   int = 15,  # min minutes between entries
    ):
        self.rest            = rest_client
        self.strategy        = strategy
        self.risk            = risk_manager
        self.symbol          = symbol
        self.product_id      = product_id
        self.resolution      = resolution_minutes
        self.api_key         = api_key
        self.api_secret      = api_secret
        self.min_confidence  = min_confidence
        self.trailing_enabled = trailing_enabled
        self.cooldown_seconds = cooldown_minutes * 60

        self._candle_buf: List[Dict] = []
        self._current_trade: Optional[TradeRecord] = None
        self._ws_price:   float = 0.0
        self._ws_mark:    float = 0.0
        self._ob:         Optional[L2OrderBook] = None
        self._funding:    float = 0.0
        self._shutdown    = False
        self._last_entry_ts: float = 0.0
        self._lot_size:   float = 0.0
        self._last_signal_candle_ts: int = 0
        self._state_store = StateStore()

    # ── Startup ──────────────────────────────────────────────────────────────

    async def bootstrap_history(self):
        logger.info("🚀 Bootstrapping %s (product_id=%d)…", self.symbol, self.product_id)

        # Fetch lot size
        self._lot_size = await self.rest.get_lot_size(self.symbol)
        logger.info("📦 Lot size for %s: %s", self.symbol, self._lot_size)

        # Prime candle buffer (300 bars = enough for EMA200)
        end   = int(time.time())
        start = end - self.resolution * 60 * 310
        try:
            candles = await self.rest.get_ohlcv(self.symbol, self.resolution, start, end)
            for c in candles:
                self._candle_buf.append({
                    "timestamp": c.timestamp,
                    "open": c.open, "high": c.high,
                    "low": c.low, "close": c.close,
                    "volume": c.volume,
                })
            logger.info("📊 Buffer primed with %d candles", len(self._candle_buf))
        except Exception as exc:
            logger.warning("Could not prime candle buffer: %s", exc)

        await self._recover_trade_state()

        # Close any orphaned positions
        try:
            positions = await self.rest.get_positions()
            for pos in positions:
                if pos.symbol == self.symbol and pos.size > 0:
                    if self._current_trade and not self._current_trade.closed:
                        continue
                    logger.warning("⚠️ ORPHANED POSITION: %s %s %.4f @ %.4f — closing!",
                                   pos.symbol, pos.side, pos.size, pos.entry_price)
                    await self._force_close(pos)
        except Exception as exc:
            logger.warning("Position check failed: %s", exc)

        # Fetch balance and update equity
        try:
            equity = await self.rest.get_account_equity("USDT")
            if equity > 0:
                self.risk.update_equity(equity)
                logger.info("💰 Account equity: %.4f USDT", equity)
        except Exception as exc:
            logger.warning("Balance fetch failed: %s", exc)

        # Set leverage
        leverage = int(self.risk.get_leverage_for_symbol(self.symbol))
        try:
            await self.rest.set_leverage(self.product_id, leverage)
        except Exception:
            logger.info("Leverage API unavailable — using configured %dx", leverage)

        # Fetch initial funding rate
        try:
            self._funding = await self.rest.get_funding_rate(self.symbol)
            logger.info("📈 Funding rate: %.6f", self._funding)
        except Exception:
            pass

    async def _force_close(self, pos) -> bool:
        try:
            lots = max(1, int(abs(pos.size)))
            await self.rest.close_position(self.product_id, pos.side, lots)
            logger.info("✅ Orphaned position closed")
            return True
        except Exception as exc:
            logger.error("❌ Force close failed: %s", exc)
            return False

    # ── Main Loop ─────────────────────────────────────────────────────────────

    async def run(self):
        """Entry point: bootstrap then run WebSocket + signal tasks concurrently."""
        await self.bootstrap_history()

        # Build WebSocket client
        ws = DeltaWSClient(self.api_key, self.api_secret, self._handle_ws)
        ws.subscribe_public("v2/ticker",    [self.symbol])
        ws.subscribe_public("l2_orderbook", [self.symbol])
        ws.subscribe_private(["orders", "positions", "user_trades"], symbols=[self.symbol])

        ws_task     = asyncio.create_task(ws.connect())
        signal_task = asyncio.create_task(self._signal_loop())
        balance_task = asyncio.create_task(self._balance_loop())

        logger.info("✅ Engine running: %s | resolution=%dm | min_confidence=%.2f",
                    self.symbol, self.resolution, self.min_confidence)

        try:
            await asyncio.gather(ws_task, signal_task, balance_task)
        except asyncio.CancelledError:
            logger.info("Engine shutting down…")
            await ws.disconnect()

    # ── WebSocket Handler ─────────────────────────────────────────────────────

    async def _handle_ws(self, msg: Dict):
        msg_type = msg.get("type", "")

        # Ticker update → price tracking + SL/TP check
        if msg_type == "v2/ticker":
            data  = msg.get("symbol_data", msg)
            price = float(data.get("close", 0) or data.get("last_price", 0))
            if price > 0:
                self._ws_price = price
                self._ws_mark  = float(data.get("mark_price", price))
                await self._check_sl_tp(price)
                if self.trailing_enabled:
                    await self._update_trailing(price)

        # Order book update → save for OB filter
        elif msg_type == "l2_orderbook":
            data = msg.get("buy", None)
            if data is not None:
                self._ob = L2OrderBook(
                    symbol=self.symbol,
                    buy=msg.get("buy", []),
                    sell=msg.get("sell", []),
                )

        # Private order fills → sync trade state
        elif msg_type in ("orders", "user_trades"):
            await self._handle_private_event(msg)

    async def _check_sl_tp(self, price: float):
        """WebSocket SL/TP backup check on every tick."""
        trade = self._current_trade
        if not trade or trade.closed:
            return

        hit_sl = False
        hit_tp = False

        if trade.side == "long":
            hit_sl = price <= trade.stop_loss
            hit_tp = trade.take_profit is not None and price >= trade.take_profit
        else:
            hit_sl = price >= trade.stop_loss
            hit_tp = trade.take_profit is not None and price <= trade.take_profit

        if hit_tp:
            logger.info("🎯 WS TP HIT: %s price=%.4f tp=%.4f", self.symbol, price, trade.take_profit)
            await self._execute_close(trade, price, "take_profit_ws")
        elif hit_sl:
            logger.info("🛑 WS SL HIT: %s price=%.4f sl=%.4f", self.symbol, price, trade.stop_loss)
            await self._execute_close(trade, price, "stop_loss_ws")

    async def _update_trailing(self, price: float):
        """Update trailing stop via risk manager."""
        trade = self._current_trade
        if not trade or trade.closed:
            return
        new_sl = self.risk.update_trailing_stop(trade, price)
        # Note: we update in-memory only. Exchange bracket SL is primary.
        # Could call edit_bracket_order here if Delta supports it.
        if new_sl is not None:
            self._persist_trade_state()

    async def _handle_private_event(self, msg: Dict):
        """Handle private fills/orders to sync trade state."""
        trade = self._current_trade
        if not trade:
            return

        msg_type = msg.get("type", "")
        payload = msg.get("result") if isinstance(msg.get("result"), dict) else msg
        order_id = str(payload.get("id") or payload.get("order_id") or "")
        client_order_id = str(payload.get("client_order_id") or "")
        state = msg.get("state") or msg.get("order_state", "")
        reason = msg.get("close_reason", "") or msg.get("reason", "")
        state = str(state or payload.get("state") or payload.get("order_state") or "").lower()
        reason = str(reason or payload.get("close_reason") or "").lower()
        avg_fill_price = float(payload.get("average_fill_price", 0) or 0)
        raw_size = int(float(payload.get("size", 0) or 0))
        unfilled_size = int(float(payload.get("unfilled_size", 0) or 0))
        filled_size = int(float(payload.get("filled_size", 0) or max(0, raw_size - unfilled_size)))

        is_entry_event = order_id == trade.order_id or (client_order_id and client_order_id == trade.entry_client_order_id)
        if is_entry_event:
            if avg_fill_price > 0:
                trade.entry_price = avg_fill_price
                trade.peak_price = avg_fill_price
                trade.valley_price = avg_fill_price
            if filled_size > 0:
                trade.filled_size = filled_size
            if state in ("filled", "closed", "partially_filled", "partially_closed"):
                trade.entry_filled = trade.filled_size > 0
            if state in ("cancelled", "rejected") and not trade.entry_filled:
                logger.warning("Entry order %s %s; releasing local trade state", trade.order_id, state)
                self.risk.release_trade(trade, reason=state)
                self._state_store.clear_trade(self.symbol)
                self._current_trade = None
                return
            self._persist_trade_state()

        if msg_type == "positions":
            symbol = str(payload.get("product_symbol") or payload.get("symbol") or "")
            if symbol == self.symbol:
                size = abs(float(payload.get("size", 0) or 0))
                if size == 0 and trade.entry_filled and not trade.closed:
                    exit_p = self._ws_price or self._ws_mark or trade.entry_price
                    await self._mark_closed(trade, exit_p, "position_flattened")
                    return

        is_exit_event = order_id in {trade.stop_order_id, trade.take_profit_order_id}
        if state in ("closed", "filled") and reason in ("sl_trigger", "tp_trigger", "stop_trigger") and (is_exit_event or is_entry_event):
            if trade and not trade.closed:
                exit_p = avg_fill_price or self._ws_price or trade.entry_price
                close_reason = "stop_loss_exchange" if trade.stop_order_id == order_id or "sl" in reason else "take_profit_exchange"
                logger.info("📩 Exchange %s: %s @ %.4f", close_reason, self.symbol, exit_p)
                await self._mark_closed(trade, exit_p, close_reason)

    # ── Signal Loop ───────────────────────────────────────────────────────────

    async def _signal_loop(self):
        """Run strategy on every candle close."""
        # Wait for WebSocket to connect and get first price
        await asyncio.sleep(5)
        logger.info("📡 Signal loop started")

        while not self._shutdown:
            try:
                await self._tick()
            except Exception as exc:
                logger.error("Signal tick error: %s", exc, exc_info=True)
            await asyncio.sleep(self._seconds_until_next_close())

    def _seconds_until_next_close(self, buffer_seconds: int = 2) -> float:
        resolution_seconds = self.resolution * 60
        now = time.time()
        next_close = (math.floor(now / resolution_seconds) + 1) * resolution_seconds
        return max(1.0, next_close - now + buffer_seconds)

    def _latest_closed_candle_ts(self) -> int:
        resolution_seconds = self.resolution * 60
        now = time.time()
        return int((math.floor(now / resolution_seconds) - 1) * resolution_seconds)

    async def _tick(self):
        """One strategy tick: fetch candle, generate signal, execute if valid."""
        # Fetch latest candle
        closed_candle_ts = self._latest_closed_candle_ts()
        if closed_candle_ts <= self._last_signal_candle_ts:
            return

        end = closed_candle_ts
        start = end - self.resolution * 60 * 6
        try:
            new_candles = await self.rest.get_ohlcv(self.symbol, self.resolution, start, end)
            for c in new_candles:
                existing_ts = {b["timestamp"] for b in self._candle_buf}
                if c.timestamp not in existing_ts:
                    self._candle_buf.append({
                        "timestamp": c.timestamp,
                        "open": c.open, "high": c.high,
                        "low": c.low, "close": c.close,
                        "volume": c.volume,
                    })
            # Keep last 350 bars
            self._candle_buf = sorted(self._candle_buf, key=lambda x: x["timestamp"])[-350:]
        except Exception as exc:
            logger.warning("Candle fetch failed: %s", exc)
            return

        if len(self._candle_buf) < 210:
            logger.info("Warming up… %d/210 candles", len(self._candle_buf))
            return

        # Build DataFrame
        df = pd.DataFrame(self._candle_buf).set_index("timestamp")
        df.index = pd.to_datetime(df.index, unit="s", utc=True)
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        df = df[df.index <= pd.to_datetime(closed_candle_ts, unit="s", utc=True)]

        # Update equity from balance periodically
        # (also done in _balance_loop, this is a secondary update)

        # Generate signal
        try:
            signal = self.strategy.generate_signal(df, self.symbol, self._funding)
        except Exception as exc:
            logger.error("Strategy error: %s", exc, exc_info=True)
            return

        if signal is None or signal.type == SignalType.NEUTRAL:
            self._last_signal_candle_ts = closed_candle_ts
            logger.debug("Neutral signal — no action")
            return

        # Current price
        price = self._ws_price or float(df["close"].iloc[-1])

        # Risk checks
        if not self.risk.check_signal(signal):
            self._last_signal_candle_ts = closed_candle_ts
            logger.info("Signal rejected by risk manager")
            return

        # Cooldown check
        time_since_last = time.time() - self._last_entry_ts
        if time_since_last < self.cooldown_seconds:
            self._last_signal_candle_ts = closed_candle_ts
            logger.info("Cooldown: %.0fs remaining", self.cooldown_seconds - time_since_last)
            return

        # Trade already open?
        if self._current_trade and not self._current_trade.closed:
            self._last_signal_candle_ts = closed_candle_ts
            logger.debug("Trade already open — skipping new entry")
            return

        # Confidence filter
        if signal.confidence < self.min_confidence:
            self._last_signal_candle_ts = closed_candle_ts
            logger.info("Confidence %.2f < %.2f — skipping", signal.confidence, self.min_confidence)
            return

        # Validate SL/TP exist and make sense
        if not signal.stop_loss or signal.stop_loss <= 0:
            self._last_signal_candle_ts = closed_candle_ts
            logger.warning("Signal has no valid SL — skipping")
            return

        await self._execute_entry(signal, price, df)
        self._last_signal_candle_ts = closed_candle_ts

    # ── Balance Loop ──────────────────────────────────────────────────────────

    async def _balance_loop(self):
        """Refresh wallet balance every 5 minutes."""
        while not self._shutdown:
            await asyncio.sleep(300)
            try:
                equity = await self.rest.get_account_equity("USDT")
                if equity > 0:
                    self.risk.update_equity(equity)
            except Exception as exc:
                logger.debug("Balance refresh failed: %s", exc)
            # Also refresh funding rate
            try:
                self._funding = await self.rest.get_funding_rate(self.symbol)
            except Exception:
                pass

    # ── Entry ─────────────────────────────────────────────────────────────────

    async def _execute_entry(self, signal: Signal, price: float, df: pd.DataFrame):
        """Place bracket order for a new entry."""
        equity   = self.risk.current_equity
        sl_dist  = abs(price - signal.stop_loss)
        notional = self.risk.calculate_position_size(equity, price, sl_dist, self.symbol)

        if notional <= 0:
            logger.warning("Position size = 0, skipping (equity=%.4f sl_dist=%.4f)", equity, sl_dist)
            return

        lots = self.rest.usd_to_lots(self.symbol, notional, price, self._lot_size)
        if lots < 1:
            logger.warning("Lots < 1, skipping (notional=%.2f price=%.4f lot=%.6f)",
                           notional, price, self._lot_size)
            return

        side = OrderSide.BUY if signal.type == SignalType.LONG else OrderSide.SELL
        coid = f"bot_{self.symbol}_{int(time.time())}"

        # Validate SL/TP direction
        if signal.type == SignalType.LONG and signal.stop_loss >= price:
            logger.warning("SL %.4f >= price %.4f for LONG — aborting", signal.stop_loss, price)
            return
        if signal.type == SignalType.SHORT and signal.stop_loss <= price:
            logger.warning("SL %.4f <= price %.4f for SHORT — aborting", signal.stop_loss, price)
            return

        logger.info(
            "📤 PLACING BRACKET: %s %s %d lots | price=%.4f | SL=%.4f | TP=%s | conf=%.2f",
            self.symbol, side.value.upper(), lots, price,
            signal.stop_loss,
            f"{signal.take_profit:.4f}" if signal.take_profit else "NONE",
            signal.confidence,
        )

        try:
            result: BracketOrderResult = await self.rest.place_bracket_order(
                product_id        = self.product_id,
                side              = side,
                size              = lots,
                stop_loss_price   = signal.stop_loss,
                take_profit_price = signal.take_profit,
                client_order_id   = coid,
            )
        except DeltaAPIError as exc:
            logger.error("❌ Bracket order failed: %s", exc)
            return

        trade = TradeRecord(
            id          = str(uuid.uuid4()),
            symbol      = self.symbol,
            side        = "long" if signal.type == SignalType.LONG else "short",
            entry_price = result.average_fill_price or price,
            size        = lots,
            contract_value = self._lot_size,
            stop_loss   = signal.stop_loss,
            take_profit = signal.take_profit,
            entry_time  = datetime.now(timezone.utc),
            order_id    = result.entry_order_id,
            entry_client_order_id = coid,
            notional_usd = lots * self._lot_size * (result.average_fill_price or price),
            entry_filled = bool(result.average_fill_price or result.filled_size or result.state in ("filled", "closed")),
            filled_size = result.filled_size or lots,
            stop_order_id = result.sl_order_id,
            take_profit_order_id = result.tp_order_id,
        )
        self._current_trade = trade
        self.risk.register_trade(trade)
        self._last_entry_ts = time.time()
        self._persist_trade_state()

        # Log to CSV
        _log_csv({
            "timestamp": datetime.utcnow().isoformat(),
            "symbol": self.symbol,
            "event": "ENTRY",
            "side": trade.side,
            "price": trade.entry_price,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "lots": lots,
            "contract_value": self._lot_size,
            "notional_usd": round(trade.notional_usd, 4),
            "confidence": signal.confidence,
            "regime": signal.metadata.get("regime", ""),
            "htf": signal.metadata.get("htf", ""),
        })

        # Telegram notification
        try:
            from notifier import send_trade_alert
            send_trade_alert({
                "symbol": self.symbol, "side": trade.side,
                "entry_price": trade.entry_price, "stop_loss": signal.stop_loss,
                "take_profit": signal.take_profit, "size": lots,
            })
        except Exception:
            pass

    # ── Exit ──────────────────────────────────────────────────────────────────

    async def _execute_close(self, trade: TradeRecord, price: float, reason: str):
        if trade.closed:
            return  # Double-close prevention

        close_side = OrderSide.SELL if trade.side == "long" else OrderSide.BUY
        try:
            await self.rest.place_market_order(
                product_id  = self.product_id,
                side        = close_side,
                size        = trade.size,
                reduce_only = True,
            )
            # Cancel any remaining bracket orders
            await self.rest.cancel_all_orders(self.product_id)
        except DeltaAPIError as exc:
            if "no_position" in str(exc).lower() or "not_found" in str(exc).lower():
                logger.info("Position already closed by exchange (SL/TP fired)")
            else:
                logger.error("Close order failed: %s", exc)

        await self._mark_closed(trade, price, reason)

    async def _mark_closed(self, trade: TradeRecord, exit_price: float, reason: str):
        if trade.closed:
            return

        trade.closed     = True
        trade.exit_price = exit_price
        trade.exit_time  = datetime.now(timezone.utc)
        trade.reason     = reason

        pnl = trade.net_pnl or 0.0

        self.risk.close_trade(trade, exit_price)
        self._state_store.clear_trade(self.symbol)
        self._current_trade = None

        pnl_emoji = "✅" if pnl >= 0 else "❌"
        logger.info(
            "%s CLOSED: %s %s | entry=%.4f exit=%.4f pnl=%+.4f | reason=%s",
            pnl_emoji, trade.symbol, trade.side.upper(),
            trade.entry_price, exit_price, pnl, reason,
        )

        _log_csv({
            "timestamp": datetime.utcnow().isoformat(),
            "symbol": trade.symbol,
            "event": "EXIT",
            "side": trade.side,
            "price": exit_price,
            "stop_loss": trade.stop_loss,
            "take_profit": trade.take_profit,
            "lots": trade.size,
            "pnl": pnl,
            "reason": reason,
        })

        # Print running stats
        stats = self.risk.get_stats()
        logger.info("📊 Stats: trades=%d win_rate=%.1f%% total_pnl=%.4f equity=%.4f dd=%.1f%%",
                    stats.get("total_trades", 0), stats.get("win_rate", 0),
                    stats.get("total_pnl", 0), stats.get("current_equity", 0),
                    stats.get("drawdown_pct", 0))

        try:
            from notifier import send_exit_alert
            send_exit_alert(trade.symbol, trade.side, trade.entry_price, exit_price, pnl, reason)
        except Exception:
            pass

    def _persist_trade_state(self):
        if self._current_trade and not self._current_trade.closed:
            self._state_store.save_trade(self._current_trade)

    async def _recover_trade_state(self):
        persisted = self._state_store.load_trade(self.symbol)
        if not persisted:
            return

        logger.info("Recovering persisted trade state for %s", self.symbol)
        recovered = await self._reconcile_trade_with_exchange(persisted)
        if recovered and not recovered.closed:
            self._current_trade = recovered
            self.risk.register_trade(recovered)
            self._persist_trade_state()
            logger.info(
                "Recovered live trade: %s %s %d lots @ %.4f",
                recovered.symbol,
                recovered.side.upper(),
                recovered.filled_size,
                recovered.entry_price,
            )
        else:
            self._state_store.clear_trade(self.symbol)

    async def _reconcile_trade_with_exchange(self, trade: TradeRecord) -> Optional[TradeRecord]:
        entry_order = await self._fetch_best_order_snapshot(trade.order_id, trade.entry_client_order_id)
        stop_order = await self._fetch_best_order_snapshot(trade.stop_order_id)
        tp_order = await self._fetch_best_order_snapshot(trade.take_profit_order_id)
        positions = await self.rest.get_positions()
        position = next((p for p in positions if p.symbol == self.symbol and p.size > 0), None)

        if entry_order:
            avg_fill_price = float(entry_order.get("average_fill_price", 0) or 0)
            raw_size = int(float(entry_order.get("size", 0) or trade.size))
            unfilled_size = int(float(entry_order.get("unfilled_size", 0) or 0))
            filled_size = max(0, raw_size - unfilled_size)
            if avg_fill_price > 0:
                trade.entry_price = avg_fill_price
                trade.peak_price = avg_fill_price
                trade.valley_price = avg_fill_price
            if filled_size > 0:
                trade.filled_size = filled_size
                trade.entry_filled = True

        if position:
            trade.entry_filled = True
            trade.side = position.side
            trade.filled_size = max(1, int(abs(position.size)))
            if position.entry_price > 0:
                trade.entry_price = position.entry_price
            trade.notional_usd = trade.filled_size * trade.contract_value * trade.entry_price
            trade.stop_order_id = str((stop_order or {}).get("id") or trade.stop_order_id or "")
            trade.take_profit_order_id = str((tp_order or {}).get("id") or trade.take_profit_order_id or "")
            return trade

        exit_order = None
        if stop_order and str(stop_order.get("state", "")).lower() in ("closed", "filled"):
            exit_order = stop_order
        elif tp_order and str(tp_order.get("state", "")).lower() in ("closed", "filled"):
            exit_order = tp_order
        elif entry_order and str(entry_order.get("state", "")).lower() in ("cancelled", "rejected", "closed") and not trade.entry_filled:
            return None

        if exit_order:
            exit_price = float(exit_order.get("average_fill_price", 0) or trade.entry_price)
            trade.closed = True
            trade.exit_price = exit_price
            trade.exit_time = datetime.now(timezone.utc)
            trade.reason = "recovered_closed"
            return trade

        return None if not trade.entry_filled else trade

    async def _fetch_best_order_snapshot(self, order_id: Optional[str], client_order_id: str = "") -> Optional[Dict]:
        if order_id:
            order = await self.rest.get_order_by_id(str(order_id))
            if order:
                return order
        if client_order_id:
            return await self.rest.get_order_by_client_order_id(client_order_id)
        return None


__all__ = ["ExecutionEngine"]
