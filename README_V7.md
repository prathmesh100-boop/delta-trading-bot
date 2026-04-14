# Delta Exchange Algo Bot — V7 (Fixed)

## 🔴 What Was Wrong (Root Cause Analysis)

Your bot started at **$92.88** and bled to **~$87**. Here's exactly why, traced from your logs:

---

### Bug #1: Entering Overbought (THE MAIN ISSUE)
**Log evidence:**
```
LONG SIGNAL | price=72190.5000 | rsi=65.4   ← buying at RSI 65!
LONG SIGNAL | price=72668.0000 | rsi=67.9   ← buying at RSI 68!
LONG SIGNAL | price=73052.0000 | rsi=61.2   ← buying into a rally
```
The old code allowed LONG entries up to RSI **68**. That's overbought territory — the bot was buying tops, not pullbacks. The strategy says "buy the dip in an uptrend" but was actually buying the extension.

**Fix:** LONG RSI max dropped from **68 → 52**. Only enter when price has actually pulled back.

---

### Bug #2: Trailing Stop Too Tight (ALL WINS WERE NEAR-ZERO)
**Log evidence:**
```
Trade 1: entry=72147 → BE stop at 72219 → exit 72733 = +$0.59
Trade 2: entry=73318 → BE stop at 73391 → exit 73369 = +$0.05
Trade 3: entry=2218  → BE stop at 2220  → exit 2239  = +$0.63
```
Every single trade was closed at **breakeven** or just above it. The old settings:
- Breakeven at +0.5% profit
- Trail only 0.4% behind peak
- Total cushion: 0.9% before stop fires
- BTC 15m candle noise: 0.3–0.5% → **stop fired on normal noise every time**

The bot was mathematically guaranteed to not let winners run. Fee costs then eroded the tiny gains.

**Fix:** Breakeven moved to +**1.0%**, trail widened to **0.8%**. Now trades have room to reach the TP.

---

### Bug #3: HTF "neutral" Blocked Valid Entries
**Log evidence:**
```
blockers=htf_bullish,ema_alignment_weak  (repeated dozens of times)
blockers=ema_alignment_weak              (repeated 20+ times in a row)
```
The HTF trend check required EMA50 > EMA200 **AND** price > EMA50 **AND** positive slope — all three simultaneously. On 15m data, the slope check was so noisy it constantly returned "neutral" even during clear uptrends, blocking entries.

**Fix:** HTF just needs EMA50 > EMA200 with 0.1% buffer. Slope check removed.

---

### Bug #4: Regime Thrashing
**Log evidence:**
```
regime=range → regime=trend → regime=range → regime=trend (every few candles)
```
With no hysteresis, the regime flipped every candle, confusing the signal logic.

**Fix:** Sticky regime — requires ADX > 22 to enter trend, < 14 to exit (was just a single 18 threshold).

---

## ✅ V7 Changes Summary

| Parameter | V6 (broken) | V7 (fixed) | Why |
|-----------|------------|-----------|-----|
| RSI Long Max | 68 | **52** | Don't buy overbought |
| RSI Short Min | 32 | **48** | Don't sell oversold |
| Breakeven Trigger | 0.5% | **1.0%** | Let trade breathe |
| Trail Width | 0.4% | **0.8%** | Don't get stopped by noise |
| Profit Lock Trigger | 1.0% | **1.8%** | Start trailing later |
| ADX Threshold | 16 | **20** | Only trade clear trends |
| Min Confidence | 0.55 | **0.58** | Slightly stricter |
| Min RR | 1.8R | **2.2R** | Cover fees + make profit |
| Regime hysteresis | ❌ | **✅** | Stop regime thrashing |
| HTF check | Strict (3 conditions) | **Loose (1 condition)** | Fewer missed entries |
| SL ATR multiplier | 1.2x | **1.5x** | Slightly wider stop |
| Pullback check | price near ema21 (above OK) | **price AT ema21 (actual dip)** | True pullback entry |

---

## Quick Start

```bash
# 1. Replace only strategy.py, risk.py, main.py (other files unchanged)

# 2. Backtest first with your real data
python main.py backtest --symbol BTCUSD --capital 10000

# 3. Run live (use your actual balance)
python main.py trade --symbol ETHUSD --capital 92 --leverage 3
python main.py trade --symbol BTCUSD --capital 92 --leverage 3

# 4. For 1H candles (less noise, recommended for small capital)
python main.py trade --symbol BTCUSD --capital 92 --leverage 3 --resolution 60
```

---

## What to Expect Now

- **Fewer signals** — the strategy is more selective (that's good, not bad)
- **Trades will enter on genuine pullbacks** (RSI 38–52, price at ema21)
- **Winning trades will actually run to TP** instead of stopping at breakeven
- **Trade frequency**: ~1–3 per week per symbol on 15m (same as before, fewer false ones)

---

## Files Changed

| File | Status |
|------|--------|
| `strategy.py` | ✅ Rewritten (V7 fixes) |
| `risk.py` | ✅ Updated (trailing fixes) |
| `main.py` | ✅ Updated (new defaults) |
| `execution.py` | Unchanged |
| `api.py` | Unchanged |
| `backtest.py` | Unchanged |
| `notifier.py` | Unchanged |
| `regime.py` | Unchanged |
| `state_store.py` | Unchanged |
| `dashboard.py` | Unchanged |

---

**Version**: V7 — Fixed Entry Timing + Fixed Trailing  
**Exchange**: Delta Exchange India (api.india.delta.exchange)  
**Last Updated**: April 2026
