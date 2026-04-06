"""
execution.py — Delta Exchange Execution Engine (PRODUCTION v6)

Architecture:
  - Bracket-first: every entry uses place_bracket_order() (atomic SL/TP on exchange)
  - WebSocket monitoring for trailing SL updates and private order fill events
  - Concurrent tasks: signal loop (candle interval) + WS monitoring (real-time)
  - Orderbook imbalance filter: entry only when OB is aligned with signal
  - AI/ML signal filter: configurable confidence threshold
  - Emergency recovery: closes orphaned positions on startup
  - Double-close prevention via trade.closed flag
  - Rate-limit aware: respects X-RATE-LIMIT-RESET header
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import pandas as pd
import os
import csv

# logging helper
logger = logging.getLogger(__name__)

# decisions CSV helper
DECISIONS_CSV = os.path.join(os.getcwd(), "decisions.csv")

def log_decision_csv(row: dict, file: str = DECISIONS_CSV) -> None:
    """Append a decision row to CSV, creating header if needed."""
    if not isinstance(row, dict):
        return
    write_header = not os.path.exists(file)
    try:
        with open(file, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(row.keys()))
            if write_header:
                writer.writeheader()
            writer.writerow(row)
    except Exception:
        # CSV logging must never crash the engine
        logger.exception("Failed to write decision CSV")

# ─────────────────────────────────────────────────────────────────────────────
# Execution Engine
# ─────────────────────────────────────────────────────────────────────────────

class ExecutionEngine:
    """
    Production execution engine with:
      1. Bracket orders   — exchange-native SL/TP (50ms latency)
      2. WebSocket backup — real-time SL/TP monitor if exchange SL fails
      3. Private WS       — order fill events for exact fill price
      4. OB imbalance     — orderbook filter before entry
      5. Trailing SL      — pushed via edit_bracket_order() on every tick
      6. Emergency close  — handles orphaned positions on startup
    """

    def __init__(
        self,
        rest_client:       DeltaRESTClient,
        strategy:          BaseStrategy,
        risk_manager:      RiskManager,
        symbol:            str,
        product_id:        int,
        resolution_minutes: int = 15,
        api_key:           str = "",
        api_secret:        str = "",
        ob_imbalance_min:  float = 0.0,    # minimum |OB imbalance| to allow entry (0=disabled)
        confidence_min:    float = 0.0,    # minimum signal confidence to allow entry (0=disabled)
        trailing_enabled:  bool = True,
    ):
        self.rest           = rest_client
        self.strategy       = strategy
        self.risk           = risk_manager
        self.symbol         = symbol
        self.product_id     = product_id
        self.resolution     = resolution_minutes
        self.api_key        = api_key
        self.api_secret     = api_secret
        self.ob_imbalance_min = ob_imbalance_min
        self.confidence_min   = confidence_min
        self.trailing_enabled = trailing_enabled

        self._candle_buf: List[Dict] = []
        self._current_trade: Optional[TradeRecord] = None
        self._last_signal_ts: float = 0.0
        self._ws_price: float = 0.0
        self._ws_mark:  float = 0.0
        self._ob:       Optional[L2OrderBook] = None
        self._shutdown  = False
        # Regime detector for adaptive strategy switching
        self.regime = RegimeDetector()
        # last entry timestamp (cooldown between trades)
        self._last_entry_ts: float = 0.0

    # ── Startup ────────────────────────────────────────────────────────────

    async def bootstrap_history(self):
        """
        Prime candle buffer and recover orphaned positions.
        Called once before main loop.
        """
        logger.info("Bootstrapping %s (product_id=%d)…", self.symbol, self.product_id)

        # Fetch initial candle buffer (last 200 bars)
        end   = int(time.time())
        start = end - self.resolution * 60 * 250
        try:
            candles = await self.rest.get_ohlcv(self.symbol, self.resolution, start, end)
            for c in candles:
                self._candle_buf.append({
                    "open": c.open, "high": c.high, "low": c.low,
                    "close": c.close, "volume": c.volume,
                    "timestamp": c.timestamp,
                })
            logger.info("Buffer primed with %d candles", len(self._candle_buf))
        except Exception as exc:
            logger.warning("Could not prime candle buffer: %s", exc)

        # Emergency: close any orphaned positions from previous crash
        positions = await self.rest.get_positions()
        for pos in positions:
            if pos.symbol == self.symbol and pos.size > 0:
                logger.warning(
                    "⚠️ ORPHANED POSITION FOUND: %s %s %.4f @ %.2f — closing immediately!",
                    pos.symbol, pos.side, pos.size, pos.entry_price
                )
                await self._force_close_position(pos)

        # Set leverage
        leverage = self.risk.config.leverage
        await self.rest.set_leverage(self.product_id, int(leverage))
        logger.info("Leverage set to %dx", int(leverage))

    async def _force_close_position(self, pos) -> bool:
        """Emergency close: place market reduce-only order."""
        from api import Order, OrderType, OrderSide
        side = OrderSide.SELL if pos.side == "long" else OrderSide.BUY
        lots = self.rest.usd_to_lots(self.symbol, pos.size * pos.mark_price, pos.mark_price)
        lots = max(1, int(pos.size))  # use raw size for positions
        order = Order(
            product_id  = self.product_id,
            side        = side,
            order_type  = OrderType.MARKET,
            size        = lots,
            reduce_only = True,
        )
        try:
            await self.rest.place_order(order)
            logger.info("✅ Orphaned position closed: %s", pos.symbol)
            return True
        except Exception as exc:
            logger.error("❌ Force-close failed: %s", exc)
            return False

    # ── Main Loop ──────────────────────────────────────────────────────────

    async def run_polling(self, interval_seconds: Optional[int] = None):
        """
        Main entry point. Runs two concurrent tasks:
          1. WebSocket task: real-time price updates, OB updates, private fills
          2. Signal task: candle-based signal generation + order execution
        """
        await self.bootstrap_history()

        interval = interval_seconds or (self.resolution * 60)

        # Build WebSocket client with public + private subscriptions
        ws_client = DeltaWSClient(self.api_key, self.api_secret, self._handle_ws_message)
        ws_client.subscribe_public("v2/ticker",    [self.symbol])
        ws_client.subscribe_public("l2_orderbook", [self.symbol])
        ws_client.subscribe_public("all_trades",   [self.symbol])
        ws_client.subscribe_private(["orders", "positions", "user_trades"])

        ws_task     = asyncio.create_task(ws_client.connect(), name="ws")
        signal_task = asyncio.create_task(
            self._signal_loop(interval), name="signal"
        )

        logger.info(
            "🚀 ExecutionEngine running — %s | %dm candles | interval=%ds",
            self.symbol, self.resolution, interval
        )

        try:
            await asyncio.gather(ws_task, signal_task)
        except asyncio.CancelledError:
            pass
        finally:
            self._shutdown = True
            await ws_client.disconnect()
            logger.info("ExecutionEngine stopped")

    # ── WebSocket Handler ──────────────────────────────────────────────────

    async def _handle_ws_message(self, msg: Dict):
        """Dispatcher for all WebSocket messages."""
        msg_type = msg.get("type", "")

        if msg_type in ("v2/ticker", "ticker"):
            await self._handle_ticker(msg)

        elif msg_type in ("l2_orderbook",):
            await self._handle_orderbook(msg)

        elif msg_type == "orders":
            # Private: order status update (fills, cancellations)
            await self._handle_order_update(msg)

        elif msg_type == "user_trades":
            # Private: your trade fill
            await self._handle_fill_event(msg)

        elif msg_type == "positions":
            # Private: position update
            await self._handle_position_update(msg)

    async def _handle_ticker(self, msg: Dict):
        """Update live price; check WebSocket SL/TP as backup."""
        payload = msg.get("payload", msg)
        try:
            self._ws_price = float(payload.get("last_price", 0) or payload.get("close", 0))
            self._ws_mark  = float(payload.get("mark_price", 0) or self._ws_price)
        except (TypeError, ValueError):
            return

        if not self._current_trade or self._current_trade.closed:
            return

        price = self._ws_price
        trade = self._current_trade

        # WebSocket SL backup (primary SL is on exchange via bracket order)
        if trade.side == "long":
            if price <= trade.stop_loss and not trade.closed:
                logger.warning("🛑 WS SL BACKUP HIT: %s price=%.4f sl=%.4f", self.symbol, price, trade.stop_loss)
                await self._execute_close(trade, price, "ws_sl_backup")
        else:
            if price >= trade.stop_loss and not trade.closed:
                logger.warning("🛑 WS SL BACKUP HIT: %s price=%.4f sl=%.4f", self.symbol, price, trade.stop_loss)
                await self._execute_close(trade, price, "ws_sl_backup")

        # WebSocket TP backup
        if trade.take_profit:
            if trade.side == "long" and price >= trade.take_profit and not trade.closed:
                logger.info("🎯 WS TP BACKUP HIT: %s price=%.4f tp=%.4f", self.symbol, price, trade.take_profit)
                await self._execute_close(trade, price, "ws_tp_backup")
            elif trade.side == "short" and price <= trade.take_profit and not trade.closed:
                logger.info("🎯 WS TP BACKUP HIT: %s price=%.4f tp=%.4f", self.symbol, price, trade.take_profit)
                await self._execute_close(trade, price, "ws_tp_backup")

        # Update trailing stop via edit_bracket_order (push to exchange)
        if self.trailing_enabled and trade.order_id:
            new_sl = self.risk.update_trailing_stop(trade, price)
            if new_sl and new_sl != trade.stop_loss:
                updated = await self.rest.edit_bracket_order(
                    order_id  = int(trade.order_id),
                    product_id = self.product_id,
                    stop_loss_price = new_sl,
                )
                if updated:
                    logger.debug("📈 Trailing SL updated: %.4f → %.4f", trade.stop_loss, new_sl)
                    trade.stop_loss = new_sl

    async def _handle_orderbook(self, msg: Dict):
        """Update L2 order book snapshot."""
        payload = msg.get("payload", msg)
        buy  = payload.get("buy", [])
        sell = payload.get("sell", [])
        self._ob = L2OrderBook(symbol=self.symbol, buy=buy, sell=sell)

    async def _handle_order_update(self, msg: Dict):
        """Handle private order fill/cancel events."""
        payload = msg.get("payload", msg)
        state   = payload.get("state", "")
        oid     = str(payload.get("id", ""))

        if self._current_trade and str(self._current_trade.order_id) == oid:
            if state in ("filled", "closed"):
                logger.info("📋 Order %s filled at %s", oid, payload.get("avg_fill_price"))

    async def _handle_fill_event(self, msg: Dict):
        """Handle private user fill events (exact fill price)."""
        payload  = msg.get("payload", msg)
        fill_oid = str(payload.get("order_id", ""))

        if self._current_trade and str(self._current_trade.order_id) == fill_oid:
            fill_price = float(payload.get("price", 0))
            if fill_price > 0 and not self._current_trade.closed:
                logger.info("💰 Fill confirmed: order=%s price=%.4f", fill_oid, fill_price)
                # Update entry price with actual fill
                self._current_trade.entry_price = fill_price

    async def _handle_position_update(self, msg: Dict):
        """Handle private position update events."""
        payload    = msg.get("payload", msg)
        product_id = payload.get("product_id")
        size       = float(payload.get("size", 0))

        if product_id == self.product_id:
            if size == 0 and self._current_trade and not self._current_trade.closed:
                # Position went to zero — exchange SL/TP fired
                close_price = self._ws_price
                logger.info("🏁 Position closed by exchange SL/TP at ~%.4f", close_price)
                await self._mark_trade_closed(self._current_trade, close_price, "exchange_bracket")

    # ── Signal Loop ────────────────────────────────────────────────────────

    async def _signal_loop(self, interval_seconds: int):
        """Fetch candles and generate signals on each interval."""
        while not self._shutdown:
            await asyncio.sleep(interval_seconds)
            if self._shutdown:
                break
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Signal loop error: %s", exc, exc_info=True)

    async def _tick(self):
        """One signal generation cycle."""
        # Fetch latest candle
        end   = int(time.time())
        start = end - self.resolution * 60 * 5
        try:
            candles = await self.rest.get_ohlcv(self.symbol, self.resolution, start, end)
            if candles:
                latest = candles[-1]
                new_bar = {
                    "open": latest.open, "high": latest.high,
                    "low": latest.low,   "close": latest.close,
                    "volume": latest.volume, "timestamp": latest.timestamp,
                }
                if not self._candle_buf or self._candle_buf[-1]["timestamp"] != latest.timestamp:
                    self._candle_buf.append(new_bar)
                    self._candle_buf = self._candle_buf[-300:]  # keep last 300 bars
        except Exception as exc:
            logger.warning("OHLCV fetch failed: %s", exc)
            return

        if len(self._candle_buf) < 50:
            logger.info("Buffer too short (%d bars), waiting…", len(self._candle_buf))
            return

        # Build DataFrame for strategy
        df = pd.DataFrame(self._candle_buf)
        df.index = pd.to_datetime(df["timestamp"], unit="s", utc=True)
        df = df.drop(columns=["timestamp"])

        # Get current price
        try:
            ticker = await self.rest.get_ticker(self.symbol)
            price  = ticker.last_price
        except Exception:
            price = float(df["close"].iloc[-1])

        # Risk checks before signal generation
        balance = await self.rest.get_wallet_balance("USDT")
        self.risk.update_equity(balance)

        if not self.risk.can_trade():
            logger.warning("⛔ Risk manager: trading halted (drawdown/daily-loss limit)")
            return

        if self._current_trade and not self._current_trade.closed:
            if self.risk.get_open_trade_count() >= self.risk.config.max_open_trades:
                return  # Already at max positions

        # Generate signal
        try:
            signal = self.strategy.generate_signal(df, self.symbol)
        except Exception as exc:
            logger.warning("Strategy error: %s", exc)
            return

        if not signal or signal.type == SignalType.NEUTRAL:
            return

        # Precompute observability metrics for CSV logging
        from datetime import datetime as _dt
        # ensure regime exists before any early-returns that log it
        regime = None
        hour = _dt.utcnow().hour
        avg_v = df["volume"].rolling(20).mean().iloc[-1] if len(df) >= 20 else float("nan")
        curr_v = df["volume"].iloc[-1]
        vol_ratio = None if pd.isna(avg_v) or avg_v == 0 else float(curr_v / avg_v)

        # EMA slope proxy (8-period over 5 bars)
        try:
            ema8 = df["close"].ewm(span=8, adjust=False).mean()
            ema_slope = float((ema8.iloc[-1] - ema8.iloc[-5]) / ema8.iloc[-5]) if len(ema8) >= 5 and ema8.iloc[-5] != 0 else None
        except Exception:
            ema_slope = None

        # local liquidity sweep detection
        last_high = df["high"].iloc[-5:-1].max() if len(df) >= 6 else None
        last_low = df["low"].iloc[-5:-1].min() if len(df) >= 6 else None
        liquidity_sweep = False
        if signal.type == SignalType.LONG and last_low is not None and df["low"].iloc[-1] < last_low:
            liquidity_sweep = True
        if signal.type == SignalType.SHORT and last_high is not None and df["high"].iloc[-1] > last_high:
            liquidity_sweep = True

        # HTF trend (50/200)
        try:
            ema50 = df["close"].ewm(span=50, adjust=False).mean().iloc[-1]
            ema200 = df["close"].ewm(span=200, adjust=False).mean().iloc[-1]
            if ema50 > ema200:
                htf = "bull"
            elif ema50 < ema200:
                htf = "bear"
            else:
                htf = "neutral"
        except Exception:
            htf = "neutral"

        # orderbook metrics
        imb = None
        spread = None
        if self._ob is not None:
            try:
                imb = self._ob.imbalance(levels=5)
            except Exception:
                imb = None
            try:
                spread = self._ob.spread()
            except Exception:
                spread = None

        boost_session = False
        if hour < 6:
            # log CSV and skip
            log_decision_csv({
                "timestamp": _dt.utcnow().isoformat(),
                "symbol": self.symbol,
                "regime": regime,
                "htf_trend": htf,
                "signal_type": signal.type.name,
                "confidence": getattr(signal, "confidence", 1.0),
                "volume_ratio": vol_ratio,
                "ema_slope": ema_slope,
                "liquidity_sweep": liquidity_sweep,
                "atr": atr_now if 'atr_now' in locals() else None,
                "atr_ma": atr_ma if 'atr_ma' in locals() else None,
                "spread": spread,
                "ob_imbalance": imb,
                "decision": "SKIP",
                "reason": "session",
            })
            logger.info("[SKIP] reason=session hour=%d (low liquidity)", hour)
            return
        if 13 <= hour <= 17:
            boost_session = True

        # Volatility filter: skip very low ATR regimes
        atr_now = None
        atr_ma = None
        try:
            atr_s = self.strategy.atr(df, 14)
            atr_now = float(atr_s.iloc[-1])
            atr_ma = float(atr_s.rolling(30).mean().iloc[-1])
            if atr_ma > 0 and atr_now < atr_ma * 0.7:
                log_decision_csv({
                    "timestamp": _dt.utcnow().isoformat(),
                    "symbol": self.symbol,
                    "regime": regime,
                    "htf_trend": htf,
                    "signal_type": signal.type.name,
                    "confidence": getattr(signal, "confidence", 1.0),
                    "volume_ratio": vol_ratio,
                    "ema_slope": ema_slope,
                    "liquidity_sweep": liquidity_sweep,
                    "atr": atr_now,
                    "atr_ma": atr_ma,
                    "spread": spread,
                    "ob_imbalance": imb,
                    "decision": "SKIP",
                    "reason": "low_vol",
                })
                logger.info("[SKIP] reason=low_vol atr=%.6f atr_ma=%.6f", atr_now, atr_ma)
                return
        except Exception:
            # if ATR calc fails, continue (do not block on indicator errors)
            pass

        # Regime-based confidence weighting (soft switch)
        regime = self.regime.detect_regime(df)
        if regime is None:
            logger.debug("Regime unknown — proceeding without weighting")
        else:
            if regime == "volatile":
                log_decision_csv({
                    "timestamp": _dt.utcnow().isoformat(),
                    "symbol": self.symbol,
                    "regime": regime,
                    "htf_trend": htf,
                    "signal_type": signal.type.name,
                    "confidence": getattr(signal, "confidence", 1.0),
                    "volume_ratio": vol_ratio,
                    "ema_slope": ema_slope,
                    "liquidity_sweep": liquidity_sweep,
                    "atr": atr_now if atr_now is not None else None,
                    "atr_ma": atr_ma if atr_ma is not None else None,
                    "spread": spread,
                    "ob_imbalance": imb,
                    "decision": "SKIP",
                    "reason": "regime_volatile",
                })
                logger.info("[SKIP] reason=regime=volatile")
                return
            if regime == "trend":
                signal.confidence = getattr(signal, "confidence", 1.0) * 1.2
            elif regime == "range":
                signal.confidence = getattr(signal, "confidence", 1.0) * 0.8

        # Session boost
        if boost_session:
            signal.confidence = min(1.0, getattr(signal, "confidence", 1.0) * 1.15)

        # Confidence filter (AI/ML threshold)
        if self.confidence_min > 0:
            conf = getattr(signal, "confidence", 1.0)
            if conf < self.confidence_min:
                log_decision_csv({
                    "timestamp": _dt.utcnow().isoformat(),
                    "symbol": self.symbol,
                    "regime": regime,
                    "htf_trend": htf,
                    "signal_type": signal.type.name,
                    "confidence": conf,
                    "volume_ratio": vol_ratio,
                    "ema_slope": ema_slope,
                    "liquidity_sweep": liquidity_sweep,
                    "atr": atr_now if atr_now is not None else None,
                    "atr_ma": atr_ma if atr_ma is not None else None,
                    "spread": spread,
                    "ob_imbalance": imb,
                    "decision": "SKIP",
                    "reason": "low_confidence",
                })
                logger.info("[SKIP] reason=confidence conf=%.2f < %.2f", conf, self.confidence_min)
                return

        # Trade frequency cooldown
        if time.time() - self._last_entry_ts < 900:
            remaining = 900 - (time.time() - self._last_entry_ts)
            log_decision_csv({
                "timestamp": _dt.utcnow().isoformat(),
                "symbol": self.symbol,
                "regime": regime,
                "htf_trend": htf,
                "signal_type": signal.type.name,
                "confidence": getattr(signal, "confidence", 1.0),
                "volume_ratio": vol_ratio,
                "ema_slope": ema_slope,
                "liquidity_sweep": liquidity_sweep,
                "atr": atr_now if atr_now is not None else None,
                "atr_ma": atr_ma if atr_ma is not None else None,
                "spread": spread,
                "ob_imbalance": imb,
                "decision": "SKIP",
                "reason": "cooldown",
            })
            logger.info("[SKIP] reason=cooldown remaining=%.0fs", remaining)
            return

        # Higher-timeframe trend filter: only trade with 50/200 EMA direction
        def _htf_trend(local_df: pd.DataFrame) -> str:
            ema50 = local_df["close"].ewm(span=50, adjust=False).mean().iloc[-1]
            ema200 = local_df["close"].ewm(span=200, adjust=False).mean().iloc[-1]
            if ema50 > ema200:
                return "bull"
            if ema50 < ema200:
                return "bear"
            return "neutral"

        htf = _htf_trend(df)
        if signal.type == SignalType.LONG and htf != "bull":
            log_decision_csv({
                "timestamp": _dt.utcnow().isoformat(),
                "symbol": self.symbol,
                "regime": regime,
                "htf_trend": htf,
                "signal_type": signal.type.name,
                "confidence": getattr(signal, "confidence", 1.0),
                "volume_ratio": vol_ratio,
                "ema_slope": ema_slope,
                "liquidity_sweep": liquidity_sweep,
                "atr": atr_now if atr_now is not None else None,
                "atr_ma": atr_ma if atr_ma is not None else None,
                "spread": spread,
                "ob_imbalance": imb,
                "decision": "SKIP",
                "reason": "htf_mismatch",
            })
            logger.info("[SKIP] reason=htf_mismatch htf=%s", htf)
            return
        if signal.type == SignalType.SHORT and htf != "bear":
            log_decision_csv({
                "timestamp": _dt.utcnow().isoformat(),
                "symbol": self.symbol,
                "regime": regime,
                "htf_trend": htf,
                "signal_type": signal.type.name,
                "confidence": getattr(signal, "confidence", 1.0),
                "volume_ratio": vol_ratio,
                "ema_slope": ema_slope,
                "liquidity_sweep": liquidity_sweep,
                "atr": atr_now if atr_now is not None else None,
                "atr_ma": atr_ma if atr_ma is not None else None,
                "spread": spread,
                "ob_imbalance": imb,
                "decision": "SKIP",
                "reason": "htf_mismatch",
            })
            logger.info("[SKIP] reason=htf_mismatch htf=%s", htf)
            return

        # Orderbook imbalance filter
        imb = None
        if self.ob_imbalance_min > 0 and self._ob is not None:
            imb = self._ob.imbalance(levels=5)
            if signal.type == SignalType.LONG and imb < self.ob_imbalance_min:
                log_decision_csv({
                    "timestamp": _dt.utcnow().isoformat(),
                    "symbol": self.symbol,
                    "regime": regime,
                    "htf_trend": htf,
                    "signal_type": signal.type.name,
                    "confidence": getattr(signal, "confidence", 1.0),
                    "volume_ratio": vol_ratio,
                    "ema_slope": ema_slope,
                    "liquidity_sweep": liquidity_sweep,
                    "atr": atr_now if atr_now is not None else None,
                    "atr_ma": atr_ma if atr_ma is not None else None,
                    "spread": spread,
                    "ob_imbalance": imb,
                    "decision": "SKIP",
                    "reason": "ob_imbalance_low",
                })
                logger.info("[SKIP] reason=ob_imbalance imb=%.3f < %.3f", imb, self.ob_imbalance_min)
                return
            if signal.type == SignalType.SHORT and imb > -self.ob_imbalance_min:
                log_decision_csv({
                    "timestamp": _dt.utcnow().isoformat(),
                    "symbol": self.symbol,
                    "regime": regime,
                    "htf_trend": htf,
                    "signal_type": signal.type.name,
                    "confidence": getattr(signal, "confidence", 1.0),
                    "volume_ratio": vol_ratio,
                    "ema_slope": ema_slope,
                    "liquidity_sweep": liquidity_sweep,
                    "atr": atr_now if atr_now is not None else None,
                    "atr_ma": atr_ma if atr_ma is not None else None,
                    "spread": spread,
                    "ob_imbalance": imb,
                    "decision": "SKIP",
                    "reason": "ob_imbalance_not_bearish",
                })
                logger.info("[SKIP] reason=ob_imbalance imb=%.3f not bearish", imb)
                return

        # Spread / liquidity filter — avoid wide spreads
        spread = None
        if self._ob is not None:
            spread = self._ob.spread()
            if spread is not None and spread > price * 0.001:
                log_decision_csv({
                    "timestamp": _dt.utcnow().isoformat(),
                    "symbol": self.symbol,
                    "regime": regime,
                    "htf_trend": htf,
                    "signal_type": signal.type.name,
                    "confidence": getattr(signal, "confidence", 1.0),
                    "volume_ratio": vol_ratio,
                    "ema_slope": ema_slope,
                    "liquidity_sweep": liquidity_sweep,
                    "atr": atr_now if atr_now is not None else None,
                    "atr_ma": atr_ma if atr_ma is not None else None,
                    "spread": spread,
                    "ob_imbalance": imb,
                    "decision": "SKIP",
                    "reason": "wide_spread",
                })
                logger.info("[SKIP] reason=wide_spread spread=%.6f price_thresh=%.6f", spread, price * 0.001)
                return

        # Observability / decision logging
        avg_v = df["volume"].rolling(20).mean().iloc[-1]
        curr_v = df["volume"].iloc[-1]
        vol_ok = (curr_v >= avg_v) if not pd.isna(avg_v) and avg_v > 0 else None
        # local liquidity sweep detection for extra visibility
        last_high = df["high"].iloc[-5:-1].max() if len(df) >= 6 else None
        last_low = df["low"].iloc[-5:-1].min() if len(df) >= 6 else None
        liquidity_sweep = False
        if signal.type == SignalType.LONG and last_low is not None and df["low"].iloc[-1] < last_low:
            liquidity_sweep = True
        if signal.type == SignalType.SHORT and last_high is not None and df["high"].iloc[-1] > last_high:
            liquidity_sweep = True

        logger.info(
            "[DECISION] regime=%s htf=%s conf=%.2f vol_ok=%s sweep=%s atr=%s atr_ma=%s spread=%s ob=%s",
            regime, htf, getattr(signal, "confidence", 1.0),
            vol_ok, liquidity_sweep,
            f"{atr_now:.6f}" if atr_now is not None else "n/a",
            f"{atr_ma:.6f}" if atr_ma is not None else "n/a",
            f"{spread:.6f}" if spread is not None else "n/a",
            f"{imb:.3f}" if imb is not None else "n/a",
        )

        context = {
            "regime": regime,
            "htf": htf,
            "vol_ratio": vol_ratio,
            "ema_slope": ema_slope,
            "liquidity_sweep": liquidity_sweep,
            "atr": atr_now if 'atr_now' in locals() else None,
            "atr_ma": atr_ma if 'atr_ma' in locals() else None,
            "spread": spread,
            "imb": imb,
        }
        await self._execute_entry(signal, price, context)

    # ── Entry ──────────────────────────────────────────────────────────────

    async def _execute_entry(self, signal: Signal, price: float, context: dict):
        """Place bracket order for new entry."""
        if self._current_trade and not self._current_trade.closed:
            logger.debug("Skipping entry — trade already open")
            return

        # Position sizing
        equity = self.risk.current_equity
        sl_dist = abs(price - (signal.stop_loss or price * 0.99))
        usd_notional = self.risk.calculate_position_size(equity, price, sl_dist)

        if usd_notional <= 0:
            logger.warning("Position size = 0, skipping entry")
            return

        lots = self.rest.usd_to_lots(self.symbol, usd_notional, price)
        if lots < 1:
            logger.warning("Lot size < 1, skipping entry")
            return

        side = OrderSide.BUY if signal.type == SignalType.LONG else OrderSide.SELL

        client_oid = f"bot_{self.symbol}_{int(time.time())}"

        try:
            result = await self.rest.place_bracket_order(
                product_id        = self.product_id,
                side              = side,
                size              = lots,
                stop_loss_price   = signal.stop_loss,
                take_profit_price = signal.take_profit,
                trigger_method    = StopTriggerMethod.LAST_TRADED_PRICE,
                client_order_id   = client_oid,
            )
        except DeltaAPIError as exc:
            logger.error("❌ Bracket order failed: %s", exc)
            return

        # Register trade in risk manager
        trade_side = "long" if signal.type == SignalType.LONG else "short"
        trade = TradeRecord(
            id          = str(uuid.uuid4()),
            symbol      = self.symbol,
            side        = trade_side,
            entry_price = price,
            size        = lots,
            stop_loss   = signal.stop_loss or 0.0,
            take_profit = signal.take_profit,
            entry_time  = datetime.now(timezone.utc),
            order_id    = result.entry_order_id,
        )
        self._current_trade = trade
        self.risk.register_trade(trade)
        self._last_entry_ts = time.time()

        # Log ENTRY to decisions CSV for downstream analysis
        try:
            log_decision_csv({
                "timestamp": datetime.utcnow().isoformat(),
                "symbol": self.symbol,
                "regime": context.get("regime"),
                "htf_trend": context.get("htf"),
                "signal_type": signal.type.name,
                "confidence": getattr(signal, "confidence", 1.0),
                "volume_ratio": context.get("vol_ratio"),
                "ema_slope": context.get("ema_slope"),
                "liquidity_sweep": context.get("liquidity_sweep"),
                "atr": context.get("atr"),
                "atr_ma": context.get("atr_ma"),
                "spread": context.get("spread"),
                "ob_imbalance": context.get("imb"),
                "decision": "ENTRY",
                "reason": "",
            })
        except Exception:
            logger.exception("Failed to write ENTRY to decision CSV")

        logger.info(
            "✅ ENTRY: %s %s %d lots @ %.4f | SL=%.4f | TP=%s | order=%s",
            self.symbol, trade_side.upper(), lots, price,
            signal.stop_loss or 0,
            f"{signal.take_profit:.4f}" if signal.take_profit else "NONE",
            result.entry_order_id,
        )

        send_trade_alert({
            "symbol": self.symbol, "side": trade_side,
            "entry_price": price, "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit, "size": lots,
        })

    # ── Exit ───────────────────────────────────────────────────────────────

    async def _execute_close(self, trade: TradeRecord, price: float, reason: str):
        """Place market close order and update trade record."""
        if trade.closed:
            return  # Double-close prevention

        from api import Order, OrderType as OT, OrderSide as OS
        close_side = OS.SELL if trade.side == "long" else OS.BUY
        order = Order(
            product_id  = self.product_id,
            side        = close_side,
            order_type  = OT.MARKET,
            size        = trade.size,
            reduce_only = True,
        )
        try:
            await self.rest.place_order(order)
            # Cancel any remaining bracket orders
            await self.rest.cancel_all_orders(self.product_id)
        except DeltaAPIError as exc:
            if "no_position" in str(exc).lower():
                logger.info("Position already closed by exchange (bracket SL/TP fired)")
            else:
                logger.error("Close order failed: %s", exc)

        await self._mark_trade_closed(trade, price, reason)

    async def _mark_trade_closed(self, trade: TradeRecord, exit_price: float, reason: str):
        """Mark trade as closed and update risk manager."""
        if trade.closed:
            return

        trade.closed     = True
        trade.exit_price = exit_price
        trade.exit_time  = datetime.now(timezone.utc)
        trade.reason     = reason

        pnl_mult = 1 if trade.side == "long" else -1
        pnl = pnl_mult * (exit_price - trade.entry_price) / trade.entry_price * trade.size * trade.entry_price
        self.risk.close_trade(trade, exit_price)

        logger.info(
            "🏁 CLOSED: %s %s | entry=%.4f exit=%.4f | reason=%s",
            trade.symbol, trade.side.upper(),
            trade.entry_price, exit_price, reason
        )
        try:
            from notifier import send_exit_alert
            send_exit_alert(trade.symbol, trade.side, trade.entry_price, exit_price, pnl, reason)
        except Exception:
            logger.exception("Failed to send exit alert")

        self._current_trade = None


__all__ = ["ExecutionEngine"]
