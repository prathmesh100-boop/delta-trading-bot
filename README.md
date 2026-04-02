# Delta Exchange Algorithmic Trading System v5

## Architecture — BRACKET-FIRST

```
Signal → place_bracket_order() ──→ Exchange holds SL + TP orders
                                       │
                         Price hits SL ├──→ Exchange fires instantly ✅
                         Price hits TP └──→ Exchange fires instantly ✅

Bot WebSocket: only updates trailing SL via edit_bracket_order()
```

**Key change from v4:** SL and TP are placed on the exchange, not monitored by software. The exchange fires them in microseconds. The bot only improves them over time (trailing / breakeven).

---

## Files

| File | Role |
|------|------|
| `api.py` | REST + WebSocket client. `place_bracket_order()` sends `bracket_stop_loss_price` + `bracket_take_profit_price` per Delta docs. |
| `execution.py` | Bracket-first engine. Entry → bracket order → done. Trailing SL pushed via `edit_bracket_order()`. |
| `risk.py` | Position sizing, drawdown halt, daily loss limit, trailing/breakeven stops (in-memory). |
| `strategy.py` | EMA Crossover, Bollinger Mean Reversion, Smart Money (MTF). |
| `regime.py` | ADX + BB-width regime detector (trend/range/volatile). |
| `backtest.py` | Bar-by-bar backtester with fee/slippage, optimiser, Sharpe metric. |
| `dashboard.py` | FastAPI dashboard with SSE live equity chart. |
| `notifier.py` | Telegram alerts. |
| `main.py` | CLI entry point: trade / backtest / optimize. |

---

## Setup

```bash
pip install -r requirements.txt

# Create .env
cat > .env << 'EOF'
DELTA_API_KEY=your_key_here
DELTA_API_SECRET=your_secret_here
TELEGRAM_BOT_TOKEN=optional
TELEGRAM_CHAT_ID=optional
DASHBOARD_TOKEN=secret123
EOF
```

---

## Usage

```bash
# Live trading
python main.py trade --strategy smart_money --symbol ETH_USDT --capital 500 --leverage 5

# Backtest
python main.py backtest --strategy ema_crossover --symbol BTCUSD --capital 10000

# Optimise
python main.py optimize --strategy smart_money --symbol ETH_USDT

# Dashboard (separate terminal)
python dashboard.py
# Open: http://localhost:8000/dashboard?token=secret123
```

---

## Bracket Order API Reference

```
POST /v2/orders
{
  "product_id": 27,
  "side": "buy",
  "order_type": "market_order",
  "size": 5,
  "bracket_stop_loss_price": "56000",
  "bracket_take_profit_price": "64000",
  "bracket_stop_trigger_method": "last_traded_price"
}
```

Exchange attaches SL and TP to the position. They fire instantly on trigger — no bot involved.

To update trailing SL:
```
PUT /v2/orders/bracket
{
  "id": <entry_order_id>,
  "product_id": 27,
  "bracket_stop_loss_price": "57500",   ← improved SL
  "bracket_stop_trigger_method": "last_traded_price"
}
```

---

## Risk Controls

- **1% risk per trade** (configurable via `--risk-per-trade`)
- **5% daily loss limit** → bot halts for the day
- **15% drawdown limit** → bot halts permanently
- **Max 3 open trades** simultaneously
- **Breakeven**: SL moves to entry when price moves 0.2% in favour
- **Profit-lock trailing**: trails at 0.2% from peak when profit > 0.4%
