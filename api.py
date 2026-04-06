"""
api.py — Delta Exchange REST + WebSocket client (PRODUCTION v6)

Based on official Delta Exchange API docs (api.india.delta.exchange):
  - Full HMAC-SHA256 auth per spec (method+timestamp+path+querystring+body)
  - All order types: market, limit, stop-market, stop-limit, bracket
  - Bracket orders: POST /v2/orders with bracket_stop_loss_price + bracket_take_profit_price
  - Position bracket: POST /v2/orders/bracket (separate endpoint for open positions)
  - Edit bracket: PUT /v2/orders/bracket
  - Get order by ID: GET /v2/orders/{id}
  - Batch orders: POST/PUT/DELETE /v2/orders/batch
  - Fills, positions, wallet, leverage, heartbeat
  - WebSocket: v2/ticker, l2_orderbook, candlesticks, all_trades, mark_price
  - Private WS channels: orders, positions, fills, liquidations
  - Rate limiter: 25 req/s (quota 10,000 / 5-min window, weight-aware)
  - Exponential backoff, re-sign on every retry
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

# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────

class OrderType(str, Enum):
    MARKET      = "market_order"
    LIMIT       = "limit_order"
    STOP_MARKET = "stop_market_order"
    STOP_LIMIT  = "stop_limit_order"

class OrderSide(str, Enum):
    BUY  = "buy"
    SELL = "sell"

class OrderStatus(str, Enum):
    OPEN      = "open"
    PENDING   = "pending"
    CLOSED    = "closed"
    CANCELLED = "cancelled"
    FILLED    = "filled"

class TimeInForce(str, Enum):
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"

class StopTriggerMethod(str, Enum):
    LAST_TRADED_PRICE = "last_traded_price"
    MARK_PRICE        = "mark_price"
    INDEX_PRICE       = "index_price"

# ─────────────────────────────────────────────────────────────────────────────
# Data Classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Order:
    product_id:      int
    side:            OrderSide
    order_type:      OrderType
    size:            int
    limit_price:     Optional[float] = None
    stop_price:      Optional[float] = None
    reduce_only:     bool = False
    time_in_force:   TimeInForce = TimeInForce.GTC
    client_order_id: Optional[str] = None
    order_id:        Optional[str] = None
    status:          Optional[OrderStatus] = None
    filled_size:     float = 0.0
    avg_fill_price:  Optional[float] = None
    created_at:      Optional[str] = None

@dataclass
class BracketOrderResult:
    entry_order_id: str
    sl_order_id:    Optional[str]
    tp_order_id:    Optional[str]
    entry_side:     str
    size:           int
    raw:            Dict = field(default_factory=dict)

@dataclass
class Position:
    product_id:     int
    symbol:         str
    size:           float
    entry_price:    float
    mark_price:     float
    unrealized_pnl: float
    realized_pnl:   float
    margin:         float
    side:           str = ""   # "long" | "short"
    liquidation_price: Optional[float] = None

@dataclass
class OHLCV:
    timestamp: int
    open:      float
    high:      float
    low:       float
    close:     float
    volume:    float

@dataclass
class L2OrderBook:
    symbol: str
    buy:    List[Dict]   # [{price, size}, ...]
    sell:   List[Dict]
    ts:     int = 0

    def best_bid(self) -> Optional[float]:
        return float(self.buy[0]["limit_price"]) if self.buy else None

    def best_ask(self) -> Optional[float]:
        return float(self.sell[0]["limit_price"]) if self.sell else None

    def spread(self) -> Optional[float]:
        b, a = self.best_bid(), self.best_ask()
        return (a - b) if b and a else None

    def imbalance(self, levels: int = 5) -> float:
        """Bid/ask imbalance: >0 = more bids (bullish), <0 = more asks."""
        bid_vol = sum(float(x["size"]) for x in self.buy[:levels])
        ask_vol = sum(float(x["size"]) for x in self.sell[:levels])
        total   = bid_vol + ask_vol
        return (bid_vol - ask_vol) / total if total > 0 else 0.0

@dataclass
class Ticker:
    symbol:      str
    last_price:  float
    mark_price:  float
    index_price: float
    bid:         float
    ask:         float
    volume:      float
    open_interest: float
    funding_rate:  float
    oi_change_usd: float = 0.0

@dataclass
class Fill:
    id:          str
    order_id:    str
    product_id:  int
    price:       float
    size:        float
    side:        str
    fee:         float
    timestamp:   str

# ─────────────────────────────────────────────────────────────────────────────
# Rate Limiter (sliding window)
# ─────────────────────────────────────────────────────────────────────────────

class RateLimiter:
    """
    Sliding-window rate limiter.
    Default: 25 req/s (well under 500 op/s product limit).
    Quota: 10,000 per 5-min window — heavier ops use more weight.
    """
    def __init__(self, max_calls: int = 25, window_seconds: float = 1.0):
        self._max    = max_calls
        self._window = window_seconds
        self._times: deque = deque()

    async def acquire(self):
        now = time.monotonic()
        while self._times and now - self._times[0] > self._window:
            self._times.popleft()
        if len(self._times) >= self._max:
            sleep_for = self._window - (now - self._times[0]) + 0.01
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
        self._times.append(time.monotonic())

# ─────────────────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────────────────

class DeltaAPIError(Exception):
    def __init__(self, status: int, body: Any):
        self.status = status
        self.body   = body
        if isinstance(body, dict):
            err = body.get("error", {})
            msg = err.get("message", str(body)) if isinstance(err, dict) else str(err)
        else:
            msg = str(body)
        super().__init__(f"Delta API {status}: {msg}")

    @property
    def error_code(self) -> str:
        if isinstance(self.body, dict):
            err = self.body.get("error", {})
            if isinstance(err, dict):
                return err.get("code", "")
        return ""

    def is_rate_limit(self) -> bool:
        return self.status == 429

    def is_auth_error(self) -> bool:
        return self.status in (401, 403)

# ─────────────────────────────────────────────────────────────────────────────
# Delta Exchange REST Client
# ─────────────────────────────────────────────────────────────────────────────

class DeltaRESTClient:
    """
    Full Delta Exchange India REST client.

    Auth scheme (per official docs):
        message   = METHOD.upper() + timestamp + path + query_string + body
        query_string = sorted k=v pairs joined by & (NO leading '?')
        timestamp = str(int(time.time()))  — must be within 5 seconds of server

    Base URL: https://api.india.delta.exchange
    Testnet:  https://cdn-ind.testnet.deltaex.org
    """

    BASE_URL     = "https://api.india.delta.exchange"
    # BASE_URL   = "https://cdn-ind.testnet.deltaex.org"  # ← testnet

    FALLBACK_LOT_SIZES: Dict[str, float] = {
        "BTC_USDT":   0.001,
        "ETH_USDT":   0.01,
        "SOL_USDT":   1.0,
        "XRP_USDT":   10.0,
        "BNB_USDT":   0.1,
        "DOGE_USDT":  100.0,
        "MATIC_USDT": 10.0,
        "AVAX_USDT":  0.1,
        "LTC_USDT":   0.1,
        "BTCUSD":     1.0,    # inverse contract (USD per lot)
        "ETHUSD":     1.0,
    }

    def __init__(self, api_key: str, api_secret: str):
        self.api_key          = api_key
        self.api_secret       = api_secret
        self._session: Optional[aiohttp.ClientSession] = None
        self._product_cache: Dict[str, Dict] = {}
        self._rate_limiter    = RateLimiter(max_calls=25, window_seconds=1.0)

    async def __aenter__(self):
        timeout = aiohttp.ClientTimeout(total=20, connect=8)
        self._session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *args):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _ensure_session(self):
        if not self._session or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=20, connect=8)
            self._session = aiohttp.ClientSession(timeout=timeout)

    # ── Authentication ─────────────────────────────────────────────────────

    def _sign(self, method: str, path: str, query_string: str, body: str) -> tuple:
        """Return (timestamp, signature) per Delta HMAC spec."""
        timestamp = str(int(time.time()))
        message   = method.upper() + timestamp + path + query_string + body
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return timestamp, signature

    def _auth_headers(
        self, method: str, path: str, query_string: str = "", body: str = ""
    ) -> Dict[str, str]:
        timestamp, signature = self._sign(method, path, query_string, body)
        return {
            "api-key":      self.api_key,
            "signature":    signature,
            "timestamp":    timestamp,
            "Content-Type": "application/json",
            "Accept":       "application/json",
            "User-Agent":   "DeltaAlgoBot/6.0",
        }

    # ── Core HTTP ──────────────────────────────────────────────────────────

    async def _request(
        self,
        method:  str,
        path:    str,
        params:  Optional[Dict] = None,
        data:    Optional[Dict] = None,
        auth:    bool = True,
        retries: int  = 3,
    ) -> Dict:
        await self._ensure_session()
        await self._rate_limiter.acquire()

        url          = self.BASE_URL + path
        # Sort params alphabetically for deterministic query_string in signature
        query_string = "&".join(f"{k}={v}" for k, v in sorted(params.items())) if params else ""
        body         = json.dumps(data, separators=(",", ":")) if data is not None else ""

        last_exc = None
        for attempt in range(retries):
            headers = (
                self._auth_headers(method.upper(), path, query_string, body)
                if auth else {
                    "Content-Type": "application/json",
                    "Accept":       "application/json",
                    "User-Agent":   "DeltaAlgoBot/6.0",
                }
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
                        reset_ms  = resp.headers.get("X-RATE-LIMIT-RESET", "5000")
                        wait_secs = min(int(reset_ms) / 1000 + 0.1, 60)
                        logger.warning("Rate-limited — sleeping %.1fs", wait_secs)
                        await asyncio.sleep(wait_secs)
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

    # ── Products ───────────────────────────────────────────────────────────

    async def get_products(self, page_size: int = 100) -> List[Dict]:
        """GET /v2/products — paginated product list."""
        products = []
        params   = {"page_size": page_size}
        while True:
            resp     = await self._request("GET", "/v2/products", params=params, auth=False)
            result   = resp.get("result", [])
            products.extend(result)
            meta = resp.get("meta", {})
            after = meta.get("after")
            if not after or len(result) < page_size:
                break
            params["after"] = after

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
        Convert USD notional to integer lots.
        Handles linear (USDT) and inverse (USD) contract math.
        Always returns ≥ min_size if notional > 0.
        """
        if price <= 0:
            return 1

        product        = self._product_cache.get(symbol, {})
        cv_raw         = product.get("contract_value")
        contract_value = float(cv_raw) if cv_raw is not None else 0.0

        if contract_value <= 0:
            contract_value = float(self.FALLBACK_LOT_SIZES.get(symbol, 0.001))

        contract_type = product.get("contract_type", "")
        quoting_asset = product.get("quoting_asset", {})
        quoting_sym   = quoting_asset.get("symbol", "") if isinstance(quoting_asset, dict) else ""

        if "inverse" in contract_type.lower() or quoting_sym in ("BTC", "ETH"):
            # Inverse: lot value = contract_value USD (not affected by spot price)
            lots = int(usd_notional / contract_value) if contract_value > 0 else 0
        else:
            # Linear: lot value = contract_value × price USDT
            value_per_lot = contract_value * price
            lots = int(usd_notional / value_per_lot) if value_per_lot > 0 else 0

        try:
            min_size = max(1, int(product.get("min_size", 1) or 1))
        except (TypeError, ValueError):
            min_size = 1

        if 0 < lots < min_size:
            lots = min_size
        if lots == 0 and usd_notional > 0:
            lots = min_size
            logger.info("usd_to_lots: floored to %d lot (%s usd=%.2f)", min_size, symbol, usd_notional)

        return max(0, lots)

    # ── Market Data ────────────────────────────────────────────────────────

    async def get_ticker(self, symbol: str) -> Ticker:
        """GET /v2/tickers/{symbol} — real-time ticker snapshot."""
        resp = await self._request("GET", f"/v2/tickers/{symbol}", auth=False)
        r    = resp.get("result", {})
        return Ticker(
            symbol       = r.get("symbol", symbol),
            last_price   = float(r.get("close", 0) or 0),
            mark_price   = float(r.get("mark_price", 0) or 0),
            index_price  = float(r.get("spot_price", 0) or 0),
            bid          = float(r.get("quotes", {}).get("best_bid", 0) or 0),
            ask          = float(r.get("quotes", {}).get("best_ask", 0) or 0),
            volume       = float(r.get("volume", 0) or 0),
            open_interest= float(r.get("oi_value_usd", 0) or 0),
            funding_rate = float(r.get("funding_rate", 0) or 0),
        )

    async def get_orderbook(self, symbol: str, depth: int = 20) -> L2OrderBook:
        """GET /v2/l2orderbook/{symbol} — L2 order book."""
        resp = await self._request(
            "GET", f"/v2/l2orderbook/{symbol}",
            params={"depth": depth}, auth=False
        )
        r = resp.get("result", {})
        return L2OrderBook(
            symbol = symbol,
            buy    = r.get("buy", []),
            sell   = r.get("sell", []),
        )

    async def get_public_trades(self, symbol: str, limit: int = 100) -> List[Dict]:
        """GET /v2/trades/{symbol} — recent public trades."""
        resp = await self._request(
            "GET", f"/v2/trades/{symbol}",
            params={"page_size": limit}, auth=False
        )
        return resp.get("result", [])

    async def get_ohlcv(
        self,
        symbol:     str,
        resolution: Union[int, str],
        start:      int,
        end:        int,
    ) -> List[OHLCV]:
        """GET /v2/history/candles — OHLCV, deduped and sorted ascending."""
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

        params  = {"symbol": symbol, "resolution": res_str, "start": start, "end": end}
        resp    = await self._request("GET", "/v2/history/candles", params=params, auth=False)

        seen_ts = set()
        candles = []
        for c in resp.get("result", []):
            ts = c.get("time", 0)
            if ts in seen_ts:
                continue
            seen_ts.add(ts)
            candles.append(OHLCV(
                timestamp = ts,
                open  = float(c["open"]),
                high  = float(c["high"]),
                low   = float(c["low"]),
                close = float(c["close"]),
                volume= float(c["volume"]),
            ))
        candles.sort(key=lambda x: x.timestamp)
        return candles

    async def get_indices(self) -> List[Dict]:
        """GET /v2/indices — all index prices."""
        resp = await self._request("GET", "/v2/indices", auth=False)
        return resp.get("result", [])

    async def get_settlement_prices(self, product_id: int) -> Dict:
        """GET /v2/products/{product_id}/settlement_prices."""
        resp = await self._request("GET", f"/v2/products/{product_id}/settlement_prices", auth=False)
        return resp.get("result", {})

    # ── Account & Wallet ───────────────────────────────────────────────────

    async def get_wallet_balance(self, asset: str = "USDT") -> float:
        """GET /v2/wallet/balances — available balance for asset."""
        resp = await self._request("GET", "/v2/wallet/balances", auth=True)
        for bal in resp.get("result", []):
            if bal.get("asset_symbol") == asset:
                return float(bal.get("available_balance", 0))
        return 0.0

    async def get_all_wallet_balances(self) -> List[Dict]:
        """GET /v2/wallet/balances — all asset balances."""
        resp = await self._request("GET", "/v2/wallet/balances", auth=True)
        return resp.get("result", [])

    async def get_wallet_transactions(self, page_size: int = 50) -> List[Dict]:
        """GET /v2/wallet/transactions — transaction history."""
        resp = await self._request(
            "GET", "/v2/wallet/transactions",
            params={"page_size": page_size}, auth=True
        )
        return resp.get("result", [])

    async def get_user(self) -> Dict:
        """GET /v2/users/me — account info."""
        resp = await self._request("GET", "/v2/users/me", auth=True)
        return resp.get("result", {})

    async def get_rate_limit_quota(self) -> Dict:
        """GET /v2/users/rate_limit — current quota remaining."""
        resp = await self._request("GET", "/v2/users/rate_limit", auth=True)
        return resp.get("result", {})

    # ── Positions ──────────────────────────────────────────────────────────

    async def get_positions(self) -> List[Position]:
        """GET /v2/positions/margined — all open margined positions."""
        resp      = await self._request("GET", "/v2/positions/margined", auth=True)
        positions = []
        for p in resp.get("result", []):
            size = float(p.get("size", 0))
            if size == 0:
                continue
            product = p.get("product", {}) or {}
            positions.append(Position(
                product_id      = p["product_id"],
                symbol          = product.get("symbol", ""),
                size            = abs(size),
                entry_price     = float(p.get("entry_price") or 0),
                mark_price      = float(p.get("mark_price") or 0),
                unrealized_pnl  = float(p.get("unrealized_pnl") or 0),
                realized_pnl    = float(p.get("realized_pnl") or 0),
                margin          = float(p.get("margin") or 0),
                side            = "long" if size > 0 else "short",
                liquidation_price = float(p.get("liquidation_price") or 0) or None,
            ))
        return positions

    async def get_position(self, product_id: int) -> Optional[Position]:
        """GET /v2/positions — single position for product."""
        try:
            resp = await self._request("GET", "/v2/positions",
                                       params={"product_id": product_id}, auth=True)
            p    = resp.get("result", {})
            size = float(p.get("size", 0))
            if size == 0:
                return None
            product = p.get("product", {}) or {}
            return Position(
                product_id      = p["product_id"],
                symbol          = product.get("symbol", ""),
                size            = abs(size),
                entry_price     = float(p.get("entry_price") or 0),
                mark_price      = float(p.get("mark_price") or 0),
                unrealized_pnl  = float(p.get("unrealized_pnl") or 0),
                realized_pnl    = float(p.get("realized_pnl") or 0),
                margin          = float(p.get("margin") or 0),
                side            = "long" if size > 0 else "short",
            )
        except DeltaAPIError:
            return None

    async def close_all_positions(self) -> bool:
        """DELETE /v2/positions — emergency close all positions."""
        try:
            await self._request("DELETE", "/v2/positions", auth=True)
            logger.warning("⚠️ ALL POSITIONS CLOSED (emergency)")
            return True
        except DeltaAPIError as exc:
            logger.error("close_all_positions failed: %s", exc)
            return False

    async def add_position_margin(self, product_id: int, amount: float) -> bool:
        """POST /v2/positions/change_margin — add or remove margin."""
        try:
            await self._request(
                "POST", "/v2/positions/change_margin",
                data={"product_id": product_id, "delta_margin": str(round(amount, 2))},
                auth=True,
            )
            return True
        except DeltaAPIError as exc:
            logger.warning("add_position_margin failed: %s", exc)
            return False

    # ── Single Orders ──────────────────────────────────────────────────────

    async def place_order(self, order: Order) -> Order:
        """POST /v2/orders — standard market or limit order."""
        if not isinstance(order.size, int) or order.size < 1:
            raise ValueError(f"order.size must be a positive integer, got {order.size!r}")

        payload: Dict[str, Any] = {
            "product_id":    order.product_id,
            "side":          order.side.value,
            "order_type":    order.order_type.value,
            "size":          order.size,
            "time_in_force": order.time_in_force.value,
            "reduce_only":   order.reduce_only,
        }
        if order.limit_price is not None:
            payload["limit_price"] = str(round(order.limit_price, 2))
        if order.stop_price is not None:
            payload["stop_price"] = str(round(order.stop_price, 2))
        if order.client_order_id:
            payload["client_order_id"] = order.client_order_id

        logger.info("Placing %s %s %d lots @ %s", order.order_type.value, order.side.value, order.size, order.limit_price)
        resp   = await self._request("POST", "/v2/orders", data=payload)
        result = resp.get("result", {})

        order.order_id   = str(result.get("id", ""))
        order.created_at = result.get("created_at")
        try:
            order.status = OrderStatus(result.get("state", "pending"))
        except ValueError:
            order.status = OrderStatus.PENDING

        logger.info("✅ Order placed — id=%s status=%s", order.order_id, order.status)
        return order

    async def place_bracket_order(
        self,
        product_id:        int,
        side:              OrderSide,
        size:              int,
        stop_loss_price:   Optional[float]      = None,
        take_profit_price: Optional[float]      = None,
        entry_price:       Optional[float]      = None,
        trigger_method:    StopTriggerMethod    = StopTriggerMethod.LAST_TRADED_PRICE,
        client_order_id:   Optional[str]        = None,
        trail_amount:      Optional[float]      = None,
    ) -> BracketOrderResult:
        """
        POST /v2/orders — bracket order (entry + exchange-native SL + TP).

        Per Delta Exchange docs, the bracket fields go directly on the order:
            bracket_stop_loss_price       — exchange-side SL trigger
            bracket_take_profit_price     — exchange-side TP trigger
            bracket_stop_trigger_method   — last_traded_price / mark_price / index_price

        The exchange fires these atomically — no bot required for exit.
        If entry_price is None, uses market order entry.
        """
        if size < 1:
            raise ValueError(f"Bracket order size must be ≥ 1 lot, got {size}")

        payload: Dict[str, Any] = {
            "product_id":    product_id,
            "side":          side.value,
            "order_type":    "market_order",
            "size":          size,
            "time_in_force": "gtc",
        }

        if entry_price is not None:
            payload["order_type"]  = "limit_order"
            payload["limit_price"] = str(round(entry_price, 2))

        if stop_loss_price is not None and stop_loss_price > 0:
            payload["bracket_stop_loss_price"]       = str(round(stop_loss_price, 2))
            payload["bracket_stop_trigger_method"]    = trigger_method.value
            if trail_amount is not None and trail_amount > 0:
                payload["bracket_trail_amount"]      = str(round(trail_amount, 2))

        if take_profit_price is not None and take_profit_price > 0:
            payload["bracket_take_profit_price"]     = str(round(take_profit_price, 2))

        if client_order_id:
            payload["client_order_id"] = client_order_id

        logger.info(
            "🔲 BRACKET → %s %d lots | entry=%s sl=%s tp=%s trigger=%s",
            side.value.upper(), size,
            f"{entry_price:.4f}" if entry_price else "MARKET",
            f"{stop_loss_price:.4f}" if stop_loss_price else "NONE",
            f"{take_profit_price:.4f}" if take_profit_price else "NONE",
            trigger_method.value,
        )

        resp   = await self._request("POST", "/v2/orders", data=payload)
        result = resp.get("result", {})

        r = BracketOrderResult(
            entry_order_id = str(result.get("id", "")),
            sl_order_id    = str(result.get("bracket_stop_loss_order_id", "")) or None,
            tp_order_id    = str(result.get("bracket_take_profit_order_id", "")) or None,
            entry_side     = side.value,
            size           = size,
            raw            = result,
        )
        logger.info("✅ Bracket placed — entry_id=%s sl_id=%s tp_id=%s", r.entry_order_id, r.sl_order_id, r.tp_order_id)
        return r

    async def place_position_bracket(
        self,
        product_id:        int,
        stop_loss_price:   Optional[float]   = None,
        take_profit_price: Optional[float]   = None,
        trigger_method:    StopTriggerMethod = StopTriggerMethod.LAST_TRADED_PRICE,
        trail_amount:      Optional[float]   = None,
    ) -> bool:
        """
        POST /v2/orders/bracket — attach SL/TP to an EXISTING open position.
        Separate from the order bracket endpoint.
        """
        payload: Dict[str, Any] = {
            "product_id":                  product_id,
            "bracket_stop_trigger_method": trigger_method.value,
        }
        if stop_loss_price:
            sl_order: Dict[str, Any] = {
                "order_type": "market_order",
                "stop_price": str(round(stop_loss_price, 2)),
            }
            if trail_amount:
                sl_order["trail_amount"] = str(round(trail_amount, 2))
            payload["stop_loss_order"] = sl_order
        if take_profit_price:
            payload["take_profit_order"] = {
                "order_type": "limit_order",
                "stop_price": str(round(take_profit_price, 2)),
                "limit_price": str(round(take_profit_price, 2)),
            }
        try:
            await self._request("POST", "/v2/orders/bracket", data=payload)
            logger.info("✅ Position bracket set: sl=%s tp=%s", stop_loss_price, take_profit_price)
            return True
        except DeltaAPIError as exc:
            logger.warning("Position bracket failed: %s", exc)
            return False

    async def edit_bracket_order(
        self,
        order_id:          int,
        product_id:        int,
        stop_loss_price:   Optional[float]   = None,
        take_profit_price: Optional[float]   = None,
        trigger_method:    StopTriggerMethod = StopTriggerMethod.LAST_TRADED_PRICE,
    ) -> bool:
        """PUT /v2/orders/bracket — edit existing bracket (move trailing SL / TP)."""
        payload: Dict[str, Any] = {
            "id":                          order_id,
            "product_id":                  product_id,
            "bracket_stop_trigger_method": trigger_method.value,
        }
        if stop_loss_price:
            payload["bracket_stop_loss_price"]   = str(round(stop_loss_price, 2))
        if take_profit_price:
            payload["bracket_take_profit_price"] = str(round(take_profit_price, 2))
        try:
            await self._request("PUT", "/v2/orders/bracket", data=payload)
            logger.debug("✅ Bracket edited: id=%s sl=%s tp=%s", order_id, stop_loss_price, take_profit_price)
            return True
        except DeltaAPIError as exc:
            logger.warning("edit_bracket_order failed: %s", exc)
            return False

    async def edit_order(
        self,
        order_id:    int,
        product_id:  int,
        size:        Optional[int]   = None,
        limit_price: Optional[float] = None,
        stop_price:  Optional[float] = None,
    ) -> bool:
        """PUT /v2/orders — edit existing limit/stop order."""
        payload: Dict[str, Any] = {"id": order_id, "product_id": product_id}
        if size is not None:
            payload["size"] = size
        if limit_price is not None:
            payload["limit_price"] = str(round(limit_price, 2))
        if stop_price is not None:
            payload["stop_price"] = str(round(stop_price, 2))
        try:
            await self._request("PUT", "/v2/orders", data=payload)
            return True
        except DeltaAPIError as exc:
            logger.warning("edit_order failed: %s", exc)
            return False

    async def cancel_order(self, order_id: Union[str, int], product_id: int) -> bool:
        """DELETE /v2/orders — cancel a single order."""
        try:
            await self._request("DELETE", "/v2/orders",
                                data={"id": int(order_id), "product_id": product_id})
            return True
        except DeltaAPIError as exc:
            logger.warning("cancel_order %s failed: %s", order_id, exc)
            return False

    async def cancel_all_orders(self, product_id: int) -> bool:
        """DELETE /v2/orders/all — cancel ALL open orders for a product."""
        try:
            await self._request(
                "DELETE", "/v2/orders/all",
                data={
                    "product_id":          product_id,
                    "cancel_limit_orders": True,
                    "cancel_stop_orders":  True,
                },
            )
            logger.info("🔴 All orders cancelled for product_id=%d", product_id)
            return True
        except DeltaAPIError as exc:
            logger.warning("cancel_all_orders failed: %s", exc)
            return False

    # ── Order Queries ──────────────────────────────────────────────────────

    async def get_order_by_id(self, order_id: Union[str, int]) -> Optional[Dict]:
        """GET /v2/orders/{id} — fetch single order by exchange ID."""
        try:
            resp = await self._request("GET", f"/v2/orders/{order_id}")
            return resp.get("result")
        except DeltaAPIError as exc:
            logger.warning("get_order_by_id(%s) failed: %s", order_id, exc)
            return None

    async def get_order_by_client_id(self, client_order_id: str) -> Optional[Dict]:
        """GET /v2/orders/client_order_id/{id} — fetch order by client order ID."""
        try:
            resp = await self._request("GET", f"/v2/orders/client_order_id/{client_order_id}")
            return resp.get("result")
        except DeltaAPIError as exc:
            logger.warning("get_order_by_client_id(%s) failed: %s", client_order_id, exc)
            return None

    async def get_open_orders(self, product_id: Optional[int] = None, page_size: int = 100) -> List[Dict]:
        """GET /v2/orders — open + pending orders, paginated."""
        params: Dict[str, Any] = {"states": "open,pending", "page_size": page_size}
        if product_id:
            params["product_ids"] = str(product_id)
        resp = await self._request("GET", "/v2/orders", params=params)
        return resp.get("result", [])

    async def get_order_history(self, product_id: Optional[int] = None, page_size: int = 50) -> List[Dict]:
        """GET /v2/orders/history — cancelled and filled orders."""
        params: Dict[str, Any] = {"page_size": page_size}
        if product_id:
            params["product_id"] = product_id
        resp = await self._request("GET", "/v2/orders/history", params=params)
        return resp.get("result", [])

    async def get_fills(self, order_id: Optional[str] = None, product_id: Optional[int] = None,
                        page_size: int = 50) -> List[Fill]:
        """GET /v2/fills — user fills, optionally filtered by order or product."""
        params: Dict[str, Any] = {"page_size": page_size}
        if order_id:
            params["order_id"] = order_id
        if product_id:
            params["product_id"] = product_id
        try:
            resp  = await self._request("GET", "/v2/fills", params=params)
            fills = []
            for f in resp.get("result", []):
                fills.append(Fill(
                    id         = str(f.get("id", "")),
                    order_id   = str(f.get("order_id", "")),
                    product_id = int(f.get("product_id", 0)),
                    price      = float(f.get("price", 0)),
                    size       = float(f.get("size", 0)),
                    side       = f.get("side", ""),
                    fee        = float(f.get("commission", 0)),
                    timestamp  = f.get("created_at", ""),
                ))
            return fills
        except Exception as exc:
            logger.debug("get_fills failed: %s", exc)
            return []

    async def get_actual_fill_price(self, order_id: str, fallback: float) -> float:
        """Return VWAP fill price for an order, fallback if not available."""
        fills = await self.get_fills(order_id=order_id)
        if fills:
            total_qty   = sum(f.size for f in fills)
            total_value = sum(f.price * f.size for f in fills)
            if total_qty > 0:
                return total_value / total_qty
        return fallback

    # ── Batch Orders ───────────────────────────────────────────────────────

    async def place_batch_orders(self, product_id: int, orders: List[Dict]) -> List[Dict]:
        """POST /v2/orders/batch — up to 50 limit orders atomically."""
        if not orders:
            return []
        if len(orders) > 50:
            raise ValueError("Batch size must be ≤ 50 orders")
        resp = await self._request(
            "POST", "/v2/orders/batch",
            data={"product_id": product_id, "orders": orders}
        )
        return resp.get("result", [])

    async def edit_batch_orders(self, product_id: int, orders: List[Dict]) -> List[Dict]:
        """PUT /v2/orders/batch — edit multiple orders atomically."""
        if not orders:
            return []
        resp = await self._request(
            "PUT", "/v2/orders/batch",
            data={"product_id": product_id, "orders": orders}
        )
        return resp.get("result", [])

    async def delete_batch_orders(self, product_id: int, order_ids: List[Union[str, int]]) -> List[Dict]:
        """DELETE /v2/orders/batch — cancel multiple orders by ID."""
        if not order_ids:
            return []
        resp = await self._request(
            "DELETE", "/v2/orders/batch",
            data={"product_id": product_id, "orders": [{"id": int(oid)} for oid in order_ids]}
        )
        return resp.get("result", [])

    # ── Leverage ───────────────────────────────────────────────────────────

    async def set_leverage(self, product_id: int, leverage: int) -> bool:
        """POST /v2/orders/leverage — set leverage for a product."""
        try:
            await self._request(
                "POST", "/v2/orders/leverage",
                data={"product_id": product_id, "leverage": str(leverage)},
            )
            logger.info("✅ Leverage set to %dx for product_id=%d", leverage, product_id)
            return True
        except DeltaAPIError as exc:
            logger.warning("set_leverage failed: %s", exc)
            return False

    async def get_leverage(self, product_id: int) -> Optional[Dict]:
        """GET /v2/orders/leverage — current leverage for a product."""
        try:
            resp = await self._request("GET", "/v2/orders/leverage",
                                       params={"product_id": product_id})
            return resp.get("result")
        except DeltaAPIError:
            return None

    async def change_margin_mode(self, product_id: int, mode: str) -> bool:
        """POST /v2/users/change_margin_mode — 'isolated' or 'cross'."""
        try:
            await self._request(
                "POST", "/v2/users/change_margin_mode",
                data={"product_id": product_id, "margin_mode": mode},
            )
            return True
        except DeltaAPIError as exc:
            logger.warning("change_margin_mode failed: %s", exc)
            return False

    # ── MMP (Market Maker Protection) ─────────────────────────────────────

    async def update_mmp_config(self, product_id: int, config: Dict) -> bool:
        """POST /v2/mmp/config — update Market Maker Protection settings."""
        try:
            await self._request("POST", "/v2/mmp/config",
                                data={"product_id": product_id, **config})
            return True
        except DeltaAPIError as exc:
            logger.warning("update_mmp_config failed: %s", exc)
            return False

    async def reset_mmp(self, product_id: int) -> bool:
        """POST /v2/mmp/reset — reset MMP after trigger."""
        try:
            await self._request("POST", "/v2/mmp/reset", data={"product_id": product_id})
            return True
        except DeltaAPIError as exc:
            logger.warning("reset_mmp failed: %s", exc)
            return False

    # ── Volume Stats ───────────────────────────────────────────────────────

    async def get_volume_stats(self) -> Dict:
        """GET /v2/stats — exchange-wide volume stats."""
        resp = await self._request("GET", "/v2/stats", auth=False)
        return resp.get("result", {})


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket Client
# ─────────────────────────────────────────────────────────────────────────────

