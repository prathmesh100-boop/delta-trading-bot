"""
notifier.py — Telegram Trade Alerts
"""

import os
import requests


def send(msg: str) -> None:
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": chat_id, "text": msg}, timeout=5)
        if resp.status_code != 200:
            print(f"[NOTIFIER] {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"[NOTIFIER] Failed: {e}")


def send_trade_alert(trade) -> None:
    try:
        if isinstance(trade, dict):
            sym, side, entry = trade.get("symbol"), trade.get("side"), trade.get("entry_price")
            sl, tp, size     = trade.get("stop_loss"), trade.get("take_profit"), trade.get("size")
        else:
            sym, side, entry = trade.symbol, trade.side, trade.entry_price
            sl, tp, size     = trade.stop_loss, trade.take_profit, trade.size

        msg = (
            f"🔲 TRADE ENTRY\n\n"
            f"Symbol : {sym}\n"
            f"Side   : {str(side).upper()}\n"
            f"Entry  : {entry:.4f}\n"
            f"SL     : {sl:.4f}\n"
            f"TP     : {f'{tp:.4f}' if tp else 'NONE'}\n"
            f"Size   : {size} lots\n"
            f"Risk   : {abs((entry - sl) / entry * 100):.2f}%\n\n"
            f"🚀 Trade active!"
        )
        send(msg)
    except Exception as e:
        print(f"[NOTIFIER] trade_alert failed: {e}")


def send_exit_alert(symbol, side, entry_price, exit_price, pnl, reason) -> None:
    try:
        if "stop_loss" in reason:
            emoji, reason_text = "🛑", "STOP LOSS HIT"
        elif "take_profit" in reason:
            emoji, reason_text = "🎯", "TAKE PROFIT HIT"
        else:
            emoji, reason_text = "📍", "CLOSED"

        pnl_emoji = "✅" if pnl >= 0 else "❌"
        rr = abs((exit_price - entry_price) / abs(entry_price - 0.00001)) if entry_price else 0

        msg = (
            f"{emoji} TRADE {reason_text}\n\n"
            f"Symbol : {symbol}\n"
            f"Side   : {side.upper()}\n"
            f"Entry  : {entry_price:.4f}\n"
            f"Exit   : {exit_price:.4f}\n"
            f"{pnl_emoji} PnL : {pnl:+.4f} USDT\n\n"
            f"Reason : {reason}"
        )
        send(msg)
    except Exception as e:
        print(f"[NOTIFIER] exit_alert failed: {e}")


def send_status(msg: str) -> None:
    send(f"ℹ️ BOT STATUS\n\n{msg}")


def send_telegram(msg: str) -> None:
    send(msg)
