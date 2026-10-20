"""One clock for every module.

`GOV_OPS_NOW` (ISO-8601) pins "now" so the demo and tests can time-travel
(e.g. "48 hours later, did the approval expire?") without sleeping. Child
processes launched by the dispatcher inherit it.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone


def now() -> datetime:
    pinned = os.environ.get("GOV_OPS_NOW")
    if pinned:
        return parse(pinned)
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def now_iso() -> str:
    return iso(now())


def parse(value) -> datetime:
    """Accept a datetime (Postgres) or an ISO string (SQLite)."""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def hours_since(value) -> float:
    return (now() - parse(value)).total_seconds() / 3600


def interval(schedule: str) -> timedelta:
    """Minimal interval scheduler: '5m', '1h', '24h', '7d'."""
    unit, n = schedule[-1], int(schedule[:-1])
    delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}.get(unit)
    if delta is None:
        raise ValueError(f"unsupported schedule: {schedule}")
    return delta
