"""The `events` table is the system's ledger: what was CAUSED, not what ran."""
from __future__ import annotations

from typing import Any

from common.clock import now_iso
from common.db import DB, dumps


def log_event(db: DB, agent: str, action: str, target: str | None = None,
              params: dict | None = None, result: dict | None = None,
              cost_usd: float | None = None) -> int:
    event_id = db.insert(
        "INSERT INTO events (ts, agent, action, target, params, result, cost_usd) "
        "VALUES (?,?,?,?,?,?,?) RETURNING id",
        (now_iso(), agent, action, target, dumps(params or {}), dumps(result or {}), cost_usd))
    db.commit()
    return event_id


def count_events(db: DB, action: str | None = None) -> int:
    if action:
        return int(db.scalar("SELECT COUNT(*) FROM events WHERE action = ?", (action,)))
    return int(db.scalar("SELECT COUNT(*) FROM events"))


def recent(db: DB, limit: int = 20) -> list[dict[str, Any]]:
    return db.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
