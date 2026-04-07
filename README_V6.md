# Delta Exchange Algo Bot — Institutional Confluence Strategy

## What Makes This Bot Different

This bot uses the **same logic professional prop traders and hedge funds use**. It's not a simple indicator bot. It's a multi-layer confluence system that only trades when **everything aligns**.

---

## The Strategy: Multi-Timeframe Confluence

### Entry Conditions (LONG — requires ALL to be true):
| # | Condition | Why It Matters |
|---|-----------|----------------|
| 1 | **HTF Trend = Bull** (EMA50 > EMA200, price above both) | Only trade with institutional flow |
| 2 | **Price near EMA21** (within 1.5%) | Structural pullback = tight stop + room to run |
| 3 | **RSI 35–60** | Not overbought, momentum reset |
| 4 | **MACD histogram rising** | Momentum inflection — bears giving up |
| 5 | **ADX > 20** | Market is actually trending, not choppy |
| 6 | **+DI > -DI** | Bulls in directional control |
| 7 | **ATR > ATR_MA** | Volatility is alive (filters dead markets) |
| 8 | **Funding rate < 0.1%** | Not a crowded long trade |
| 9 | **Last swing low intact** | Structure support for SL placement |

**SHORT**: Mirror conditions. Bear trend, price near EMA21 resistance, RSI 40-65, MACD falling, ADX > 20, -DI > +DI.

### In Ranging Markets (ADX < 22):
- Bollinger Band mean reversion
- Buy below lower band (RSI < 35), sell above upper band (RSI > 65)
- TP = middle band (natural mean reversion target)

### SL/TP:
- **SL**: Below last swing low (long) / above last swing high (short) + 1.5 ATR buffer
- **TP**: 2.5R from entry (minimum 2.5:1 risk-reward)
- **Trailing**: 3 stages — initial → breakeven at +0.5% → profit-lock trailing at +1%

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Create .env file (copy from template)
cp .env.template .env
# Edit .env with your real API keys

# 3. Run info check first
python main.py info --symbol ETH_USDT

# 4. Check account status
python main.py status

# 5. Run backtest first!
python main.py backtest --symbol ETH_USDT --capital 10000

# 6. Start live bot (ETH, 15m candles, $500 capital, 5x leverage)
python main.py trade --symbol ETH_USDT --capital 500 --leverage 5

# 7. For BTC with larger capital
python main.py trade --symbol BTC_USDT --capital 1000 --leverage 3 --resolution 60
```

---

## All Trade Commands

```bash
# Minimal (uses defaults)
python main.py trade --symbol ETH_USDT --capital 200

# Full configuration
python main.py trade \
  --symbol ETH_USDT \
  --capital 500 \
  --leverage 5 \
  --resolution 15 \
  --risk-per-trade 0.01 \
  --max-drawdown 0.15 \
  --daily-loss-limit 0.08 \
  --min-confidence 0.55 \
  --adx-threshold 20 \
  --tp-rr 2.5
```

---

## Risk Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--risk-per-trade` | 0.01 (1%) | % of equity risked per trade |
| `--leverage` | 5 | Exchange leverage (3-10 recommended) |
| `--max-drawdown` | 0.15 (15%) | Bot halts if drawdown exceeds this |
| `--daily-loss-limit` | 0.08 (8%) | Bot pauses for the day if exceeded |
| `--min-confidence` | 0.55 | Minimum signal quality score |
| `--adx-threshold` | 20 | Minimum ADX for trend trades |
| `--tp-rr` | 2.5 | Take profit risk-reward ratio |

---

## Capital Requirements

| Symbol | Min Capital | Recommended | Leverage |
|--------|------------|-------------|---------|
| ETH_USDT | $30 | $200+ | 5-10x |
| BTC_USDT | $30 | $500+ | 3-5x |
| SOL_USDT | $20 | $100+ | 5-10x |

---

## Safety Features

### 3-Layer SL Protection
```
Layer 1: Exchange bracket SL (50ms trigger — primary)
Layer 2: WebSocket SL check on every tick (100ms backup)
Layer 3: Emergency recovery on restart (orphan position closer)
```

### Circuit Breakers
- **15% max drawdown**: All trading halted, must restart manually
- **8% daily loss**: Trading paused until midnight UTC
- **Single position limit**: One trade at a time (safer for small accounts)
- **Cooldown**: No re-entry for 1 candle after exit (prevents overtrading)

### Anti-Tilt Features
- **Confidence gate**: Low-quality signals rejected automatically
- **Funding rate filter**: Won't enter crowded trades
- **Volatility filter**: Won't trade dead markets (low ATR)
- **Volume filter**: Won't trade on fake volume

---

## File Structure

```
delta_bot/
├── main.py          ← Entry point, CLI commands
├── api.py           ← Delta Exchange REST + WebSocket client
├── strategy.py      ← Confluence strategy (the edge)
├── execution.py     ← Execution engine (bracket orders + WS monitoring)
├── risk.py          ← Risk manager (sizing, trailing stops, circuit breakers)
├── backtest.py      ← Bar-by-bar backtester
├── notifier.py      ← Telegram alerts
├── requirements.txt
├── .env             ← Your API keys (never commit this!)
└── decisions.csv    ← Auto-generated trade log
```

---

## Reading the Logs

```
✅ LONG SIGNAL  → Strategy found valid entry
📤 PLACING BRACKET → Sending order to exchange
🔲 BRACKET PLACED → Order confirmed, SL/TP live on exchange
⚡ BREAKEVEN SL → Stop moved to entry (risk = 0)
📈 TRAIL SL → Stop trailing behind price
🎯 WS TP HIT → Take profit triggered
🛑 WS SL HIT → Stop loss triggered
🏁 CLOSED → Trade closed, PnL logged
🔴 CIRCUIT BREAKER → Trading halted (drawdown exceeded)
```

---

## Telegram Alerts

Set in `.env`:
```
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

You'll receive alerts for:
- Every trade entry (symbol, side, price, SL, TP)
- Every exit (reason, P&L)
- Daily summary

---

## Important Notes

1. **Always backtest first** before going live
2. **Start small** — $50-100 to verify it works, then scale
3. **The bot trades infrequently** — Confluence means it waits for the right setup. Don't expect 5 trades per day.
4. **15-minute candles = 1-4 trades per week** on average. 1-hour = 1-2 per week.
5. **Never use all your capital** — Keep 50%+ as safety reserve
6. **Crypto is risky** — Past backtest results don't guarantee future profits

---

## Troubleshooting

| Problem | Solution |
|---------|---------|
| "Symbol not found" | Run `python main.py info --symbol X` to check exact symbol name |
| "Lot size < 1" | Your capital is too small for the position sizing formula |
| "API key error" | Check `.env` file has correct keys |
| "WebSocket disconnects" | Normal — auto-reconnects. Check your internet |
| No trades for days | Market is sideways/volatile — bot waits for clear trend |
| "Circuit breaker active" | You hit max drawdown. Stop, review, restart manually |

---

**Version**: 3.0 — Institutional Confluence Strategy  
**Exchange**: Delta Exchange India (api.india.delta.exchange)  
**Last Updated**: April 2026