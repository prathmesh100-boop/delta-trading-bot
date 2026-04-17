"""
api.py — Delta Exchange India REST + WebSocket Client
Production-grade with HMAC auth, rate limiting, bracket orders, WebSocket.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

import aiohttp
import websockets
from delta_bot.symbol_specs import SYMBOL_SPECS
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

RESOLUTION_MAP = {
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

# ─── Enums ────────────────────────────────────────────────────────────────────

class OrderSide(str, Enum):
    BUY  = "buy"
    SELL = "sell"

class OrderType(str, Enum):
    MARKET      = "market_order"
    LIMIT       = "limit_order"
    STOP_MARKET = "stop_market_order"
    STOP_LIMIT  = "stop_limit_order"

class TimeInForce(str, Enum):
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"

# ─── Data Classes ─────────────────────────────────────────────────────────────

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
    side:           str = ""
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
class Ticker:
    symbol:       str
    last_price:   float
    mark_price:   float
    index_price:  float
    bid:          float
    ask:          float
    volume:       float
    open_interest: float
    funding_rate:  float

@dataclass
class L2OrderBook:
    symbol: str
    buy:    List[Dict]
    sell:   List[Dict]

    def best_bid(self) -> Optional[float]:
        return float(self.buy[0]["limit_price"]) if self.buy else None

    def best_ask(self) -> Optional[float]:
        return float(self.sell[0]["limit_price"]) if self.sell else None

    def spread(self) -> Optional[float]:
        b, a = self.best_bid(), self.best_ask()
        return (a - b) if b and a else None

    def imbalance(self, levels: int = 5) -> float:
        bid_vol = sum(float(x["size"]) for x in self.buy[:levels])
        ask_vol = sum(float(x["size"]) for x in self.sell[:levels])
        total   = bid_vol + ask_vol
        return (bid_vol - ask_vol) / total if total > 0 else 0.0

@dataclass
class BracketOrderResult:
    entry_order_id: str
    sl_order_id:    Optional[str]
    tp_order_id:    Optional[str]
    entry_side:     str
    size:           int
    average_fill_price: Optional[float] = None
    filled_size:    int = 0
    state:          str = ""
    raw:            Dict = field(default_factory=dict)

# ─── Exceptions ───────────────────────────────────────────────────────────────

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

# ─── Rate Limiter ─────────────────────────────────────────────────────────────

class RateLimiter:
    def __init__(self, max_calls: int = 20, window_seconds: float = 1.0):
        self._max    = max_calls
        self._window = window_seconds
        self._times: deque = deque()

    async def acquire(self):
        now = time.monotonic()
        while self._times and now - self._times[0] > self._window:
            self._times.popleft()
        if len(self._times) >= self._max:
            sleep_for = self._window - (now - self._times[0]) + 0.02
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
        self._times.append(time.monotonic())

# ─── Lot Size Registry ────────────────────────────────────────────────────────

FALLBACK_LOT_SIZES: Dict[str, float] = {
    symbol: spec.fallback_lot_size for symbol, spec in SYMBOL_SPECS.items()
}
FALLBACK_LOT_SIZES.update({
    "MATIC_USDT": 10.0,
    "DOGE_USDT": 100.0,
    "AVAX_USDT": 1.0,
    "LINK_USDT": 1.0,
    "DOT_USDT": 1.0,
})

# ─── REST Client ──────────────────────────────────────────────────────────────

class DeltaRESTClient:
    """
    Delta Exchange India REST API client.
    Base URL: https://api.india.delta.exchange
    Auth: HMAC-SHA256 of method+timestamp+path+querystring+body
    """

    BASE_URL = "https://api.india.delta.exchange"

    def __init__(self, api_key: str, api_secret: str):
        self.api_key    = api_key
        self.api_secret = api_secret
        self._session:  Optional[aiohttp.ClientSession] = None
        self._limiter   = RateLimiter(max_calls=20, window_seconds=1.0)
        self._lot_cache: Dict[str, float] = {}

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            headers={"Content-Type": "application/json", "Accept": "application/json"}
        )
        return self

    async def __aexit__(self, *_):
        if self._session:
            await self._session.close()

    def _sign(self, method: str, path: str, query: str = "", body: str = "") -> Dict[str, str]:
        timestamp = str(int(time.time()))
        msg = method.upper() + timestamp + path + query + body
        sig = hmac.new(
            self.api_secret.encode("utf-8"),
            msg.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "api-key":   self.api_key,
            "timestamp": timestamp,
            "signature": sig,
        }

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
        data:   Optional[Dict] = None,
        auth:   bool = True,
        retries: int = 3,
    ) -> Dict:
        import urllib.parse
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        body  = json.dumps(data, separators=(",", ":")) if data else ""
        url   = self.BASE_URL + path + (("?" + urllib.parse.urlencode(params)) if params else "")

        last_exc = None
        for attempt in range(retries):
            await self._limiter.acquire()
            headers = {}
            if auth:
                headers.update(self._sign(method, path, query, body))

            try:
                async with self._session.request(
                    method, url,
                    headers = headers,
                    data    = body or None,
                    timeout = aiohttp.ClientTimeout(total=10),
                ) as resp:
                    raw = await resp.json(content_type=None)
                    if resp.status == 429:
                        retry_after = int(resp.headers.get("X-RATE-LIMIT-RESET", 2))
                        logger.warning("Rate limit hit — sleeping %ds", retry_after)
                        await asyncio.sleep(retry_after)
                        continue
                    if resp.status >= 400:
                        raise DeltaAPIError(resp.status, raw)
                    return raw
            except DeltaAPIError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                backoff = 2 ** attempt
                logger.warning("Request failed (%s), retry %d/%d in %ds", exc, attempt+1, retries, backoff)
                await asyncio.sleep(backoff)

        raise last_exc or RuntimeError("Request failed after retries")

    # ── Market Data ───────────────────────────────────────────────────────────

    async def get_products(self) -> List[Dict]:
        resp = await self._request("GET", "/v2/products", auth=False)
        return resp.get("result", [])

    async def get_product(self, symbol: str) -> Optional[Dict]:
        products = await self.get_products()
        return next((p for p in products if p.get("symbol") == symbol), None)

    @staticmethod
    def infer_account_asset(product: Optional[Dict], symbol: str = "") -> str:
        candidates = []
        if product:
            candidates.extend([
                product.get("settlement_asset", {}).get("symbol") if isinstance(product.get("settlement_asset"), dict) else None,
                product.get("settlement_currency"),
                product.get("settlement_currency_symbol"),
                product.get("settlement_asset_symbol"),
                product.get("quote_asset", {}).get("symbol") if isinstance(product.get("quote_asset"), dict) else None,
                product.get("quoting_asset", {}).get("symbol") if isinstance(product.get("quoting_asset"), dict) else None,
                product.get("quote_currency"),
                product.get("quote_currency_symbol"),
                product.get("quote_asset_symbol"),
            ])
        if symbol.endswith("USDT"):
            candidates.append("USDT")
        if symbol.endswith("USD"):
            candidates.append("USD")
        for item in candidates:
            if isinstance(item, str) and item.strip():
                return item.strip().upper()
        return "USDT"

    async def get_ticker(self, symbol: str) -> Ticker:
        resp = await self._request("GET", f"/v2/tickers/{symbol}", auth=False)
        r = resp.get("result", {})
        return Ticker(
            symbol       = symbol,
            last_price   = float(r.get("close", 0)),
            mark_price   = float(r.get("mark_price", 0)),
            index_price  = float(r.get("spot_price", 0)),
            bid          = float(r.get("bid", 0)),
            ask          = float(r.get("ask", 0)),
            volume       = float(r.get("volume", 0)),
            open_interest= float(r.get("oi", 0)),
            funding_rate = float(r.get("funding_rate", 0)),
        )

    async def get_ohlcv(
        self,
        symbol:     str,
        resolution: int | str,
        start_time: int,
        end_time:   int,
    ) -> List[OHLCV]:
        resolution_value = self._normalize_resolution(resolution)
        resp = await self._request(
            "GET", "/v2/history/candles",
            params={
                "symbol":     symbol,
                "resolution": resolution_value,
                "start":      start_time,
                "end":        end_time,
            },
            auth=False,
        )
        candles = []
        for c in resp.get("result", []):
            candles.append(OHLCV(
                timestamp = int(c.get("time", 0)),
                open      = float(c.get("open", 0)),
                high      = float(c.get("high", 0)),
                low       = float(c.get("low", 0)),
                close     = float(c.get("close", 0)),
                volume    = float(c.get("volume", 0)),
            ))
        return sorted(candles, key=lambda x: x.timestamp)

    def _normalize_resolution(self, resolution: int | str) -> str:
        if isinstance(resolution, str):
            value = resolution.strip().lower()
            if value:
                return value
        mapped = RESOLUTION_MAP.get(int(resolution))
        if mapped:
            return mapped
        raise ValueError(
            f"Unsupported resolution '{resolution}'. Use one of: "
            + ", ".join(str(k) for k in sorted(RESOLUTION_MAP))
        )

    async def get_orderbook(self, symbol: str, depth: int = 10) -> L2OrderBook:
        resp = await self._request(
            "GET", f"/v2/l2orderbook/{symbol}",
            params={"depth": depth},
            auth=False,
        )
        r = resp.get("result", {})
        return L2OrderBook(
            symbol = symbol,
            buy    = r.get("buy", []),
            sell   = r.get("sell", []),
        )

    async def get_funding_rate(self, symbol: str) -> float:
        try:
            t = await self.get_ticker(symbol)
            return t.funding_rate
        except Exception:
            return 0.0

    # ── Account ───────────────────────────────────────────────────────────────

    async def get_wallet_balance(self, asset: str = "USDT") -> float:
        resp = await self._request("GET", "/v2/wallet/balances")
        balances = resp.get("result", [])
        for b in balances:
            if b.get("asset_symbol") == asset:
                return float(b.get("available_balance", 0))
        if balances:
            ranked = sorted(
                balances,
                key=lambda b: float(b.get("equity", 0) or b.get("total_balance", 0) or b.get("wallet_balance", 0) or b.get("balance", 0) or b.get("available_balance", 0) or 0),
                reverse=True,
            )
            return float(ranked[0].get("available_balance", 0) or 0)
        return 0.0

    async def get_account_equity(self, asset: str = "USDT") -> float:
        """
        Best-effort account equity.
        Prefer exchange-reported equity fields; otherwise approximate with
        available balance + margin in use + unrealized PnL.
        """
        resp = await self._request("GET", "/v2/wallet/balances")
        balances = resp.get("result", [])
        equity = 0.0
        available = 0.0
        matched = False

        for b in balances:
            if b.get("asset_symbol") != asset:
                continue
            matched = True
            for field in ("equity", "total_balance", "wallet_balance", "balance"):
                raw = b.get(field)
                if raw is not None:
                    try:
                        equity = float(raw)
                        break
                    except (TypeError, ValueError):
                        continue
            try:
                available = float(b.get("available_balance", 0) or 0)
            except (TypeError, ValueError):
                available = 0.0
            break

        if equity > 0:
            return equity

        if not matched and balances:
            ranked = []
            for b in balances:
                balance_equity = 0.0
                for field in ("equity", "total_balance", "wallet_balance", "balance", "available_balance"):
                    raw = b.get(field)
                    if raw is None:
                        continue
                    try:
                        balance_equity = float(raw)
                        break
                    except (TypeError, ValueError):
                        continue
                ranked.append((balance_equity, str(b.get("asset_symbol", ""))))
            ranked.sort(reverse=True)
            if ranked and ranked[0][0] > 0:
                logger.warning(
                    "Requested account asset '%s' not found; using largest wallet equity from '%s' = %.4f",
                    asset, ranked[0][1], ranked[0][0],
                )
                return ranked[0][0]

        try:
            positions = await self.get_positions()
        except Exception:
            positions = []

        margin_in_use = sum(max(0.0, float(p.margin)) for p in positions)
        unrealized = sum(float(p.unrealized_pnl) for p in positions)
        approx_equity = available + margin_in_use + unrealized
        return approx_equity if approx_equity > 0 else available

    async def get_positions(self) -> List[Position]:
        resp = await self._request("GET", "/v2/positions/margined")
        positions = []
        for p in resp.get("result", []):
            size = float(p.get("size", 0))
            if size == 0:
                continue
            side = "long" if size > 0 else "short"
            positions.append(Position(
                product_id     = int(p.get("product_id", 0)),
                symbol         = p.get("product_symbol", ""),
                size           = abs(size),
                entry_price    = float(p.get("entry_price", 0)),
                mark_price     = float(p.get("mark_price", 0)),
                unrealized_pnl = float(p.get("unrealized_pnl", 0)),
                realized_pnl   = float(p.get("realized_pnl", 0)),
                margin         = float(p.get("margin", 0)),
                side           = side,
                liquidation_price = float(p.get("liquidation_price", 0)) or None,
            ))
        return positions

    async def get_open_orders(self, product_id: Optional[int] = None) -> List[Dict]:
        params = {"state": "open"}
        if product_id:
            params["product_id"] = product_id
        resp = await self._request("GET", "/v2/orders", params=params)
        return resp.get("result", [])

    async def get_order_by_id(self, order_id: str) -> Optional[Dict]:
        try:
            resp = await self._request("GET", f"/v2/orders/{order_id}")
            return resp.get("result")
        except DeltaAPIError:
            return None

    async def get_order_by_client_order_id(self, client_order_id: str) -> Optional[Dict]:
        try:
            resp = await self._request("GET", f"/v2/orders/client_order_id/{client_order_id}")
            return resp.get("result")
        except DeltaAPIError:
            return None

    async def cancel_all_orders(self, product_id: int) -> bool:
        try:
            await self._request("DELETE", "/v2/orders/all",
                                data={"product_id": product_id, "cancel_limit_orders": True, "cancel_stop_orders": True})
            return True
        except DeltaAPIError as exc:
            logger.warning("Cancel all orders failed: %s", exc)
            return False

    # ── Order Placement ───────────────────────────────────────────────────────

    async def place_market_order(
        self,
        product_id: int,
        side: OrderSide,
        size: int,
        reduce_only: bool = False,
        client_order_id: str = "",
    ) -> Dict:
        payload: Dict[str, Any] = {
            "product_id":    product_id,
            "size":          size,
            "side":          side.value,
            "order_type":    "market_order",
            "time_in_force": "gtc",
        }
        if reduce_only:
            payload["reduce_only"] = True
        if client_order_id:
            payload["client_order_id"] = client_order_id

        resp = await self._request("POST", "/v2/orders", data=payload)
        return resp.get("result", resp)

    async def place_bracket_order(
        self,
        product_id:        int,
        side:              OrderSide,
        size:              int,
        stop_loss_price:   Optional[float] = None,
        take_profit_price: Optional[float] = None,
        client_order_id:   str = "",
    ) -> BracketOrderResult:
        """
        Place a bracket order (market entry + exchange-managed SL + TP).
        Uses POST /v2/orders with bracket_stop_loss_price and bracket_take_profit_price.
        """
        payload: Dict[str, Any] = {
            "product_id":    product_id,
            "size":          size,
            "side":          side.value,
            "order_type":    "market_order",
            "time_in_force": "gtc",
        }
        if stop_loss_price is not None:
            payload["bracket_stop_loss_price"]  = str(round(stop_loss_price, 2))
            payload["bracket_stop_loss_limit_price"] = str(round(stop_loss_price * (0.999 if side == OrderSide.BUY else 1.001), 2))
        if take_profit_price is not None:
            payload["bracket_take_profit_price"] = str(round(take_profit_price, 2))
            payload["bracket_take_profit_limit_price"] = str(round(take_profit_price * (0.999 if side == OrderSide.SELL else 1.001), 2))
        if client_order_id:
            payload["client_order_id"] = client_order_id

        resp   = await self._request("POST", "/v2/orders", data=payload)
        result = resp.get("result", resp)

        entry_id = str(result.get("id", ""))
        sl_id    = str(result.get("bracket_stop_loss_order_id", ""))
        tp_id    = str(result.get("bracket_take_profit_order_id", ""))

        logger.info(
            "🔲 BRACKET PLACED: %s %d lots | SL=%s TP=%s | entry_id=%s",
            side.value.upper(), size,
            f"{stop_loss_price:.2f}" if stop_loss_price else "NONE",
            f"{take_profit_price:.2f}" if take_profit_price else "NONE",
            entry_id,
        )
        return BracketOrderResult(
            entry_order_id = entry_id,
            sl_order_id    = sl_id or None,
            tp_order_id    = tp_id or None,
            entry_side     = side.value,
            size           = size,
            average_fill_price = float(result.get("average_fill_price", 0) or 0) or None,
            filled_size    = int(float(result.get("filled_size", 0) or 0)),
            state          = str(result.get("state", "") or result.get("order_state", "")),
            raw            = result,
        )

    async def close_position(self, product_id: int, side: str, size: int) -> Dict:
        """Close position with reduce-only market order."""
        close_side = OrderSide.SELL if side == "long" else OrderSide.BUY
        return await self.place_market_order(
            product_id   = product_id,
            side         = close_side,
            size         = size,
            reduce_only  = True,
        )

    # ── Leverage & Margin ─────────────────────────────────────────────────────

    async def set_leverage(self, product_id: int, leverage: int) -> bool:
        try:
            await self._request("POST", "/v2/orders/leverage",
                                data={"product_id": product_id, "leverage": str(leverage)})
            logger.info("✅ Leverage set to %dx for product_id=%d", leverage, product_id)
            return True
        except DeltaAPIError as exc:
            logger.warning("set_leverage failed: %s", exc)
            return False

    async def get_rate_limit_quota(self) -> Optional[Dict]:
        try:
            resp = await self._request("GET", "/v2/users/rate_limit")
            return resp.get("result")
        except Exception:
            return None

    # ── Lot Sizing ────────────────────────────────────────────────────────────

    async def get_lot_size(self, symbol: str) -> float:
        if symbol in self._lot_cache:
            return self._lot_cache[symbol]
        try:
            product = await self.get_product(symbol)
            if product:
                cv = float(product.get("contract_value", 0) or 0)
                if cv > 0:
                    self._lot_cache[symbol] = cv
                    return cv
        except Exception:
            pass
        fallback = FALLBACK_LOT_SIZES.get(symbol, 1.0)
        self._lot_cache[symbol] = fallback
        return fallback

    def usd_to_lots(self, symbol: str, usd_notional: float, price: float, cached_lot: float = 0.0) -> int:
        lot_size = cached_lot or FALLBACK_LOT_SIZES.get(symbol, 1.0)
        if lot_size <= 0 or price <= 0:
            return 1
        lots = int(usd_notional / (price * lot_size))
        return max(1, lots)


# ─── WebSocket Client ─────────────────────────────────────────────────────────

class DeltaWSClient:
    """
    Delta Exchange India WebSocket client.
    URL: wss://socket.india.delta.exchange
    """

    WS_URL = "wss://socket.india.delta.exchange"

    def __init__(self, api_key: str, api_secret: str, on_message: Callable):
        self.api_key    = api_key
        self.api_secret = api_secret
        self.on_message = on_message
        self._subs: List[Dict] = []
        self._ws    = None
        self._running = False

    def _auth_payload(self) -> Dict:
        ts  = str(int(time.time()))
        msg = "GET" + ts + "/live"
        sig = hmac.new(self.api_secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
        return {"type": "auth", "payload": {"api-key": self.api_key, "signature": sig, "timestamp": ts}}

    def subscribe_public(self, channel: str, symbols: List[str]):
        self._subs.append({
            "type": "subscribe",
            "payload": {"channels": [{"name": channel, "symbols": symbols}]},
        })

    def subscribe_private(self, channels: List[str], symbols: Optional[List[str]] = None):
        channel_payloads = []
        for ch in channels:
            payload = {"name": ch}
            if symbols and ch == "orders":
                payload["symbols"] = symbols
            channel_payloads.append(payload)
        self._subs.append({
            "type": "subscribe",
            "payload": {"channels": channel_payloads},
        })

    async def _dispatch(self, msg: Dict):
        try:
            import inspect
            if inspect.iscoroutinefunction(self.on_message):
                await self.on_message(msg)
            else:
                self.on_message(msg)
        except Exception as exc:
            logger.error("WS handler error: %s", exc)

    async def _heartbeat(self, ws):
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
                    ping_interval = None,
                    close_timeout = 5,
                    max_size      = 2 ** 20,
                ) as ws:
                    self._ws = ws
                    backoff  = 2
                    await ws.send(json.dumps(self._auth_payload()))
                    for sub in self._subs:
                        await ws.send(json.dumps(sub))
                    logger.info("🔌 WebSocket connected")

                    hb = asyncio.create_task(self._heartbeat(ws))
                    try:
                        async for raw in ws:
                            try:
                                msg = json.loads(raw)
                                if isinstance(msg, dict):
                                    await self._dispatch(msg)
                            except json.JSONDecodeError:
                                pass
                    finally:
                        hb.cancel()
            except (ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                logger.warning("WS disconnected: %s — reconnecting in %ds", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as exc:
                logger.error("WS error: %s — reconnecting in %ds", exc, backoff)
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
    "OrderSide", "OrderType", "TimeInForce",
    "Position", "OHLCV", "Ticker", "L2OrderBook", "BracketOrderResult",
    "DeltaAPIError",
]
