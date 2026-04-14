from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return asdict(value)
    return str(value)


class AuditStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_time TEXT NOT NULL,
                    category TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    symbol TEXT,
                    severity TEXT,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_events_time ON audit_events(event_time DESC);
                CREATE INDEX IF NOT EXISTS idx_audit_events_category ON audit_events(category, event_time DESC);

                CREATE TABLE IF NOT EXISTS trade_audit (
                    trade_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    status TEXT NOT NULL,
                    side TEXT,
                    entry_time TEXT,
                    exit_time TEXT,
                    entry_price REAL,
                    exit_price REAL,
                    stop_loss REAL,
                    take_profit REAL,
                    size INTEGER,
                    contract_value REAL,
                    notional_usd REAL,
                    pnl REAL,
                    reason TEXT,
                    setup_type TEXT,
                    entry_grade TEXT,
                    quality_score REAL,
                    regime TEXT,
                    htf TEXT,
                    rsi REAL,
                    adx REAL,
                    raw_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_trade_audit_symbol ON trade_audit(symbol, updated_at DESC);

                CREATE TABLE IF NOT EXISTS runtime_state (
                    namespace TEXT NOT NULL,
                    state_key TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(namespace, state_key)
                );

                CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_time TEXT NOT NULL,
                    equity REAL NOT NULL,
                    capital REAL NOT NULL,
                    peak_equity REAL NOT NULL,
                    daily_start_equity REAL NOT NULL,
                    drawdown_pct REAL NOT NULL,
                    daily_loss_pct REAL NOT NULL,
                    open_positions INTEGER NOT NULL,
                    open_notional_usd REAL NOT NULL,
                    open_risk_usd REAL NOT NULL,
                    kill_switch INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_time ON portfolio_snapshots(snapshot_time DESC);
                """
            )

    def _dump(self, payload: Any) -> str:
        return json.dumps(payload, default=_json_default, sort_keys=True)

    def record_event(
        self,
        category: str,
        event_type: str,
        payload: Dict[str, Any],
        *,
        symbol: Optional[str] = None,
        severity: str = "info",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_events (event_time, category, event_type, symbol, severity, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (_utc_now(), category, event_type, symbol, severity, self._dump(payload)),
            )

    def upsert_trade(self, trade: Any, status: str) -> None:
        trade_dict = asdict(trade) if is_dataclass(trade) else dict(trade)
        pnl = trade_dict.get("net_pnl")
        if pnl is None and trade_dict.get("exit_price") is not None and trade_dict.get("entry_price") is not None:
            mult = 1 if trade_dict.get("side") == "long" else -1
            pnl = mult * (trade_dict["exit_price"] - trade_dict["entry_price"]) * trade_dict.get("filled_size", trade_dict.get("size", 0)) * trade_dict.get("contract_value", 0.0)
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO trade_audit (
                    trade_id, symbol, status, side, entry_time, exit_time, entry_price, exit_price,
                    stop_loss, take_profit, size, contract_value, notional_usd, pnl, reason,
                    setup_type, entry_grade, quality_score, regime, htf, rsi, adx, raw_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_id) DO UPDATE SET
                    status=excluded.status, side=excluded.side, entry_time=excluded.entry_time, exit_time=excluded.exit_time,
                    entry_price=excluded.entry_price, exit_price=excluded.exit_price, stop_loss=excluded.stop_loss,
                    take_profit=excluded.take_profit, size=excluded.size, contract_value=excluded.contract_value,
                    notional_usd=excluded.notional_usd, pnl=excluded.pnl, reason=excluded.reason,
                    setup_type=excluded.setup_type, entry_grade=excluded.entry_grade, quality_score=excluded.quality_score,
                    regime=excluded.regime, htf=excluded.htf, rsi=excluded.rsi, adx=excluded.adx,
                    raw_json=excluded.raw_json, updated_at=excluded.updated_at
                """,
                (
                    trade_dict.get("id"),
                    trade_dict.get("symbol"),
                    status,
                    trade_dict.get("side"),
                    _json_default(trade_dict.get("entry_time")) if trade_dict.get("entry_time") else None,
                    _json_default(trade_dict.get("exit_time")) if trade_dict.get("exit_time") else None,
                    trade_dict.get("entry_price"),
                    trade_dict.get("exit_price"),
                    trade_dict.get("stop_loss"),
                    trade_dict.get("take_profit"),
                    trade_dict.get("size"),
                    trade_dict.get("contract_value"),
                    trade_dict.get("notional_usd"),
                    pnl,
                    trade_dict.get("reason"),
                    trade_dict.get("setup_type"),
                    trade_dict.get("entry_grade"),
                    trade_dict.get("entry_quality_score"),
                    trade_dict.get("regime_at_entry"),
                    trade_dict.get("htf_at_entry"),
                    trade_dict.get("rsi_at_entry"),
                    trade_dict.get("adx_at_entry"),
                    self._dump(trade_dict),
                    now,
                ),
            )

    def set_runtime_state(self, namespace: str, state_key: str, value: Dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runtime_state (namespace, state_key, value_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(namespace, state_key) DO UPDATE SET
                    value_json=excluded.value_json,
                    updated_at=excluded.updated_at
                """,
                (namespace, state_key, self._dump(value), _utc_now()),
            )

    def delete_runtime_state(self, namespace: str, state_key: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM runtime_state WHERE namespace = ? AND state_key = ?",
                (namespace, state_key),
            )

    def list_runtime_states(self, namespace: Optional[str] = None) -> List[Dict[str, Any]]:
        query = "SELECT namespace, state_key, value_json, updated_at FROM runtime_state"
        params: Iterable[Any] = ()
        if namespace:
            query += " WHERE namespace = ?"
            params = (namespace,)
        query += " ORDER BY updated_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "namespace": row["namespace"],
                "state_key": row["state_key"],
                "value": json.loads(row["value_json"]),
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def record_portfolio_snapshot(self, snapshot: Dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO portfolio_snapshots (
                    snapshot_time, equity, capital, peak_equity, daily_start_equity, drawdown_pct,
                    daily_loss_pct, open_positions, open_notional_usd, open_risk_usd, kill_switch, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.get("timestamp", _utc_now()),
                    snapshot.get("current_equity", 0.0),
                    snapshot.get("current_capital", 0.0),
                    snapshot.get("peak_equity", 0.0),
                    snapshot.get("daily_start_equity", 0.0),
                    snapshot.get("drawdown_pct", 0.0),
                    snapshot.get("daily_loss_pct", 0.0),
                    snapshot.get("open_positions", 0),
                    snapshot.get("open_notional_usd", 0.0),
                    snapshot.get("open_risk_usd", 0.0),
                    1 if snapshot.get("kill_switch") else 0,
                    self._dump(snapshot),
                ),
            )

    def latest_portfolio_snapshot(self) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM portfolio_snapshots ORDER BY snapshot_time DESC LIMIT 1"
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def recent_events(self, limit: int = 200, category: Optional[str] = None) -> List[Dict[str, Any]]:
        query = "SELECT event_time, category, event_type, symbol, severity, payload_json FROM audit_events"
        params: List[Any] = []
        if category:
            query += " WHERE category = ?"
            params.append(category)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "event_time": row["event_time"],
                "category": row["category"],
                "event_type": row["event_type"],
                "symbol": row["symbol"],
                "severity": row["severity"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def recent_trades(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT raw_json, status, pnl, updated_at FROM trade_audit ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        items: List[Dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["raw_json"])
            payload["status"] = row["status"]
            payload["pnl"] = row["pnl"]
            payload["updated_at"] = row["updated_at"]
            items.append(payload)
        return items

