# Delta Exchange Algorithmic Trading System

Production-grade Python algorithmic trading bot for **Delta Exchange** (crypto derivatives).

---

## ⚠️ Disclaimer
This software is for **educational purposes**. Crypto derivatives trading carries extreme
risk of loss. Past backtest performance is **not** indicative of future results.
Never trade with money you cannot afford to lose. Always paper-trade / testnet first.

---

## Project Structure

```
delta_trader/
├── api.py          # REST + WebSocket client for Delta Exchange
├── strategy.py     # Strategy framework + EMA Crossover + Bollinger Mean Reversion
├── risk.py         # Position sizing, drawdown, daily loss limits, trailing stops
├── backtest.py     # Event-driven backtester + performance metrics + optimiser
├── execution.py    # Real-time execution engine (polling & WS modes)
├── main.py         # CLI entry point
├── requirements.txt
└── README.md
```

---

## Installation

```bash
# 1. Clone / copy the project
cd delta_trader

# 2. Create virtual environment
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt
```

---

## Configuration

Set your Delta Exchange API credentials as environment variables:

```bash
export DELTA_API_KEY="your_api_key_here"
export DELTA_API_SECRET="your_api_secret_here"
```

To use testnet, change the `BASE_URL` in `api.py`:
```python
BASE_URL = "https://testnet-api.delta.exchange"
```

---

## Usage

### 1. Backtest (no API keys needed)

```bash
# Backtest EMA Crossover strategy on synthetic data
python main.py backtest --strategy ema_crossover --symbol BTCUSD --capital 10000

# Backtest Bollinger Mean Reversion
python main.py backtest --strategy bollinger_mean_reversion --symbol BTCUSD

# Backtest on your own OHLCV CSV (columns: open,high,low,close,volume)
python main.py backtest --strategy ema_crossover --data-file my_data.csv
```

### 2. Parameter Optimisation

```bash
python main.py optimize --strategy ema_crossover --symbol BTCUSD
```
This runs grid search on a 70% training set and validates on the remaining 30%.

### 3. Live Trading

```bash
python main.py trade \
  --strategy ema_crossover \
  --symbol BTCUSD \
  --capital 1000 \
  --resolution 15
```

---

## Strategies

### EMA Crossover (Trend Following)
- **Entry**: Fast EMA (9) crosses above/below Slow EMA (21)
- **Filter**: RSI must be in 50–70 (long) or 30–50 (short) — avoids chasing extremes
- **Exit**: ATR-based stop-loss (1.5× ATR), 2:1 risk/reward take-profit
- **Best for**: Trending markets (BTC bull/bear runs)

### Bollinger Mean Reversion
- **Entry**: Price touches lower/upper Bollinger Band + RSI oversold/overbought + volume spike
- **Filter**: ADX < 30 (avoids trading in strong trends where mean reversion fails)
- **Exit**: Reversion to 20-period moving average (middle band)
- **Best for**: Consolidating / ranging markets

---

## Risk Management Features

| Feature | Default | Config Key |
|---|---|---|
| Risk per trade | 1% of capital | `risk_per_trade` |
| Max position size | 5% of capital | `max_position_size_pct` |
| Max open trades | 3 | `max_open_trades` |
| Max drawdown halt | 10% | `max_drawdown_pct` |
| Daily loss limit | 3% | `daily_loss_limit_pct` |
| Trailing stop | 2% from peak | `trailing_stop_pct` |

---

## Performance Metrics (Backtest Output)

- **Win Rate** — % of trades that were profitable
- **Profit Factor** — Gross profit / Gross loss (>1.5 is reasonable)
- **Sharpe Ratio** — Risk-adjusted return (>1.0 is decent, >2.0 is excellent)
- **Sortino Ratio** — Like Sharpe but only penalises downside volatility
- **Max Drawdown** — Largest peak-to-trough equity decline
- **Avg Win / Avg Loss** — Reward:risk in practice

---

## Extending the System

### Add a new strategy:
```python
# In strategy.py
class MyStrategy(BaseStrategy):
    name = "my_strategy"

    def generate_signal(self, df: pd.DataFrame, symbol: str) -> Signal:
        # Your logic here
        return Signal(SignalType.LONG, symbol, df['close'].iloc[-1],
                      stop_loss=..., take_profit=...)

# Register it
STRATEGY_REGISTRY["my_strategy"] = MyStrategy
```

### Change risk settings:
```python
from risk import RiskConfig, RiskManager

config = RiskConfig(
    risk_per_trade=0.005,       # 0.5% per trade (conservative)
    max_drawdown_pct=0.08,      # 8% halt
    daily_loss_limit_pct=0.02,  # 2% daily limit
    leverage=2.0,               # 2× leverage (use carefully)
)
risk_mgr = RiskManager(config, initial_capital=5000)
```

---

## Output Files

| File | Contents |
|---|---|
| `trading_bot.log` | Full execution log |
| `trade_history.csv` | All closed trades with entry/exit/PnL |
| `equity_curve.csv` | Per-bar equity for charting |

---

## Important Notes

1. **No Holy Grail** — These strategies are starting points. Expect 40–55% win rates.
   The edge comes from risk management and discipline, not prediction accuracy.

2. **Parameter sensitivity** — Optimised parameters often overfit. Always validate
   on out-of-sample data and expect degraded live performance.

3. **Market regime** — EMA crossover works in trends; Bollinger works in ranges.
   Neither works well in the wrong regime. A regime filter (e.g. ADX, volatility
   percentile) is a worthwhile enhancement.

4. **Slippage & fees** — At high frequency, fees eat most alpha. The backtest
   simulates 0.05% taker fee + 0.03% slippage per fill.

5. **Testnet first** — Always run on Delta's testnet for at least 2–4 weeks before
   committing real capital.
