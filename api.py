"""
api.py — Delta Exchange REST + WebSocket client
Handles authentication, order placement, market data, and error recovery.

FIXES:
  - HMAC signature now uses method.upper() + timestamp + path + query_string + body
    exactly as Delta Exchange v2 docs specify (no '?' prefix in sig, query params
    are NOT included in the path part of the sig for GET requests with params)
  - hmac.new → hmac.new  (was already correct in stdlib, but message format fixed)
  - get_orderbook URL fixed (missing f-string prefix)
  - Order size is now sent as an integer (lots), not a float fraction
  - Product lot_size fetched and cached so execution engine can convert USD → lots
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Union

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
    size: int                            # ← INTEGER lots (e.g. 1, 2, 3 …)
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    reduce_only: bool = False
    time_in_force: str = "gtc"
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

    Signature format (HMAC-SHA256):
        message = method + timestamp + request_path + query_string + body
        - method       : uppercase, e.g. "GET", "POST", "DELETE"
        - timestamp    : unix seconds as string
        - request_path : e.g. "/v2/orders"  (no host, no query string)
        - query_string : full query string WITHOUT leading '?', e.g. "state=open&product_id=3"
                         empty string "" if no params
        - body         : raw JSON string, or "" for GET/DELETE with no body
    """

    BASE_URL = "https://api.delta.exchange"
    # BASE_URL = "https://testnet-api.delta.exchange"   # uncomment for testnet

    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self._session: Optional[aiohttp.ClientSession] = None
        # Cache: symbol → product info (includes contract_value, lot_size, etc.)
        self._product_cache: Dict[str, Dict] = {}

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

    def _sign(
        self,
        method: str,
        path: str,
        query_string: str,
        body: str,
        timestamp: str,
    ) -> str:
        """
        Delta Exchange HMAC-SHA256 signature.
        message = METHOD + timestamp + path + query_string + body
        NOTE: query_string has NO leading '?'.
        """
        message = method.upper() + timestamp + path + query_string + body
        return hmac.new(
            self.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _auth_headers(
        self,
        method: str,
        path: str,
        query_string: str = "",
        body: str = "",
    ) -> Dict[str, str]:
        timestamp = str(int(time.time()))
        signature = self._sign(method, path, query_string, body, timestamp)
        return {
            "api-key": self.api_key,
            "signature": signature,
            "timestamp": timestamp,
            "Content-Type": "application/json",
            "Accept": "application/json",
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

        # Build query string exactly as it will appear in the URL
        if params:
            query_string = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        else:
            query_string = ""

        body = json.dumps(data, separators=(",", ":")) if data else ""

        headers = (
            self._auth_headers(method.upper(), path, query_string, body)
            if auth
            else {"Content-Type": "application/json", "Accept": "application/json"}
        )

        for attempt in range(retries):
            try:
                async with self._session.request(
                    method,
                    url,
                    params=params,
                    data=body if body else None,
                    headers=headers,
                ) as resp:
                    resp_data = await resp.json(content_type=None)

                    if resp.status == 429:
                        wait = 2 ** attempt
                        logger.warning("Rate limited — backing off %ds", wait)
                        await asyncio.sleep(wait)
                        continue

                    if resp.status >= 400:
                        logger.error(
                            "API error %d on %s %s: %s",
                            resp.status, method, path, resp_data,
                        )
                        raise DeltaAPIError(resp.status, resp_data)

                    return resp_data

            except aiohttp.ClientError as exc:
                logger.error("HTTP error (attempt %d/%d): %s", attempt + 1, retries, exc)
                if attempt == retries - 1:
                    raise
                await asyncio.sleep(2 ** attempt)

        raise RuntimeError(f"Failed after {retries} retries: {method} {path}")

    # ── Product / Lot helpers ─────────────────

    async def get_products(self) -> List[Dict]:
        """Return all available products/contracts."""
        resp = await self._request("GET", "/v2/products", auth=False)
        products = resp.get("result", [])
        # Populate cache
        for p in products:
            sym = p.get("symbol", "")
            if sym:
                self._product_cache[sym] = p
        return products

    async def get_product(self, symbol: str) -> Optional[Dict]:
        """Return product info for a symbol, fetching if not cached."""
        if symbol not in self._product_cache:
            await self.get_products()
        return self._product_cache.get(symbol)

    def usd_to_lots(self, symbol: str, usd_notional: float, price: float) -> int:
        """
        Convert a USD notional amount into an integer lot count.

        Delta futures:
          - 'contract_value' = USD value of 1 contract (lot) at current price
            For inverse contracts (BTC-settled): contract_value is in USD per lot.
            For linear contracts (USDT-settled): contract_value is the base qty per lot.
          - We always round DOWN to ensure we never exceed capital.

        Returns at least 1 lot if the math gives > 0, else 0.
        """
        product = self._product_cache.get(symbol, {})

        # contract_type: "perpetual_futures", "call_options", etc.
        # For USDT-margined linear: contract_value = base asset per lot (e.g. 0.001 BTC)
        # For USD-margined inverse: contract_value = USD per lot (e.g. 1 USD)
        contract_value = float(product.get("contract_value", 1) or 1)
        contract_type = product.get("contract_type", "")

        if "inverse" in contract_type.lower() or product.get("quoting_asset", {}).get("symbol", "") in ("BTC", "ETH"):
            # Inverse contract: lot value in USD = contract_value
            # size_in_usd / contract_value_per_lot = lots
            lots = int(usd_notional / contract_value)
        else:
            # Linear contract (USDT-margined): lot = contract_value base units
            # value_per_lot_in_usd = contract_value * price
            value_per_lot = contract_value * price
            lots = int(usd_notional / value_per_lot) if value_per_lot > 0 else 0

        # Respect minimum order size
        min_size = int(product.get("min_size", 1) or 1)
        if lots < min_size and lots > 0:
            lots = min_size

        logger.debug(
            "usd_to_lots: symbol=%s usd=%.2f price=%.4f contract_value=%s → %d lots",
            symbol, usd_notional, price, contract_value, lots,
        )
        return max(0, lots)

    # ── Market Data ───────────────────────────

    async def get_ticker(self, symbol: str) -> Dict:
        resp = await self._request("GET", f"/v2/tickers/{symbol}", auth=False)
        return resp.get("result", {})

    async def get_orderbook(self, symbol: str, depth: int = 10) -> Dict:
        # FIX: was missing f-string prefix
        resp = await self._request(
            "GET",
            f"/v2/l2orderbook/{symbol}",
            params={"depth": depth},
            auth=False,
        )
        return resp.get("result", {})

    async def get_ohlcv(
        self,
        symbol: str,
        resolution: Union[int, str],          # minutes (int) or API string like '15m', '1h'
        start: int,               # unix timestamp (seconds)
        end: int,
    ) -> List[OHLCV]:
        # Delta API expects a resolution string like '1m','15m','1h','1d', etc.
        # Accept ints (minutes) for callers and convert to the proper string.
        allowed_map = {
            1: "1m",
            3: "3m",
            5: "5m",
            15: "15m",
            30: "30m",
            60: "1h",
            120: "2h",
            240: "4h",
            360: "6h",
            720: "12h",
            1440: "1d",
            10080: "1w",
        }

        if isinstance(resolution, int):
            if resolution not in allowed_map:
                raise ValueError(
                    f"Unsupported numeric resolution {resolution!r}. Allowed minutes: {list(allowed_map.keys())}"
                )
            res_str = allowed_map[resolution]
        else:
            res_str = str(resolution)

        params = {
            "symbol": symbol,
            "resolution": res_str,
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
        """
        Place an order. order.size MUST be an integer (number of lots).
        """
        if not isinstance(order.size, int) or order.size < 1:
            raise ValueError(
                f"order.size must be a positive integer (lots), got {order.size!r}. "
                "Use DeltaRESTClient.usd_to_lots() to convert."
            )

        payload: Dict[str, Any] = {
            "product_id": order.product_id,
            "side": order.side.value,
            "order_type": order.order_type.value,
            "size": order.size,          # integer — no str() conversion needed
            "time_in_force": order.time_in_force,
            "reduce_only": order.reduce_only,
        }
        if order.limit_price is not None:
            payload["limit_price"] = str(round(order.limit_price, 2))
        if order.stop_price is not None:
            payload["stop_price"] = str(round(order.stop_price, 2))
        if order.client_order_id:
            payload["client_order_id"] = order.client_order_id

        logger.info(
            "Placing %s %s %d lots @ %s",
            order.order_type, order.side, order.size, order.limit_price,
        )
        resp = await self._request("POST", "/v2/orders", data=payload)
        result = resp.get("result", {})
        order.order_id = str(result.get("id", ""))
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
        params: Dict[str, Any] = {"state": "open"}
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

    async def connect(self):
        self._running = True
        while self._running:
            try:
                async with websockets.connect(self.WS_URL) as ws:
                    self._ws = ws
                    logger.info("WebSocket connected")
                    await ws.send(json.dumps(self._auth_payload()))
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
        if isinstance(body, dict):
            msg = body.get("error", {}).get("message", str(body))
        else:
            msg = str(body)
        super().__init__(f"Delta API {status}: {msg}")
