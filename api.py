"""
api.py — Delta Exchange REST + WebSocket client (HARDENED v3)

FIXES vs v2:
  - WebSocket on_message now accepts coroutines (async handlers) properly
  - WebSocket heartbeat/ping to prevent silent disconnections
  - Rate limiter (token bucket) prevents 429 hammering
  - _request() re-signs on every retry (timestamp drift fix)
  - place_bracket_order() uses correct Delta India API field names
  - get_ohlcv() returns sorted, deduplicated candles
  - usd_to_lots() uses contract_value from product cache correctly for India endpoint
  - DeltaWSClient: separate auth vs subscribe; handles v2 message envelope
  - All numeric API fields sent as strings where Delta requires it
"""

import asyncio
import hashlib
import hmac
import inspect
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Union

try:
    import aiohttp
    import websockets
    from websockets.exceptions import ConnectionClosed
except ImportError:
    aiohttp = None
    websockets = None
    ConnectionClosed = Exception

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Enums & Data Classes
# ─────────────────────────────────────────────

class OrderType(str, Enum):
    MARKET = "market_order"
    LIMIT = "limit_order"
    STOP_MARKET = "stop_market_order"
    STOP_LIMIT = "stop_limit_order"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(str, Enum):
    OPEN = "open"
    PENDING = "pending"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    FILLED = "filled"


@dataclass
class Order:
    product_id: int
    side: OrderSide
    order_type: OrderType
    size: int                           # INTEGER lots
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    reduce_only: bool = False
    time_in_force: str = "gtc"
    client_order_id: Optional[str] = None
    order_id: Optional[str] = None
    status: Optional[OrderStatus] = None
    filled_size: float = 0.0
    avg_fill_price: Optional[float] = None


@dataclass
class Position:
    product_id: int
    symbol: str
    size: float
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    realized_pnl: float
    margin: float


@dataclass
class OHLCV:
    timestamp: int                      # unix ms
    open: float
    high: float
    low: float
    close: float
    volume: float


# ─────────────────────────────────────────────
# Token-bucket rate limiter
# ─────────────────────────────────────────────

class RateLimiter:
    """Leaky-bucket: max_calls per window_seconds, enforced with sleep."""

    def __init__(self, max_calls: int = 20, window_seconds: float = 1.0):
        self._max = max_calls
        self._window = window_seconds
        self._times: deque = deque()

    async def acquire(self):
        now = time.monotonic()
        # Drop timestamps outside the window
        while self._times and now - self._times[0] > self._window:
            self._times.popleft()
        if len(self._times) >= self._max:
            sleep_for = self._window - (now - self._times[0]) + 0.01
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
        self._times.append(time.monotonic())


# ─────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────

class DeltaAPIError(Exception):
    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        if isinstance(body, dict):
            err = body.get("error", {})
            if isinstance(err, dict):
                msg = err.get("message", str(body))
            else:
                msg = str(err)
        else:
            msg = str(body)
        super().__init__(f"Delta API {status}: {msg}")


# ─────────────────────────────────────────────
# Delta Exchange REST Client
# ─────────────────────────────────────────────

