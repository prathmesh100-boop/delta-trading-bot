# Delta Exchange Trading Bot v6

## Quick Start
```bash
pip install -r requirements.txt

# .env file
DELTA_API_KEY=your_key
DELTA_API_SECRET=your_secret

# Live trade
python main.py trade --strategy smart_money --symbol ETH_USDT --capital 500 --leverage 10

# With AI signal filter + orderbook filter
python main.py trade --strategy ema_crossover --symbol BTC_USDT --capital 500 \
  --leverage 10 --ai-filter --ob-filter 0.05 --confidence 0.6

# Backtest
python main.py backtest --strategy smart_money --symbol BTCUSD --capital 10000

# Optimize
python main.py optimize --strategy ema_crossover --symbol BTCUSD

# Check product info & ticker
python main.py info --symbol BTC_USDT

# Check account status
python main.py status
```

## Strategies
| Name | Description |
|------|-------------|
| `ema_crossover` | EMA 9/21 crossover + RSI filter |
| `smart_money` | ICT concepts: CHoCH + FVG + OB zones |
| `bollinger_mean_reversion` | BB extremes + RSI + volume |
| `breakout` | N-bar high/low breakout + volume |
| `vwap_mean_reversion` | VWAP deviation + RSI |

Add `--ai-filter` to any strategy to enable the multi-factor signal scorer.

## Key v6 Changes (from your uploaded code)
- **All Delta API endpoints used correctly** per official docs PDF
- `get_order_by_id()` — GET /v2/orders/{id}
- `get_order_by_client_id()` — GET /v2/orders/client_order_id/{id}
- `get_order_history()` — GET /v2/orders/history
- `get_fills()` — GET /v2/fills with order_id/product_id filters
- `change_margin_mode()` — POST /v2/users/change_margin_mode
- `get_leverage()` — GET /v2/orders/leverage
- `get_rate_limit_quota()` — GET /v2/users/rate_limit (429 header aware)
- `get_public_trades()` — GET /v2/trades/{symbol}
- `get_position()` — GET /v2/positions (single product)
- `close_all_positions()` — DELETE /v2/positions
- `update_mmp_config()` / `reset_mmp()` — MMP endpoints
- **Private WebSocket channels**: orders, positions, user_trades (exact fill prices)
- **L2OrderBook.imbalance()** — orderbook filter before entry
- **5 strategies** including VWAP + breakout
- **AIFilteredStrategy** — multi-factor confidence scoring
- **`main.py info`** and **`main.py status`** commands
