# Setup & Configuration Guide

## Environment Variables

Create a `.env` file in the root directory (or export these):

```bash
# Delta Exchange API
DELTA_API_KEY=your_api_key_here
DELTA_API_SECRET=your_api_secret_here

# Trading Configuration
TRADING_SYMBOL=BTC_USDT          # or ETH_USDT, SOL_USDT
TRADING_INTERVAL_MINUTES=15      # Candle interval
TRADING_RESOLUTION_MINUTES=15    # Resampling resolution

# Optional
LOG_LEVEL=INFO
NOTIFIER_WEBHOOK=https://...     # Slack/Discord webhook (optional)
```

## Installation

```bash
pip install -r requirements.txt
```

### Required Packages
- `aiohttp` - Async HTTP client for REST API
- `websockets` - WebSocket client for real-time ticks
- `pandas` - Candle/OHLCV data handling
- `numpy` - Numerical calculations
- `requests` - HTTP client for webhooks
- `python-dotenv` - Environment variable loading

## Running the Bot

### Option 1: Direct Python

```bash
python main.py \
    --symbol BTC_USDT \
    --interval 15 \
    --api-key $DELTA_API_KEY \
    --api-secret $DELTA_API_SECRET
```

### Option 2: With Environment File

```bash
export $(cat .env | xargs)
python main.py --symbol BTC_USDT --interval 15
```

### Option 3: Docker (Coming Soon)

```bash
docker build -t delta-bot .
docker run --env-file .env delta-bot
```

## Key Configuration Parameters

### Risk Management (main.py)

```python
RiskConfig = {
    "risk_per_trade": 0.02,          # 2% per position
    "max_open_trades": 2,            # Maximum concurrent positions
    "max_drawdown_pct": 0.15,        # Halt at 15% account drawdown
    "daily_loss_limit_pct": 0.10,    # Halt at 10% daily loss
}
```

### Leverage by Symbol (risk.py)

```python
leverage_by_symbol = {
    "BTC_USDT": 10,   # 10x leverage for BTC
    "ETH_USDT": 10,   # 10x leverage for ETH
    "SOL_USDT": 10,   # 10x leverage for SOL
}
```

### Lot Sizes (api.py)

```python
FALLBACK_LOT_SIZES = {
    "BTC_USDT": 0.001,    # 0.001 BTC per lot
    "ETH_USDT": 0.01,     # 0.01 ETH per lot
    "SOL_USDT": 1.0,      # 1 SOL per lot
}
```

## Operational Checklist

### Pre-Launch
- [ ] Delta API key configured and tested
- [ ] Test account with minimum $50 (for testing) or $500+ (for live)
- [ ] Symbol selection (BTC_USDT, ETH_USDT, SOL_USDT)
- [ ] Risk parameters reviewed (2% per trade is aggressive for small accounts)
- [ ] Logs directory writable
- [ ] WebSocket connectivity tested

### Launch
- [ ] Start bot: `python main.py --symbol BTC_USDT --interval 15`
- [ ] Verify logs: "Product info fetched", "Buffer primed", "WebSocket connected"
- [ ] Monitor first entry: Check position opens on Delta Exchange
- [ ] Verify SL/TP: Confirm bracket order shows SL and TP on Delta UI

### Monitoring
- [ ] Check logs every 5 minutes
- [ ] Verify WebSocket stays connected (look for reconnect attempts)
- [ ] Monitor equity curve and drawdown
- [ ] Watch for errors: "API error", "Connection refused", "Order rejected"

### Graceful Shutdown
```bash
Ctrl+C or send SIGTERM
# Bot will:
# 1. Close all open positions
# 2. Cancel pending orders
# 3. Disconnect WebSocket
# 4. Exit gracefully
```

### Emergency Stop
If bot is hanging or stuck:
```bash
pkill -9 python main.py
# Then restart:
python main.py --symbol BTC_USDT --interval 15
# (Bot will auto-close orphaned positions on startup)
```

## Troubleshooting

### WebSocket Connection Failed
```
WARNING: WebSocket disconnected: ... — reconnecting in 5s
```
**Solution**: 
- Check internet connection
- Verify firewall allows WebSocket (port 443)
- Check Delta Exchange status at status.delta.exchange

### Order Placement Fails
```
ERROR: Order placement failed: Delta API 400: ...
```
**Solution**:
- Verify API credentials are correct
- Ensure account has sufficient balance
- Check that product_id is correct for symbol
- Verify margin requirements met (10x leverage)

### Lot Size = 0
```
WARNING: USD notional too small, size = 0
```
**Solution**:
- Increase account equity (currently too small)
- Reduce leverage (risky but enables smaller positions)
- Increase risk_per_trade from 2% to 3-5% (NOT recommended for small accounts)

### Double Position Closes
```
ERROR: no_position_for_reduce_only - ...
```
**Solution**: 
- This is now prevented by `trade.closed` flag
- If still occurs, restart bot (will clean up orphaned positions)

### Slippage on SL
```
🛑 WEBSOCKET SL HIT: BTC_USDT price=42500.00 sl=42500.05
```
**Expected behavior**: Minor slippage (±0.1%) is normal due to market orders

## Performance Expectations

### Entry Latency
- 5-minute candle close detected
- Risk manager validates position
- Bracket order placed within 200-500ms
- **Total: ~200-500ms from signal to exchange**

### SL Trigger Latency
- Exchange-native SL: ~50ms (priority)
- WebSocket backup SL: ~100-200ms
- **Total: 50-200ms from SL price to market order**

### Sample Statistics (BTC_USDT, 10x leverage, 2% risk)

| Metric | Value |
|--------|-------|
| Avg Entry Position | 0.005 BTC (~$215 @ $43k) |
| Avg SL Distance | $215 (0.5% of entry price) |
| Avg TP Distance | $430 (1% of entry price) |
| Avg Hold Time | 2-5 candles |
| Max Leverage | 10x |
| Max Daily Loss | 10% of equity |
| Max Drawdown | 15% of equity |

## Next Steps for Optimization

1. **Multi-symbol**: Run bot on BTC_USDT + ETH_USDT simultaneously
   ```python
   # In main.py: Loop over symbols, create separate ExecutionEngine per symbol
   for symbol in ["BTC_USDT", "ETH_USDT"]:
       engine = ExecutionEngine(...)
       await engine.run_polling()
   ```

2. **Dashboard**: Real-time monitoring UI
   ```bash
   pip install flask
   # Serve: P&L, positions, equity curve
   ```

3. **Trailing Stop**: Advanced SL management
   ```python
   # Update risk.py: Implement trailing SL logic
   # Move SL up by N pips when price increases
   ```

4. **Backtesting**: Historical strategy validation
   ```bash
   python backtest.py --symbol BTC_USDT --start 2026-01-01 --end 2026-03-31
   ```

---

**Last Updated**: March 31, 2026  
**Version**: 2.0 (Production)