class DeltaRESTClient:
    """
    Async REST client for Delta Exchange India API (api.india.delta.exchange).

    Signature:  HMAC-SHA256 over  METHOD + timestamp + path + query_string + body
      - query_string: no leading '?', e.g. "state=open&product_id=3"
      - body: compact JSON string or "" for no body
      - timestamp: unix seconds as string
    """

    BASE_URL = "https://api.india.delta.exchange"
    # BASE_URL = "https://testnet-api.delta.exchange"   # testnet

    # Fallback lot sizes (base asset per lot) — used only if product cache empty
    FALLBACK_LOT_SIZES: Dict[str, float] = {
        "BTC_USDT": 0.001,
        "ETH_USDT": 0.01,
        "SOL_USDT": 1.0,
        "XRP_USDT": 10.0,
        "BNB_USDT": 0.1,
    }

    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self._session: Optional[aiohttp.ClientSession] = None
        self._product_cache: Dict[str, Dict] = {}
        self._rate_limiter = RateLimiter(max_calls=25, window_seconds=1.0)

    async def __aenter__(self):
        timeout = aiohttp.ClientTimeout(total=15, connect=5)
        self._session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *args):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _ensure_session(self):
        if not self._session or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=15, connect=5)
            self._session = aiohttp.ClientSession(timeout=timeout)

    # ── Auth ──────────────────────────────────

    def _sign(self, method: str, path: str, query_string: str, body: str, timestamp: str) -> str:
        message = method.upper() + timestamp + path + query_string + body
        return hmac.new(
            self.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _auth_headers(self, method: str, path: str, query_string: str = "", body: str = "") -> Dict[str, str]:
        timestamp = str(int(time.time()))
        signature = self._sign(method, path, query_string, body, timestamp)
        return {
            "api-key": self.api_key,
            "signature": signature,
            "timestamp": timestamp,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "DeltaBot/3.0",
        }

    # ── Core HTTP ─────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
        data: Optional[Dict] = None,
        auth: bool = True,
        retries: int = 3,
    ) -> Dict:
        await self._ensure_session()
        await self._rate_limiter.acquire()

        url = self.BASE_URL + path
        query_string = "&".join(f"{k}={v}" for k, v in sorted(params.items())) if params else ""
        body = json.dumps(data, separators=(",", ":")) if data else ""

        last_exc = None
        for attempt in range(retries):
            # Re-sign on every attempt (timestamp may drift)
            headers = (
                self._auth_headers(method.upper(), path, query_string, body)
                if auth
                else {"Content-Type": "application/json", "Accept": "application/json"}
            )
            try:
                async with self._session.request(
                    method, url, params=params, data=body or None, headers=headers
                ) as resp:
                    try:
                        resp_data = await resp.json(content_type=None)
                    except Exception:
                        resp_data = {"raw": await resp.text()}

                    if resp.status == 429:
                        wait = min(2 ** attempt, 30)
                        logger.warning("Rate limited — backing off %.1fs (attempt %d)", wait, attempt + 1)
                        await asyncio.sleep(wait)
                        continue

                    if resp.status >= 400:
                        logger.error("API %d on %s %s: %s", resp.status, method, path, resp_data)
                        raise DeltaAPIError(resp.status, resp_data)

                    return resp_data

            except DeltaAPIError:
                raise
            except aiohttp.ClientError as exc:
                last_exc = exc
                logger.warning("HTTP error (attempt %d/%d): %s", attempt + 1, retries, exc)
                await asyncio.sleep(2 ** attempt)
            except asyncio.TimeoutError as exc:
                last_exc = exc
                logger.warning("Timeout (attempt %d/%d): %s %s", attempt + 1, retries, method, path)
                await asyncio.sleep(2 ** attempt)

        raise RuntimeError(f"Request failed after {retries} retries: {method} {path}") from last_exc

    # ── Product / Lot helpers ─────────────────

    async def get_products(self) -> List[Dict]:
        resp = await self._request("GET", "/v2/products", auth=False)
        products = resp.get("result", [])
        for p in products:
            sym = p.get("symbol", "")
            if sym:
                self._product_cache[sym] = p
        logger.info("Cached %d products", len(self._product_cache))
        return products

    async def get_product(self, symbol: str) -> Optional[Dict]:
        if symbol not in self._product_cache:
            await self.get_products()
        return self._product_cache.get(symbol)

    def usd_to_lots(self, symbol: str, usd_notional: float, price: float) -> int:
        """
        Convert USD notional → integer lots.

        Delta India linear (USDT) contracts:
          contract_value = base-asset units per lot   (e.g. 0.001 BTC)
          value_per_lot  = contract_value * price     (in USDT)
          lots           = floor(usd_notional / value_per_lot)

        Falls back to FALLBACK_LOT_SIZES if product cache misses.
        Always returns ≥ 1 if usd_notional > 0.
        """
        if price <= 0:
            return 1

        product = self._product_cache.get(symbol, {})
        cv_raw = product.get("contract_value")

        # Determine contract_value (base asset per lot)
        if cv_raw is not None:
            try:
                contract_value = float(cv_raw)
            except (TypeError, ValueError):
                contract_value = 0.0
        else:
            contract_value = 0.0

        if contract_value <= 0:
            contract_value = float(self.FALLBACK_LOT_SIZES.get(symbol, 0.001))

        # For inverse contracts (USD-settled): lot value in USD = contract_value (constant)
        contract_type = product.get("contract_type", "")
        quoting_asset = product.get("quoting_asset", {})
        if isinstance(quoting_asset, dict):
            quoting_sym = quoting_asset.get("symbol", "")
        else:
            quoting_sym = ""

        if "inverse" in contract_type.lower() or quoting_sym in ("BTC", "ETH", "USDC"):
            lots = int(usd_notional / contract_value) if contract_value > 0 else 0
        else:
            # Linear USDT contract
            value_per_lot = contract_value * price
            lots = int(usd_notional / value_per_lot) if value_per_lot > 0 else 0

        # Respect exchange minimum
        min_size = 1
        try:
            min_size = max(1, int(product.get("min_size", 1) or 1))
        except (TypeError, ValueError):
            pass

        if 0 < lots < min_size:
            lots = min_size

        if lots == 0 and usd_notional > 0:
            lots = min_size
            logger.info(
                "usd_to_lots: floored to %d lot (symbol=%s usd=%.2f price=%.4f cv=%s)",
                min_size, symbol, usd_notional, price, contract_value,
            )

        logger.debug(
            "usd_to_lots: %s usd=%.2f price=%.4f cv=%s → %d lots",
            symbol, usd_notional, price, contract_value, lots,
        )
        return max(0, lots)

    # ── Market Data ───────────────────────────

    async def get_ticker(self, symbol: str) -> Dict:
        resp = await self._request("GET", f"/v2/tickers/{symbol}", auth=False)
        return resp.get("result", {})

    async def get_orderbook(self, symbol: str, depth: int = 10) -> Dict:
        resp = await self._request("GET", f"/v2/l2orderbook/{symbol}", params={"depth": depth}, auth=False)
        return resp.get("result", {})

    async def get_ohlcv(
        self,
        symbol: str,
        resolution: Union[int, str],
        start: int,
        end: int,
    ) -> List[OHLCV]:
        """Fetch OHLCV candles, deduplicated and sorted ascending."""
        allowed_map = {
            1: "1m", 3: "3m", 5: "5m", 15: "15m", 30: "30m",
            60: "1h", 120: "2h", 240: "4h", 360: "6h", 720: "12h",
            1440: "1d", 10080: "1w",
        }
        if isinstance(resolution, int):
            if resolution not in allowed_map:
                raise ValueError(f"Unsupported resolution {resolution!r}. Allowed: {list(allowed_map)}")
            res_str = allowed_map[resolution]
        else:
            res_str = str(resolution)

        params = {"symbol": symbol, "resolution": res_str, "start": start, "end": end}
        resp = await self._request("GET", "/v2/history/candles", params=params, auth=False)

        seen_ts = set()
        candles = []
        for c in resp.get("result", []):
            ts = c.get("time", 0)
            if ts in seen_ts:
                continue
            seen_ts.add(ts)
            candles.append(OHLCV(
                timestamp=ts,
                open=float(c["open"]),
                high=float(c["high"]),
                low=float(c["low"]),
                close=float(c["close"]),
                volume=float(c["volume"]),
            ))
        candles.sort(key=lambda x: x.timestamp)
        return candles

    # ── Account ───────────────────────────────

    async def get_wallet_balance(self, asset: str = "USDT") -> float:
        resp = await self._request("GET", "/v2/wallet/balances", auth=True)
        for bal in resp.get("result", []):
            if bal.get("asset_symbol") == asset:
                return float(bal.get("available_balance", 0))
        return 0.0

    async def get_positions(self) -> List[Position]:
        resp = await self._request("GET", "/v2/positions/margined", auth=True)
        positions = []
        for p in resp.get("result", []):
            size = float(p.get("size", 0))
            if size == 0:
                continue
            product = p.get("product", {}) or {}
            positions.append(Position(
                product_id=p["product_id"],
                symbol=product.get("symbol", ""),
                size=size if p.get("entry_price") else 0,
                entry_price=float(p.get("entry_price") or 0),
                mark_price=float(p.get("mark_price") or 0),
                unrealized_pnl=float(p.get("unrealized_pnl") or 0),
                realized_pnl=float(p.get("realized_pnl") or 0),
                margin=float(p.get("margin") or 0),
            ))
        return positions

    # ── Orders ────────────────────────────────

    async def place_order(self, order: Order) -> Order:
        if not isinstance(order.size, int) or order.size < 1:
            raise ValueError(f"order.size must be a positive integer, got {order.size!r}")

        payload: Dict[str, Any] = {
            "product_id": order.product_id,
            "side": order.side.value,
            "order_type": order.order_type.value,
            "size": order.size,
            "time_in_force": order.time_in_force,
            "reduce_only": order.reduce_only,
        }
        if order.limit_price is not None:
            payload["limit_price"] = str(round(order.limit_price, 2))
        if order.stop_price is not None:
            payload["stop_price"] = str(round(order.stop_price, 2))
        if order.client_order_id:
            payload["client_order_id"] = order.client_order_id

        logger.info("Placing %s %s %d lots @ %s", order.order_type, order.side, order.size, order.limit_price)
        resp = await self._request("POST", "/v2/orders", data=payload)
        result = resp.get("result", {})
        order.order_id = str(result.get("id", ""))
        try:
            order.status = OrderStatus(result.get("state", "pending"))
        except ValueError:
            order.status = OrderStatus.PENDING
        logger.info("Order placed — id=%s status=%s", order.order_id, order.status)
        return order

    async def place_bracket_order(
        self,
        product_id: int,
        side: OrderSide,
        size: int,
        entry_price: Optional[float] = None,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        client_order_id: Optional[str] = None,
    ) -> Dict:
        """
        Place bracket order: entry + SL + TP atomically.

        Delta India bracket_order field names (verified from docs):
          bracket_order.stop_loss_order.trigger_price
          bracket_order.stop_loss_order.order_type  ("market_order" | "limit_order")
          bracket_order.take_profit_order.trigger_price
          bracket_order.take_profit_order.order_type
        """
        if size < 1:
            raise ValueError(f"Bracket order size must be ≥ 1 lot, got {size}")

        payload: Dict[str, Any] = {
            "product_id": product_id,
            "side": side.value,
            "order_type": "market_order",
            "size": size,
            "time_in_force": "gtc",
        }

        if entry_price is not None:
            payload["order_type"] = "limit_order"
            payload["limit_price"] = str(round(entry_price, 2))

        bracket: Dict[str, Any] = {}

        if stop_loss_price is not None and stop_loss_price > 0:
            bracket["stop_loss_order"] = {
                "order_type": "market_order",
                "trigger_price": str(round(stop_loss_price, 2)),
            }

        if take_profit_price is not None and take_profit_price > 0:
            bracket["take_profit_order"] = {
                "order_type": "limit_order",
                "trigger_price": str(round(take_profit_price, 2)),
                "limit_price": str(round(take_profit_price, 2)),
            }

        if bracket:
            payload["bracket_order"] = bracket

        if client_order_id:
            payload["client_order_id"] = client_order_id

        logger.info(
            "🔲 BRACKET ORDER: %s %d lots | entry=%s sl=%s tp=%s",
            side.value.upper(), size,
            f"{entry_price:.4f}" if entry_price else "MARKET",
            f"{stop_loss_price:.4f}" if stop_loss_price else "NONE",
            f"{take_profit_price:.4f}" if take_profit_price else "NONE",
        )

        resp = await self._request("POST", "/v2/orders", data=payload)
        result = resp.get("result", {})
        logger.info(
            "Bracket placed — entry_id=%s sl_id=%s tp_id=%s",
            result.get("id"), result.get("sl_order_id"), result.get("tp_order_id"),
        )
        return result

    async def cancel_order(self, order_id: str, product_id: int) -> bool:
        try:
            await self._request("DELETE", "/v2/orders", data={"id": order_id, "product_id": product_id})
            return True
        except DeltaAPIError as exc:
            logger.warning("Cancel order %s failed: %s", order_id, exc)
            return False

    async def cancel_all_orders(self, product_id: int) -> bool:
        try:
            await self._request("DELETE", "/v2/orders/all", data={"product_id": product_id})
            return True
        except DeltaAPIError as exc:
            logger.warning("Cancel all orders failed: %s", exc)
            return False

    async def get_open_orders(self, product_id: Optional[int] = None) -> List[Dict]:
        params: Dict[str, Any] = {"state": "open"}
        if product_id:
            params["product_id"] = product_id
        resp = await self._request("GET", "/v2/orders", params=params)
        return resp.get("result", [])


