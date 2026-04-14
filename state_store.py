"""
state_store.py - durable local state for execution recovery
"""

import json
import logging
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from risk import TradeRecord

logger = logging.getLogger(__name__)


class StateStore:
    def __init__(self, root: Optional[Path] = None):
        self.root = root or (Path.cwd() / ".bot_state")
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for_symbol(self, symbol: str) -> Path:
        safe_symbol = symbol.replace("/", "_").replace("\\", "_")
        return self.root / f"{safe_symbol}.json"

    def save_trade(self, trade: TradeRecord) -> None:
        payload = asdict(trade)
        payload["entry_time"] = trade.entry_time.isoformat()
        payload["exit_time"] = trade.exit_time.isoformat() if trade.exit_time else None
        payload["updated_at"] = datetime.utcnow().isoformat()
        path = self._path_for_symbol(trade.symbol)
        temp_path = path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temp_path.replace(path)

    def load_trade(self, symbol: str) -> Optional[TradeRecord]:
        path = self._path_for_symbol(symbol)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("State file unreadable for %s: %s", symbol, exc)
            corrupt_path = path.with_suffix(".corrupt")
            try:
                path.replace(corrupt_path)
            except OSError:
                pass
            return None
        payload.pop("updated_at", None)
        payload["entry_time"] = datetime.fromisoformat(payload["entry_time"])
        if payload.get("exit_time"):
            payload["exit_time"] = datetime.fromisoformat(payload["exit_time"])
        return TradeRecord(**payload)

    def clear_trade(self, symbol: str) -> None:
        path = self._path_for_symbol(symbol)
        if path.exists():
            path.unlink()

