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
    except Exception:
        pass
