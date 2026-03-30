import os
import requests


def send(msg: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": msg}, timeout=5)
    except Exception:
        pass


def send_trade_alert(trade) -> None:
    """Format and send a trade execution alert. Accepts object or dict-like trade."""
    try:
        sym = getattr(trade, 'symbol', None) or trade.get('symbol') if isinstance(trade, dict) else None
        side = getattr(trade, 'side', None) or (trade.get('side') if isinstance(trade, dict) else None)
        entry = getattr(trade, 'entry_price', None) or (trade.get('entry_price') if isinstance(trade, dict) else None)
        sl = getattr(trade, 'stop_loss', None) or (trade.get('stop_loss') if isinstance(trade, dict) else None)
        tp = getattr(trade, 'take_profit', None) or (trade.get('take_profit') if isinstance(trade, dict) else None)
        size = getattr(trade, 'size', None) or (trade.get('size') if isinstance(trade, dict) else None)

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
    except Exception:
        pass