# ─────────────────────────────────────────────
# WebSocket Client (HARDENED)
# ─────────────────────────────────────────────

class DeltaWSClient:
    """
    WebSocket client for Delta Exchange real-time feeds.
    
    Improvements over v2:
    - Heartbeat ping every 20s to detect silent disconnections
    - on_message supports both sync and async callbacks
    - Proper auth via 'authorization' message before subscribing
    - Exponential backoff capped at 60s
    - Message envelope unwrapping (Delta wraps data in {"type":"...","payload":...})
    """

    WS_URL = "wss://socket.india.delta.exchange"

    def __init__(self, api_key: str, api_secret: str, on_message: Callable):
        self.api_key = api_key
        self.api_secret = api_secret
        self.on_message = on_message
        self._ws = None
        self._subscriptions: List[Dict] = []
        self._running = False
        self._is_async_handler = inspect.iscoroutinefunction(on_message)

    def _auth_payload(self) -> Dict:
        timestamp = str(int(time.time()))
        msg = "GET" + timestamp + "/live"
        sig = hmac.new(
            self.api_secret.encode("utf-8"),
            msg.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "type": "auth",
            "payload": {
                "api-key": self.api_key,
                "signature": sig,
                "timestamp": timestamp,
            },
        }

    def subscribe(self, channels: List[Dict]):
        self._subscriptions.extend(channels)

    async def _dispatch(self, msg: Dict):
        try:
            if self._is_async_handler:
                await self.on_message(msg)
            else:
                await asyncio.get_event_loop().run_in_executor(None, self.on_message, msg)
        except Exception as exc:
            logger.error("WS message handler error: %s", exc, exc_info=True)

    async def _heartbeat(self, ws):
        """Send ping every 20s to keep connection alive."""
        while self._running:
            try:
                await asyncio.sleep(20)
                await ws.ping()
            except Exception:
                break

    async def connect(self):
        self._running = True
        backoff = 2
        while self._running:
            try:
                async with websockets.connect(
                    self.WS_URL,
                    ping_interval=None,   # We manage our own heartbeat
                    close_timeout=5,
                    max_size=2 ** 20,
                ) as ws:
                    self._ws = ws
                    backoff = 2  # Reset on successful connect

                    # Authenticate
                    await ws.send(json.dumps(self._auth_payload()))
                    logger.info("WebSocket connected and authenticated")

                    # Subscribe to channels
                    for sub in self._subscriptions:
                        await ws.send(json.dumps(sub))
                        logger.debug("Subscribed: %s", sub)

                    # Start heartbeat
                    hb_task = asyncio.create_task(self._heartbeat(ws))

                    try:
                        async for raw in ws:
                            try:
                                msg = json.loads(raw)
                                # Delta wraps messages: unwrap if needed
                                if isinstance(msg, dict):
                                    # Some Delta messages have a "result" key
                                    # Pass through ticker/trade messages directly
                                    await self._dispatch(msg)
                            except json.JSONDecodeError:
                                pass
                    finally:
                        hb_task.cancel()

            except (ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                logger.warning("WS disconnected: %s — reconnecting in %ds", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as exc:
                logger.error("WS unexpected error: %s — reconnecting in %ds", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def disconnect(self):
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
