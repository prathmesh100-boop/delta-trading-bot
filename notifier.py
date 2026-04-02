import os
import requests


def send(msg: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": chat_id, "text": msg}, timeout=5)
        if resp.status_code != 200:
            print(f"[NOTIFIER] Telegram API returned {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"[NOTIFIER] Failed to send message: {e}")


def send_trade_alert(trade) -> None:
    """Format and send a trade execution alert. Accepts object or dict-like trade."""
    try:
        if isinstance(trade, dict):
            sym   = trade.get('symbol')
            side  = trade.get('side')
            entry = trade.get('entry_price')
            sl    = trade.get('stop_loss')
            tp    = trade.get('take_profit')
            size  = trade.get('size')
        else:
            sym   = getattr(trade, 'symbol', None)
            side  = getattr(trade, 'side', None)
            entry = getattr(trade, 'entry_price', None)
            sl    = getattr(trade, 'stop_loss', None)
            tp    = getattr(trade, 'take_profit', None)
            size  = getattr(trade, 'size', None)

        msg = (
            f"📊 TRADE EXECUTED\n\n"
            f"Symbol: {sym}\n"
            f"Side: {str(side).upper()}\n"
            f"Entry: {entry}\n"
            f"SL: {sl}\n"
            f"TP: {tp}\n"
            f"Size: {size}\n\n"
            f"🚀 Good luck!"
        )
        send(msg)
    except Exception as e:
        print(f"[NOTIFIER] send_trade_alert failed: {e}")


def send_exit_alert(symbol: str, side: str, entry_price: float, exit_price: float, pnl: float, reason: str) -> None:
    """Format and send a trade exit alert (SL/TP hit or manual close)."""
    try:
        # Determine emoji based on reason
        if "stop_loss" in reason.lower():
            emoji = "🛑"
            reason_text = "STOP LOSS HIT"
        elif "take_profit" in reason.lower():
            emoji = "✅"
            reason_text = "TAKE PROFIT HIT"
        else:
            emoji = "📍"
            reason_text = "CLOSED"

        # Determine PnL emoji
        pnl_emoji = "✅" if pnl >= 0 else "❌"

        msg = (
            f"{emoji} TRADE {reason_text}\n\n"
            f"Symbol: {symbol}\n"
            f"Side: {side.upper()}\n"
            f"Entry: {entry_price:.4f}\n"
            f"Exit: {exit_price:.4f}\n"
            f"{pnl_emoji} PnL: {pnl:+.4f} USDT\n\n"
            f"Reason: {reason}"
        )
        send(msg)
    except Exception as e:
        print(f"[NOTIFIER] send_exit_alert failed: {e}")
