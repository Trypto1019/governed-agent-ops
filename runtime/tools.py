"""Tools an agent may call. Each is a plain function plus a JSON schema.

`roles/<role>/role.json` lists the subset a role may use; runtime/agent_loop.py
enforces that allow-list. A role cannot call a tool it did not declare, and a
model cannot invent one.
"""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, Callable

from common.clock import now, now_iso
from common.db import DB, dumps
from common.events import log_event

# Views an agent may read. Base tables are absent by design (and, in Postgres,
# revoked by grant as well).
ALLOWED_VIEWS = {"leads_agg", "events", "jobs", "job_runs", "verifier_results",
                 "metrics_daily", "budgets", "approvals", "recent_feedback"}

UNSAFE_WHERE = re.compile(r";|--|/\*|\b(drop|delete|update|insert|alter|grant|copy|attach)\b", re.I)


class ToolContext:
    """What a tool may touch: the DB handle and the calling role."""

    def __init__(self, db: DB, role: str):
        self.db = db
        self.role = role


def query_view(ctx: ToolContext, view_name: str, where: str = "", limit: int = 200):
    """SELECT * from an allow-listed view. No joins, no base tables, no DDL."""
    if view_name not in ALLOWED_VIEWS:
        raise ValueError(f"view '{view_name}' is not allow-listed: {sorted(ALLOWED_VIEWS)}")
    if UNSAFE_WHERE.search(where):
        raise ValueError("unsafe WHERE clause rejected")
    limit = max(1, min(int(limit), 1000))
    sql = f"SELECT * FROM {view_name}" + (f" WHERE {where}" if where.strip() else "")
    return ctx.db.query(sql + f" LIMIT {limit}")


def metrics_read(ctx: ToolContext, metric: str = "", days: int = 28):
    """Read metrics_daily: the numbers a role is judged on, which it cannot write."""
    days = max(1, min(int(days), 400))
    since = (now().date() - timedelta(days=days)).isoformat()
    sql = "SELECT day, metric, dimension, value, source FROM metrics_daily WHERE day >= ?"
    params: list[Any] = [since]
    if metric:
        sql += " AND metric = ?"
        params.append(metric)
    return ctx.db.query(sql + " ORDER BY day DESC, metric LIMIT 2000", params)


def approvals_create(ctx: ToolContext, tier: int, title: str, body: str, sticky: bool = False):
    """Queue an above-authority decision for a human. The system waits."""
    from approvals.approvals import create
    aid = create(ctx.db, ctx.role, int(tier), title, body, sticky=bool(sticky))
    return {"approval_id": aid, "status": "pending"}


def memory_write(ctx: ToolContext, key: str, value: dict):
    """Persist one memory entry for this role."""
    ctx.db.execute("DELETE FROM memory WHERE role = ? AND key = ?", (ctx.role, key))
    ctx.db.execute("INSERT INTO memory (role, key, value, updated) VALUES (?,?,?,?)",
                   (ctx.role, key, dumps(value), now_iso()))
    ctx.db.commit()
    return {"written": key}


def _schema(name: str, description: str, props: dict, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props, "required": required or []}}}


REGISTRY: dict[str, tuple[Callable[..., Any], dict]] = {
    "query_view": (query_view, _schema(
        "query_view", "SELECT from a PII-free, allow-listed view.",
        {"view_name": {"type": "string", "enum": sorted(ALLOWED_VIEWS)},
         "where": {"type": "string"}, "limit": {"type": "integer"}}, ["view_name"])),
    "metrics_read": (metrics_read, _schema(
        "metrics_read", "Read metrics_daily.",
        {"metric": {"type": "string"}, "days": {"type": "integer"}})),
    "approvals_create": (approvals_create, _schema(
        "approvals_create", "Queue a Tier-2/3 decision for a human.",
        {"tier": {"type": "integer"}, "title": {"type": "string"},
         "body": {"type": "string"}, "sticky": {"type": "boolean"}},
        ["tier", "title", "body"])),
    "memory_write": (memory_write, _schema(
        "memory_write", "Persist a memory entry.",
        {"key": {"type": "string"}, "value": {"type": "object"}}, ["key", "value"])),
}


def record_block(ctx: ToolContext, tool: str, kind: str) -> None:
    log_event(ctx.db, ctx.role, "compliance_block", tool,
              {"tool": tool, "pattern": kind},
              {"blocked": True, "reason": "PII in tool result"})
