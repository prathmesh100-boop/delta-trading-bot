from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List

from delta_bot.config import MonitoringConfig
from delta_bot.storage import AuditStore


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RuntimeMonitor:
    def __init__(
        self,
        store: AuditStore,
        *,
        symbol: str,
        config: MonitoringConfig | None = None,
    ) -> None:
        self.store = store
        self.symbol = symbol
        self.config = config or MonitoringConfig()
        self._last_persisted: Dict[str, float] = {}

    def heartbeat(self, component: str, **details: Any) -> None:
        self._persist(
            key=component,
            payload={
                "component": component,
                "symbol": self.symbol,
                "status": "ok",
                "kind": "heartbeat",
                "last_seen_at": _utc_now().isoformat(),
                "details": details,
            },
        )

    def loop_timing(self, component: str, duration_ms: float, **details: Any) -> None:
        status = "degraded" if duration_ms >= self.config.slow_loop_threshold_ms else "ok"
        payload = {
            "component": component,
            "symbol": self.symbol,
            "kind": "loop_timing",
            "status": status,
            "last_seen_at": _utc_now().isoformat(),
            "duration_ms": round(duration_ms, 3),
            "details": details,
        }
        self._persist(key=component, payload=payload, force=status != "ok")
        if status != "ok":
            self.store.record_event(
                "monitoring",
                "slow_loop_detected",
                {"component": component, "duration_ms": round(duration_ms, 3), **details},
                symbol=self.symbol,
                severity="warning",
            )

    def error(self, component: str, message: str, **details: Any) -> None:
        payload = {
            "component": component,
            "symbol": self.symbol,
            "kind": "error",
            "status": "error",
            "last_seen_at": _utc_now().isoformat(),
            "message": message,
            "details": details,
        }
        self._persist(key=component, payload=payload, force=True)
        self.store.record_event(
            "monitoring",
            "component_error",
            {"component": component, "message": message, **details},
            symbol=self.symbol,
            severity="error",
        )

    def _persist(self, *, key: str, payload: Dict[str, Any], force: bool = False) -> None:
        now_ts = _utc_now().timestamp()
        last_ts = self._last_persisted.get(key, 0.0)
        if not force and (now_ts - last_ts) < self.config.persist_interval_seconds:
            return
        self.store.set_runtime_state("monitoring", f"{self.symbol}:{key}", payload)
        self._last_persisted[key] = now_ts


def runtime_health_summary(
    runtime_items: Iterable[Dict[str, Any]],
    *,
    stale_after_seconds: float = 180.0,
    now: datetime | None = None,
) -> Dict[str, Any]:
    now_dt = now or _utc_now()
    components: List[Dict[str, Any]] = []
    stale_count = 0
    degraded_count = 0
    error_count = 0

    for item in runtime_items:
        if item.get("namespace") != "monitoring":
            continue
        value = item.get("value", {})
        last_seen_raw = value.get("last_seen_at") or item.get("updated_at")
        try:
            last_seen = datetime.fromisoformat(last_seen_raw)
        except (TypeError, ValueError):
            last_seen = now_dt
        age_seconds = max(0.0, (now_dt - last_seen).total_seconds())
        status = str(value.get("status") or "unknown")
        stale = age_seconds > stale_after_seconds
        if stale:
            stale_count += 1
        if status == "degraded":
            degraded_count += 1
        if status == "error":
            error_count += 1
        components.append(
            {
                "component": value.get("component") or item.get("state_key"),
                "symbol": value.get("symbol"),
                "kind": value.get("kind"),
                "status": "stale" if stale else status,
                "age_seconds": round(age_seconds, 3),
                "last_seen_at": last_seen.isoformat(),
                "duration_ms": value.get("duration_ms"),
                "message": value.get("message"),
            }
        )

    overall_status = "ok"
    if error_count:
        overall_status = "error"
    elif stale_count or degraded_count:
        overall_status = "degraded"

    return {
        "status": overall_status,
        "component_count": len(components),
        "stale_count": stale_count,
        "degraded_count": degraded_count,
        "error_count": error_count,
        "components": sorted(components, key=lambda item: (item["status"], item["component"] or "")),
    }