class DeltaWSClient:
    """
    WebSocket client for Delta Exchange India.

    Production URL:  wss://socket.india.delta.exchange
    Testnet URL:     wss://socket-ind.testnet.deltaex.org

    Public channels:
      v2/ticker      — price/volume snapshot per symbol
      l2_orderbook   — L2 order book snapshot + updates
      l2_updates     — incremental L2 order book updates
      all_trades     — public trade stream
      mark_price     — mark price updates
      candlesticks   — OHLCV candle stream (specify resolution in symbol)
      funding_rate   — perpetual funding rate updates

    Private channels (require auth):
      orders         — order fills and status updates
      positions      — position updates
      margins        — margin updates
      user_trades    — your fills stream
      liquidations   — liquidation events
    """

    WS_URL = "wss://socket.india.delta.exchange"

    def __init__(self, api_key: str, api_secret: str, on_message: Callable):
        self.api_key         = api_key
        self.api_secret      = api_secret
        self.on_message      = on_message
        self._ws             = None
        self._subscriptions: List[Dict] = []
        self._running        = False
        self._is_async       = inspect.iscoroutinefunction(on_message)

    def _auth_payload(self) -> Dict:
        """Auth payload for private WebSocket channels."""
        timestamp = str(int(time.time()))
        msg       = "GET" + timestamp + "/live"
        sig       = hmac.new(
            self.api_secret.encode("utf-8"),
            msg.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "type": "auth",
            "payload": {
                "api-key":   self.api_key,
                "signature": sig,
                "timestamp": timestamp,
            },
        }

    def subscribe_public(self, channel: str, symbols: List[str]):
        """
        Subscribe to a public channel.
        Examples:
            subscribe_public("v2/ticker", ["BTCUSD", "ETHUSD"])
            subscribe_public("l2_orderbook", ["BTCUSD"])
            subscribe_public("candlesticks.1m", ["BTCUSD"])  # resolution in channel name
            subscribe_public("all_trades", ["BTCUSD"])
        """
        self._subscriptions.append({
            "type": "subscribe",
            "payload": {
                "channels": [{"name": channel, "symbols": symbols}]
            }
        })

    def subscribe_private(self, channels: List[str]):
        """
        Subscribe to private channels (after auth).
        channels: ["orders", "positions", "margins", "user_trades"]
        """
        self._subscriptions.append({
            "type": "subscribe",
            "payload": {
                "channels": [{"name": ch} for ch in channels]
            }
        })

    # Legacy helper for backward compatibility
    def subscribe(self, channels: List[Dict]):
        self._subscriptions.extend(channels)

    async def _dispatch(self, msg: Dict):
        try:
            if self._is_async:
                await self.on_message(msg)
            else:
                await asyncio.get_event_loop().run_in_executor(None, self.on_message, msg)
        except Exception as exc:
            logger.error("WS handler error: %s", exc, exc_info=True)

    async def _heartbeat(self, ws):
        """Send ping every 20s to prevent silent drop (60s inactivity disconnect)."""
        while self._running:
            try:
                await asyncio.sleep(20)
                await ws.ping()
            except Exception:
                break

    async def connect(self):
        """Connect, auth, subscribe, and dispatch messages with auto-reconnect."""
        self._running = True
        backoff       = 2
        while self._running:
            try:
                async with websockets.connect(
                    self.WS_URL,
                    ping_interval  = None,   # we handle ping manually
                    close_timeout  = 5,
                    max_size       = 2 ** 20,
                    extra_headers  = {"User-Agent": "DeltaAlgoBot/6.0"},
                ) as ws:
                    self._ws = ws
                    backoff  = 2

                    # Always authenticate first (required for private channels)
                    await ws.send(json.dumps(self._auth_payload()))
                    logger.info("🔌 WebSocket connected + authenticated")

                    # Send all subscriptions
                    for sub in self._subscriptions:
                        await ws.send(json.dumps(sub))
                        logger.debug("Subscribed: %s", sub.get("payload", {}).get("channels", sub))

                    hb_task = asyncio.create_task(self._heartbeat(ws))
                    try:
                        async for raw in ws:
                            try:
                                msg = json.loads(raw)
                                if isinstance(msg, dict):
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


__all__ = [
    "DeltaRESTClient", "DeltaWSClient",
    "Order", "BracketOrderResult", "Position", "OHLCV", "L2OrderBook", "Ticker", "Fill",
    "OrderType", "OrderSide", "OrderStatus", "TimeInForce", "StopTriggerMethod",
    "DeltaAPIError", "RateLimiter",
]
