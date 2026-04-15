"""
execution.py — V8 Production Execution Engine

Changes from V7:
  - Per-trade analytics: writes full entry_quality breakdown to decisions.csv
  - TradeRecord populated with setup_type, entry_grade, rsi/adx at entry
  - trade_history.csv now includes all analytics columns for dashboard
  - Multi-coin safety: each bot's equity tracked independently (via allocated capital)
  - Cooldown correctly resets per symbol (was already correct in V7, kept)
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
from delta_bot.monitoring import RuntimeMonitor
from delta_bot.portfolio import PortfolioRiskManager
from delta_bot.storage import AuditStore
from risk import RiskManager, TradeRecord
from state_store import StateStore
from strategy import ConfluenceStrategy, Signal, SignalType, normalize_signal

logger = logging.getLogger(__name__)

DECISIONS_CSV    = os.path.join(os.getcwd(), "decisions.csv")
TRADE_HISTORY_CSV = os.path.join(os.getcwd(), "trade_history.csv")


def _log_csv(path: str, row: dict) -> None:
    write_header = not os.path.exists(path)
    try:
        with open(path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(row.keys()))
            if write_header:
                writer.writeheader()
            writer.writerow(row)
    except Exception:
        pass


class ExecutionEngine:
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
        min_confidence:     float = 0.58,
        trailing_enabled:   bool = True,
        cooldown_minutes:   int = 15,
        account_asset:      str = "USDT",
        audit_store:        Optional[AuditStore] = None,
        portfolio_risk:     Optional[PortfolioRiskManager] = None,
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
        self.account_asset   = account_asset
        self.audit_store     = audit_store
        self.portfolio_risk  = portfolio_risk
        self.monitor         = RuntimeMonitor(audit_store, symbol=symbol) if audit_store else None

        self._candle_buf: List[Dict] = []
        self._current_trade: Optional[TradeRecord] = None
        self._ws_price:  float = 0.0
        self._ws_mark:   float = 0.0
        self._ob:        Optional[L2OrderBook] = None
        self._funding:   float = 0.0
        self._shutdown   = False
        self._last_entry_ts: float = 0.0
        self._lot_size:  float = 0.0
        self._last_signal_candle_ts: int = 0
        self._state_store = StateStore()

    def _audit_event(self, category: str, event_type: str, payload: Dict, severity: str = "info") -> None:
        if not self.audit_store:
            return
        self.audit_store.record_event(
            category,
            event_type,
            payload,
            symbol=self.symbol,
            severity=severity,
        )

    def _monitor_heartbeat(self, component: str, **details) -> None:
        if self.monitor:
            self.monitor.heartbeat(component, **details)

    def _monitor_loop_timing(self, component: str, duration_ms: float, **details) -> None:
        if self.monitor:
            self.monitor.loop_timing(component, duration_ms, **details)

    def _monitor_error(self, component: str, exc: Exception, **details) -> None:
        if self.monitor:
            self.monitor.error(component, str(exc), **details)

    # ── Startup ───────────────────────────────────────────────────────────────

    async def bootstrap_history(self):
        self._monitor_heartbeat("engine_bootstrap", product_id=self.product_id)
        self._audit_event("system", "engine_bootstrap_started", {"product_id": self.product_id})
        logger.info("🚀 Bootstrapping %s (product_id=%d)…", self.symbol, self.product_id)

        self._lot_size = await self.rest.get_lot_size(self.symbol)
        logger.info("📦 Lot size for %s: %s", self.symbol, self._lot_size)

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

        try:
            positions = await self.rest.get_positions()
            for pos in positions:
                if pos.symbol == self.symbol and pos.size > 0:
                    if self._current_trade and not self._current_trade.closed:
                        continue
                    logger.warning("⚠️ ORPHANED POSITION: %s %s — closing!", pos.symbol, pos.side)
                    await self._force_close(pos)
        except Exception as exc:
            logger.warning("Position check failed: %s", exc)

        try:
            equity = await self.rest.get_account_equity(self.account_asset)
            if equity > 0:
                self.risk.update_equity(equity)
                if self.portfolio_risk:
                    self.portfolio_risk.sync_equity(equity)
                logger.info("💰 Account equity: %.4f %s", equity, self.account_asset)
        except Exception as exc:
            logger.warning("Balance fetch failed: %s", exc)

        leverage = int(self.risk.get_leverage_for_symbol(self.symbol))
        try:
            await self.rest.set_leverage(self.product_id, leverage)
        except Exception:
            logger.info("Leverage API unavailable — using configured %dx", leverage)

        try:
            self._funding = await self.rest.get_funding_rate(self.symbol)
            logger.info("📈 Funding rate: %.6f", self._funding)
        except Exception:
            pass

    async def _force_close(self, pos) -> bool:
        try:
            lots = max(1, int(abs(pos.size)))
            await self.rest.close_position(self.product_id, pos.side, lots)
            return True
        except Exception as exc:
            logger.error("❌ Force close failed: %s", exc)
            return False

    # ── Main Loop ─────────────────────────────────────────────────────────────

    async def run(self):
        await self.bootstrap_history()

        ws = DeltaWSClient(self.api_key, self.api_secret, self._handle_ws)
        ws.subscribe_public("v2/ticker",    [self.symbol])
        ws.subscribe_public("l2_orderbook", [self.symbol])
        ws.subscribe_private(["orders", "positions", "user_trades"], symbols=[self.symbol])

        ws_task      = asyncio.create_task(ws.connect())
        signal_task  = asyncio.create_task(self._signal_loop())
        balance_task = asyncio.create_task(self._balance_loop())
        self._audit_event(
            "system",
            "engine_started",
            {"resolution_minutes": self.resolution, "min_confidence": self.min_confidence},
        )

        logger.info("✅ Engine running: %s | resolution=%dm | min_confidence=%.2f",
                    self.symbol, self.resolution, self.min_confidence)
        try:
            await asyncio.gather(ws_task, signal_task, balance_task)
        except asyncio.CancelledError:
            logger.info("Engine shutting down…")
            self._audit_event("system", "engine_shutdown", {})
            await ws.disconnect()

    # ── WebSocket Handler ─────────────────────────────────────────────────────

    async def _handle_ws(self, msg: Dict):
        msg_type = msg.get("type", "")

        if msg_type == "v2/ticker":
            data  = msg.get("symbol_data", msg)
            price = float(data.get("close", 0) or data.get("last_price", 0))
            if price > 0:
                self._ws_price = price
                self._ws_mark  = float(data.get("mark_price", price))
                self._monitor_heartbeat("market_data_ws", price=round(price, 6))
                await self._check_sl_tp(price)
                if self.trailing_enabled:
                    await self._update_trailing(price)

        elif msg_type == "l2_orderbook":
            data = msg.get("buy", None)
            if data is not None:
                self._ob = L2OrderBook(
                    symbol=self.symbol,
                    buy=msg.get("buy", []),
                    sell=msg.get("sell", []),
                )

        elif msg_type in ("orders", "user_trades"):
            await self._handle_private_event(msg)

    async def _check_sl_tp(self, price: float):
        trade = self._current_trade
        if not trade or trade.closed:
            return

        hit_sl = hit_tp = False
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
        trade = self._current_trade
        if not trade or trade.closed:
            return
        new_sl = self.risk.update_trailing_stop(trade, price)
        if new_sl is not None:
            self._persist_trade_state()

    async def _handle_private_event(self, msg: Dict):
        trade = self._current_trade
        if not trade:
            return

        msg_type = msg.get("type", "")
        payload  = msg.get("result") if isinstance(msg.get("result"), dict) else msg
        order_id = str(payload.get("id") or payload.get("order_id") or "")
        client_order_id = str(payload.get("client_order_id") or "")
        state  = str(msg.get("state") or msg.get("order_state") or payload.get("state") or payload.get("order_state") or "").lower()
        reason = str(msg.get("close_reason") or msg.get("reason") or payload.get("close_reason") or "").lower()
        avg_fill_price = float(payload.get("average_fill_price", 0) or 0)
        raw_size       = int(float(payload.get("size", 0) or 0))
        unfilled_size  = int(float(payload.get("unfilled_size", 0) or 0))
        filled_size    = int(float(payload.get("filled_size", 0) or max(0, raw_size - unfilled_size)))

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
                logger.warning("Entry order %s %s; releasing trade", trade.order_id, state)
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
                await self._mark_closed(trade, exit_p, close_reason)

    # ── Signal Loop ───────────────────────────────────────────────────────────

    async def _signal_loop(self):
        await asyncio.sleep(5)
        logger.info("📡 Signal loop started")
        while not self._shutdown:
            started = time.perf_counter()
            try:
                await self._tick()
            except Exception as exc:
                logger.error("Signal tick error: %s", exc, exc_info=True)
                self._monitor_error("signal_loop", exc)
            else:
                self._monitor_loop_timing("signal_loop", (time.perf_counter() - started) * 1000.0)
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

    def _log_signal_decision(self, signal: Optional[Signal], price: float, candle_ts: int) -> None:
        ts = datetime.fromtimestamp(candle_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        if signal is None or signal.type == SignalType.NEUTRAL:
            meta     = signal.metadata if signal else {}
            blockers = ",".join(meta.get("blockers", [])) if meta.get("blockers") else "-"
            logger.info(
                "HOLD | %s | %s | price=%.4f | regime=%s | htf=%s | rsi=%s | conf=%.2f | blockers=%s",
                self.symbol, ts, price,
                meta.get("regime", "-"), meta.get("htf", "-"),
                meta.get("rsi", "-"), float(signal.confidence) if signal else 0.0,
                blockers,
            )
            self._audit_event(
                "decision",
                "hold",
                {
                    "candle_time": ts,
                    "price": price,
                    "regime": meta.get("regime", "-"),
                    "htf": meta.get("htf", "-"),
                    "rsi": meta.get("rsi", "-"),
                    "confidence": float(signal.confidence) if signal else 0.0,
                    "blockers": meta.get("blockers", []),
                },
            )
            return

        side = "BUY" if signal.type == SignalType.LONG else "SELL"
        meta = signal.metadata or {}
        quality = meta.get("entry_quality", {})
        logger.info(
            "%s | %s | %s | price=%.4f | sl=%.4f | tp=%s | conf=%.2f | setup=%s | grade=%s | regime=%s | htf=%s | rsi=%s",
            side, self.symbol, ts, price,
            float(signal.stop_loss or 0.0),
            f"{float(signal.take_profit):.4f}" if signal.take_profit else "NONE",
            float(signal.confidence),
            meta.get("setup_type", "-"),
            quality.get("grade", "-"),
            meta.get("regime", "-"), meta.get("htf", "-"), meta.get("rsi", "-"),
        )
        self._audit_event(
            "decision",
            "signal",
            {
                "candle_time": ts,
                "side": side.lower(),
                "price": price,
                "stop_loss": float(signal.stop_loss or 0.0),
                "take_profit": float(signal.take_profit or 0.0) if signal.take_profit else None,
                "confidence": float(signal.confidence),
                "setup_type": meta.get("setup_type", "-"),
                "entry_grade": quality.get("grade", "-"),
                "regime": meta.get("regime", "-"),
                "htf": meta.get("htf", "-"),
                "rsi": meta.get("rsi", "-"),
            },
        )

    async def _tick(self):
        closed_candle_ts = self._latest_closed_candle_ts()
        if closed_candle_ts <= self._last_signal_candle_ts:
            return

        end   = closed_candle_ts
        start = end - self.resolution * 60 * 6
        try:
            new_candles = await self.rest.get_ohlcv(self.symbol, self.resolution, start, end)
            existing_ts = {b["timestamp"] for b in self._candle_buf}
            for c in new_candles:
                if c.timestamp not in existing_ts:
                    self._candle_buf.append({
                        "timestamp": c.timestamp,
                        "open": c.open, "high": c.high,
                        "low": c.low, "close": c.close,
                        "volume": c.volume,
                    })
            self._candle_buf = sorted(self._candle_buf, key=lambda x: x["timestamp"])[-350:]
        except Exception as exc:
            logger.warning("Candle fetch failed: %s", exc)
            self._monitor_error("candle_fetch", exc)
            return

        if len(self._candle_buf) < 210:
            logger.info("Warming up… %d/210 candles", len(self._candle_buf))
            return

        df = pd.DataFrame(self._candle_buf).set_index("timestamp")
        df.index = pd.to_datetime(df.index, unit="s", utc=True)
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        df = df[df.index <= pd.to_datetime(closed_candle_ts, unit="s", utc=True)]

        try:
            signal = normalize_signal(self.strategy.generate_signal(df, self.symbol, self._funding))
        except Exception as exc:
            logger.error("Strategy error: %s", exc, exc_info=True)
            self._monitor_error("strategy_generate_signal", exc)
            return

        price = self._ws_price or float(df["close"].iloc[-1])
        self._log_signal_decision(signal, price, closed_candle_ts)

        if signal is None or signal.type == SignalType.NEUTRAL:
            self._last_signal_candle_ts = closed_candle_ts
            return

        if not self.risk.check_signal(signal):
            self._last_signal_candle_ts = closed_candle_ts
            logger.info("Signal rejected by risk manager")
            self._audit_event(
                "risk",
                "signal_rejected_local_risk",
                {"confidence": signal.confidence, "candle_ts": closed_candle_ts},
                severity="warning",
            )
            return

        time_since_last = time.time() - self._last_entry_ts
        if time_since_last < self.cooldown_seconds:
            self._last_signal_candle_ts = closed_candle_ts
            logger.info("Cooldown: %.0fs remaining", self.cooldown_seconds - time_since_last)
            return

        if self._current_trade and not self._current_trade.closed:
            self._last_signal_candle_ts = closed_candle_ts
            logger.debug("Trade already open — skipping")
            return

        if not signal.stop_loss or signal.stop_loss <= 0:
            self._last_signal_candle_ts = closed_candle_ts
            logger.warning("Signal has no valid SL — skipping")
            return

        await self._execute_entry(signal, price, df)
        self._last_signal_candle_ts = closed_candle_ts

    # ── Balance Loop ──────────────────────────────────────────────────────────

    async def _balance_loop(self):
        while not self._shutdown:
            await asyncio.sleep(300)
            started = time.perf_counter()
            try:
                equity = await self.rest.get_account_equity(self.account_asset)
                if equity > 0:
                    self.risk.update_equity(equity)
                    if self.portfolio_risk:
                        self.portfolio_risk.sync_equity(equity)
            except Exception as exc:
                logger.debug("Balance refresh failed: %s", exc)
                self._monitor_error("balance_refresh", exc)
            try:
                self._funding = await self.rest.get_funding_rate(self.symbol)
            except Exception:
                pass
            self._monitor_loop_timing("balance_loop", (time.perf_counter() - started) * 1000.0)

    # ── Entry ─────────────────────────────────────────────────────────────────

    async def _execute_entry(self, signal: Signal, price: float, df: pd.DataFrame):
        equity   = self.risk.current_equity
        sl_dist  = abs(price - signal.stop_loss)
        notional = self.risk.calculate_position_size(equity, price, sl_dist, self.symbol)

        if notional <= 0:
            logger.warning("Position size = 0, skipping")
            return

        lots = self.rest.usd_to_lots(self.symbol, notional, price, self._lot_size)
        if lots < 1:
            logger.warning("Lots < 1, skipping (notional=%.2f price=%.4f lot=%.6f)",
                           notional, price, self._lot_size)
            self._audit_event(
                "risk",
                "signal_rejected_position_too_small",
                {"notional_usd": notional, "price": price, "lot_size": self._lot_size},
                severity="warning",
            )
            return

        actual_notional = lots * self._lot_size * price
        proposed_risk = abs(price - signal.stop_loss) * lots * self._lot_size
        if self.portfolio_risk:
            allowed, reason = self.portfolio_risk.can_open_trade(
                self.symbol,
                proposed_notional_usd=actual_notional,
                proposed_risk_usd=proposed_risk,
            )
            if not allowed:
                logger.warning("Portfolio risk rejected trade: %s", reason)
                self._audit_event(
                    "risk",
                    "signal_rejected_portfolio_risk",
                    {"reason": reason, "notional_usd": actual_notional, "proposed_risk_usd": proposed_risk},
                    severity="warning",
                )
                return

        side = OrderSide.BUY if signal.type == SignalType.LONG else OrderSide.SELL
        coid = f"bot_{self.symbol}_{int(time.time())}"

        if signal.type == SignalType.LONG and signal.stop_loss >= price:
            logger.warning("SL %.4f >= price %.4f for LONG — aborting", signal.stop_loss, price)
            return
        if signal.type == SignalType.SHORT and signal.stop_loss <= price:
            logger.warning("SL %.4f <= price %.4f for SHORT — aborting", signal.stop_loss, price)
            return

        # Extract analytics from signal metadata
        meta    = signal.metadata or {}
        quality = meta.get("entry_quality", {})
        setup_type   = meta.get("setup_type", "")
        entry_grade  = quality.get("grade", "")
        quality_score = quality.get("overall", 0.0)
        ema_depth    = meta.get("ema_depth_pct", 0.0)
        regime_entry = meta.get("regime", "")
        htf_entry    = meta.get("htf", "")
        rsi_entry    = meta.get("rsi", 0.0)
        adx_entry    = meta.get("adx", 0.0)

        logger.info(
            "📤 PLACING BRACKET: %s %s %d lots | price=%.4f | SL=%.4f | TP=%s | conf=%.2f | setup=%s grade=%s",
            self.symbol, side.value.upper(), lots, price,
            signal.stop_loss,
            f"{signal.take_profit:.4f}" if signal.take_profit else "NONE",
            signal.confidence, setup_type, entry_grade,
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
            id             = str(uuid.uuid4()),
            symbol         = self.symbol,
            side           = "long" if signal.type == SignalType.LONG else "short",
            entry_price    = result.average_fill_price or price,
            size           = lots,
            contract_value = self._lot_size,
            stop_loss      = signal.stop_loss,
            take_profit    = signal.take_profit,
            entry_time     = datetime.now(timezone.utc),
            order_id       = result.entry_order_id,
            entry_client_order_id = coid,
            notional_usd   = lots * self._lot_size * (result.average_fill_price or price),
            entry_filled   = bool(result.average_fill_price or result.filled_size or result.state in ("filled", "closed")),
            filled_size    = result.filled_size or lots,
            stop_order_id  = result.sl_order_id,
            take_profit_order_id = result.tp_order_id,
            # V8 analytics fields
            setup_type     = setup_type,
            entry_grade    = entry_grade,
            entry_quality_score = quality_score,
            ema_depth_pct  = ema_depth,
            regime_at_entry = regime_entry,
            htf_at_entry   = htf_entry,
            rsi_at_entry   = float(rsi_entry),
            adx_at_entry   = float(adx_entry),
        )
        self._current_trade = trade
        self.risk.register_trade(trade)
        if self.portfolio_risk:
            self.portfolio_risk.register_trade(
                trade.id,
                symbol=trade.symbol,
                side=trade.side,
                notional_usd=trade.notional_usd,
                risk_usd=proposed_risk,
            )
        self._last_entry_ts = time.time()
        self._persist_trade_state()
        if self.audit_store:
            self.audit_store.upsert_trade(trade, "open")
        self._audit_event(
            "execution",
            "entry_opened",
            {
                "trade_id": trade.id,
                "side": trade.side,
                "entry_price": trade.entry_price,
                "stop_loss": trade.stop_loss,
                "take_profit": trade.take_profit,
                "notional_usd": trade.notional_usd,
                "setup_type": trade.setup_type,
                "entry_grade": trade.entry_grade,
            },
        )

        # Write to decisions.csv (detailed signal log)
        _log_csv(DECISIONS_CSV, {
            "timestamp":        datetime.utcnow().isoformat(),
            "symbol":           self.symbol,
            "event":            "ENTRY",
            "side":             trade.side,
            "price":            trade.entry_price,
            "stop_loss":        signal.stop_loss,
            "take_profit":      signal.take_profit,
            "lots":             lots,
            "contract_value":   self._lot_size,
            "notional_usd":     round(trade.notional_usd, 4),
            "confidence":       signal.confidence,
            "setup_type":       setup_type,
            "entry_grade":      entry_grade,
            "quality_score":    quality_score,
            "ema_depth_pct":    ema_depth,
            "regime":           regime_entry,
            "htf":              htf_entry,
            "rsi":              rsi_entry,
            "adx":              adx_entry,
            "rsi_component":    quality.get("components", {}).get("rsi", ""),
            "adx_component":    quality.get("components", {}).get("adx", ""),
            "macd_component":   quality.get("components", {}).get("macd", ""),
            "volume_component": quality.get("components", {}).get("volume", ""),
            "pullback_component": quality.get("components", {}).get("pullback", ""),
            "touched_ema21":    quality.get("touched_ema", ""),
        })

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
            return
        close_side = OrderSide.SELL if trade.side == "long" else OrderSide.BUY
        try:
            await self.rest.place_market_order(
                product_id  = self.product_id,
                side        = close_side,
                size        = trade.size,
                reduce_only = True,
            )
            await self.rest.cancel_all_orders(self.product_id)
        except DeltaAPIError as exc:
            if "no_position" in str(exc).lower() or "not_found" in str(exc).lower():
                logger.info("Position already closed by exchange")
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
        if self.portfolio_risk:
            self.portfolio_risk.close_trade(trade.id, pnl)
        self._state_store.clear_trade(self.symbol)
        self._current_trade = None
        if self.audit_store:
            self.audit_store.upsert_trade(trade, "closed")
            self.audit_store.delete_runtime_state("engine", f"active_trade:{self.symbol}")

        pnl_emoji = "✅" if pnl >= 0 else "❌"
        logger.info(
            "%s CLOSED: %s %s | entry=%.4f exit=%.4f pnl=%+.4f | reason=%s | setup=%s grade=%s",
            pnl_emoji, trade.symbol, trade.side.upper(),
            trade.entry_price, exit_price, pnl, reason,
            trade.setup_type or "-", trade.entry_grade or "-",
        )

        # Write to trade_history.csv (analytics-enriched)
        _log_csv(TRADE_HISTORY_CSV, {
            "exit_time":         trade.exit_time.isoformat() if trade.exit_time else "",
            "entry_time":        trade.entry_time.isoformat() if trade.entry_time else "",
            "symbol":            trade.symbol,
            "side":              trade.side,
            "entry_price":       trade.entry_price,
            "exit_price":        exit_price,
            "stop_loss":         trade.stop_loss,
            "take_profit":       trade.take_profit,
            "size_lots":         trade.size,
            "contract_value":    trade.contract_value,
            "notional_usd":      round(trade.notional_usd, 4),
            "pnl":               round(pnl, 4),
            "exit_reason":       reason,
            "setup_type":        trade.setup_type,
            "entry_grade":       trade.entry_grade,
            "quality_score":     trade.entry_quality_score,
            "ema_depth_pct":     trade.ema_depth_pct,
            "regime":            trade.regime_at_entry,
            "htf":               trade.htf_at_entry,
            "rsi_at_entry":      trade.rsi_at_entry,
            "adx_at_entry":      trade.adx_at_entry,
        })

        # Also log to decisions.csv for full audit trail
        _log_csv(DECISIONS_CSV, {
            "timestamp":      datetime.utcnow().isoformat(),
            "symbol":         trade.symbol,
            "event":          "EXIT",
            "side":           trade.side,
            "price":          exit_price,
            "stop_loss":      trade.stop_loss,
            "take_profit":    trade.take_profit,
            "lots":           trade.size,
            "pnl":            round(pnl, 4),
            "reason":         reason,
            "setup_type":     trade.setup_type,
            "entry_grade":    trade.entry_grade,
        })
        self._audit_event(
            "execution",
            "trade_closed",
            {
                "trade_id": trade.id,
                "exit_price": exit_price,
                "pnl": pnl,
                "reason": reason,
                "setup_type": trade.setup_type,
                "entry_grade": trade.entry_grade,
            },
            severity="warning" if pnl < 0 else "info",
        )

        stats = self.risk.get_stats()
        logger.info(
            "📊 Stats: trades=%d win_rate=%.1f%% total_pnl=%.4f equity=%.4f dd=%.1f%%",
            stats.get("total_trades", 0), stats.get("win_rate", 0),
            stats.get("total_pnl", 0), stats.get("current_equity", 0),
            stats.get("drawdown_pct", 0),
        )

        try:
            from notifier import send_exit_alert
            send_exit_alert(trade.symbol, trade.side, trade.entry_price, exit_price, pnl, reason)
        except Exception:
            pass

    def _persist_trade_state(self):
        if self._current_trade and not self._current_trade.closed:
            self._state_store.save_trade(self._current_trade)
            if self.audit_store:
                self.audit_store.set_runtime_state(
                    "engine",
                    f"active_trade:{self.symbol}",
                    {
                        "symbol": self.symbol,
                        "trade_id": self._current_trade.id,
                        "entry_price": self._current_trade.entry_price,
                        "stop_loss": self._current_trade.stop_loss,
                        "take_profit": self._current_trade.take_profit,
                        "side": self._current_trade.side,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                )

    async def _recover_trade_state(self):
        persisted = self._state_store.load_trade(self.symbol)
        if not persisted:
            return
        logger.info("Recovering persisted trade state for %s", self.symbol)
        recovered = await self._reconcile_trade_with_exchange(persisted)
        if recovered and not recovered.closed:
            self._current_trade = recovered
            self.risk.register_trade(recovered)
            if self.portfolio_risk:
                risk_usd = abs(recovered.entry_price - recovered.stop_loss) * recovered.filled_size * recovered.contract_value
                self.portfolio_risk.register_trade(
                    recovered.id,
                    symbol=recovered.symbol,
                    side=recovered.side,
                    notional_usd=recovered.notional_usd,
                    risk_usd=risk_usd,
                )
            self._persist_trade_state()
            if self.audit_store:
                self.audit_store.upsert_trade(recovered, "open")
            logger.info("Recovered live trade: %s %s %d lots @ %.4f",
                        recovered.symbol, recovered.side.upper(),
                        recovered.filled_size, recovered.entry_price)
        else:
            self._state_store.clear_trade(self.symbol)
            if self.audit_store:
                self.audit_store.delete_runtime_state("engine", f"active_trade:{self.symbol}")

    async def _reconcile_trade_with_exchange(self, trade: TradeRecord) -> Optional[TradeRecord]:
        entry_order = await self._fetch_best_order_snapshot(trade.order_id, trade.entry_client_order_id)
        stop_order  = await self._fetch_best_order_snapshot(trade.stop_order_id)
        tp_order    = await self._fetch_best_order_snapshot(trade.take_profit_order_id)
        positions   = await self.rest.get_positions()
        position    = next((p for p in positions if p.symbol == self.symbol and p.size > 0), None)

        if entry_order:
            avg_fill = float(entry_order.get("average_fill_price", 0) or 0)
            raw_size = int(float(entry_order.get("size", 0) or trade.size))
            unfilled = int(float(entry_order.get("unfilled_size", 0) or 0))
            filled   = max(0, raw_size - unfilled)
            if avg_fill > 0:
                trade.entry_price = avg_fill
                trade.peak_price  = avg_fill
                trade.valley_price = avg_fill
            if filled > 0:
                trade.filled_size  = filled
                trade.entry_filled = True

        if position:
            trade.entry_filled = True
            trade.side         = position.side
            trade.filled_size  = max(1, int(abs(position.size)))
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
            trade.exit_time  = datetime.now(timezone.utc)
            trade.reason     = "recovered_closed"
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
