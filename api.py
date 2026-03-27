"""
api.py — Delta Exchange REST + WebSocket client
Handles authentication, order placement, market data, and error recovery.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

try:
    import aiohttp
    import websockets
except ImportError:
    aiohttp = None
    websockets = None

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


@dataclass
class Order:
    product_id: int
    side: OrderSide
    order_type: OrderType
    size: float
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    reduce_only: bool = False
    time_in_force: str = "gtc"          # good-till-cancelled
    client_order_id: Optional[str] = None
    # Populated after placement
    order_id: Optional[str] = None
    status: Optional[OrderStatus] = None
    filled_size: float = 0.0
    avg_fill_price: Optional[float] = None


@dataclass
class Position:
    product_id: int
    symbol: str
    size: float                          # positive = long, negative = short
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    realized_pnl: float
    margin: float


@dataclass
class OHLCV:
    timestamp: int                       # unix ms
    open: float
    high: float
    low: float
    close: float
    volume: float


# ─────────────────────────────────────────────
# Delta Exchange REST Client
# ─────────────────────────────────────────────

class DeltaRESTClient:
    """
    Async REST client for Delta Exchange v2 API.
    Docs: https://docs.delta.exchange/
    """

    BASE_URL = "https://api.delta.exchange"   # production
    # BASE_URL = "https://testnet-api.delta.exchange"  # testnet

    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self._session: Optional[aiohttp.ClientSession] = None

    # ── Session management ────────────────────

    async def __aenter__(self):
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *args):
        if self._session:
            await self._session.close()

    async def _ensure_session(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()

    # ── Auth ──────────────────────────────────

    def _sign(self, method: str, path: str, query: str, body: str, timestamp: str) -> str:
        """
        HMAC-SHA256 signature per Delta Exchange docs.
        Signature = HMAC_SHA256(secret, method + timestamp + path + query + body)
        """
        msg = method + timestamp + path + ("?" + query if query else "") + body
        return hmac.new(
            self.api_secret.encode(), msg.encode(), hashlib.sha256
        ).hexdigest()

    def _auth_headers(
        self,
        method: str,
        path: str,
        query: str = "",
        body: str = "",
    ) -> Dict[str, str]:
        timestamp = str(int(time.time()))
        signature = self._sign(method, path, query, body, timestamp)
        return {
            "api-key": self.api_key,
            "signature": signature,
            "timestamp": timestamp,
            "Content-Type": "application/json",
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
        url = self.BASE_URL + path
        query = "&".join(f"{k}={v}" for k, v in (params or {}).items())
        body = json.dumps(data) if data else ""

        headers = self._auth_headers(method.upper(), path, query, body) if auth else {}

        for attempt in range(retries):
            try:
                async with self._session.request(
                    method, url, params=params, data=body or None, headers=headers
                ) as resp:
                    resp_data = await resp.json()
                    if resp.status == 429:
                        # Rate-limited — back off
                        wait = 2 ** attempt
                        logger.warning("Rate limited, backing off %ds", wait)
                        await asyncio.sleep(wait)
                        continue
                    if resp.status >= 400:
                        raise DeltaAPIError(resp.status, resp_data)
                    return resp_data
            except aiohttp.ClientError as exc:
                logger.error("HTTP error (attempt %d/%d): %s", attempt + 1, retries, exc)
                if attempt == retries - 1:
                    raise
                await asyncio.sleep(1)

    # ── Market Data ───────────────────────────

    async def get_products(self) -> List[Dict]:
        """Return all available products/contracts."""
        resp = await self._request("GET", "/v2/products", auth=False)
        return resp.get("result", [])

    async def get_ticker(self, symbol: str) -> Dict:
        """24-hour ticker for a symbol, e.g. 'BTCUSD'."""
        resp = await self._request("GET", f"/v2/tickers/{symbol}", auth=False)
        return resp.get("result", {})

    async def get_orderbook(self, symbol: str, depth: int = 10) -> Dict:
        resp = await self._request(
            "GET", "/v2/l2orderbook/{symbol}", params={"depth": depth}, auth=False
        )
        return resp.get("result", {})

    async def get_ohlcv(
        self,
        symbol: str,
        resolution: int,          # minutes: 1, 5, 15, 60, 240, 1440
        start: int,               # unix timestamp
        end: int,
    ) -> List[OHLCV]:
        """Fetch OHLCV candles from the history endpoint."""
        params = {
            "symbol": symbol,
            "resolution": resolution,
            "start": start,
            "end": end,
        }
        resp = await self._request("GET", "/v2/history/candles", params=params, auth=False)
        candles = []
        for c in resp.get("result", []):
            candles.append(OHLCV(
                timestamp=c["time"],
                open=float(c["open"]),
                high=float(c["high"]),
                low=float(c["low"]),
                close=float(c["close"]),
                volume=float(c["volume"]),
            ))
        return candles

    # ── Account ───────────────────────────────

    async def get_wallet_balance(self, asset: str = "USDT") -> float:
        """Return available balance for an asset."""
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
            positions.append(Position(
                product_id=p["product_id"],
                symbol=p["product"]["symbol"],
                size=size if p["entry_price"] else 0,
                entry_price=float(p.get("entry_price") or 0),
                mark_price=float(p.get("mark_price") or 0),
                unrealized_pnl=float(p.get("unrealized_pnl") or 0),
                realized_pnl=float(p.get("realized_pnl") or 0),
                margin=float(p.get("margin") or 0),
            ))
        return positions

    # ── Orders ────────────────────────────────

    async def place_order(self, order: Order) -> Order:
        """Place an order and populate order.order_id / status."""
        payload: Dict[str, Any] = {
            "product_id": order.product_id,
            "side": order.side.value,
            "order_type": order.order_type.value,
            "size": str(order.size),
            "time_in_force": order.time_in_force,
            "reduce_only": order.reduce_only,
        }
        if order.limit_price is not None:
            payload["limit_price"] = str(order.limit_price)
        if order.stop_price is not None:
            payload["stop_price"] = str(order.stop_price)
        if order.client_order_id:
            payload["client_order_id"] = order.client_order_id

        logger.info("Placing %s %s %s @ %s", order.order_type, order.side, order.size, order.limit_price)
        resp = await self._request("POST", "/v2/orders", data=payload)
        result = resp.get("result", {})
        order.order_id = result.get("id")
        order.status = OrderStatus(result.get("state", "pending"))
        logger.info("Order placed — id=%s status=%s", order.order_id, order.status)
        return order

    async def cancel_order(self, order_id: str, product_id: int) -> bool:
        payload = {"id": order_id, "product_id": product_id}
        try:
            await self._request("DELETE", "/v2/orders", data=payload)
            logger.info("Cancelled order %s", order_id)
            return True
        except DeltaAPIError as exc:
            logger.error("Cancel failed: %s", exc)
            return False

    async def get_order(self, order_id: str) -> Dict:
        resp = await self._request("GET", f"/v2/orders/{order_id}")
        return resp.get("result", {})

    async def get_open_orders(self, product_id: Optional[int] = None) -> List[Dict]:
        params = {"state": "open"}
        if product_id:
            params["product_id"] = product_id
        resp = await self._request("GET", "/v2/orders", params=params)
        return resp.get("result", [])

    async def cancel_all_orders(self, product_id: int) -> bool:
        payload = {"product_id": product_id}
        try:
            await self._request("DELETE", "/v2/orders/all", data=payload)
            return True
        except DeltaAPIError:
            return False


# ─────────────────────────────────────────────
# WebSocket Client
# ─────────────────────────────────────────────

class DeltaWSClient:
    """
    WebSocket client for Delta Exchange real-time feeds.
    Channels: ticker, orderbook, trades, candlestick, positions, orders
    """

    WS_URL = "wss://socket.delta.exchange"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        on_message: Callable[[Dict], None],
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.on_message = on_message
        self._ws = None
        self._subscriptions: List[Dict] = []
        self._running = False

    def _auth_payload(self) -> Dict:
        timestamp = str(int(time.time()))
        msg = "GET" + timestamp + "/live"
        sig = hmac.new(
            self.api_secret.encode(), msg.encode(), hashlib.sha256
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
        """
        Queue subscriptions. E.g.:
          [{"type": "subscribe", "payload": {"channels": [{"name": "candlestick_1m", "symbols": ["BTCUSD"]}]}}]
        """
        self._subscriptions.extend(channels)

    async def connect(self):
        """Connect, authenticate, subscribe, and dispatch messages."""
        self._running = True
        while self._running:
            try:
                async with websockets.connect(self.WS_URL) as ws:
                    self._ws = ws
                    logger.info("WebSocket connected")

                    # Authenticate
                    await ws.send(json.dumps(self._auth_payload()))

                    # Subscribe to channels
                    for sub in self._subscriptions:
                        await ws.send(json.dumps(sub))

                    async for raw in ws:
                        msg = json.loads(raw)
                        try:
                            await asyncio.get_event_loop().run_in_executor(
                                None, self.on_message, msg
                            )
                        except Exception as exc:
                            logger.error("Message handler error: %s", exc)

            except (websockets.ConnectionClosed, OSError) as exc:
                logger.warning("WebSocket disconnected: %s — reconnecting in 5s", exc)
                await asyncio.sleep(5)

    async def disconnect(self):
        self._running = False
        if self._ws:
            await self._ws.close()


# ─────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────

class DeltaAPIError(Exception):
    def __init__(self, status: int, body: Dict):
        self.status = status
        self.body = body
        msg = body.get("error", {}).get("message", str(body))
        super().__init__(f"Delta API {status}: {msg}")
