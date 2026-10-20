#!/usr/bin/env python3
"""approvals.py - the human-in-the-loop queue. The system NEVER proceeds on silence.

Agents act within their authority tier. Anything above it becomes a row here,
and the requesting work waits for an explicit `approved`. No answer is a "no".

  * Non-sticky items: one reminder at 24h, then EXPIRE at 48h. Expired items are
    logged and never executed.
  * Sticky items (a legal, financial or deadline clock is running) never
    expire. They re-escalate on a 24h -> 24h -> 12h -> 12h ladder (then every
    12h), ALTERNATING between approvers so one unavailable person cannot stall
    the system.

  python -m approvals.approvals --post       notify new pending items
  python -m approvals.approvals --escalate   run the reminder/expiry ladder
  python -m approvals.approvals --decide <id> approve|reject --by <name>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from typing import Protocol

from common.clock import hours_since, now_iso
from common.db import DB, connect
from common.events import log_event

LADDER_H = [24, 24, 12, 12]
NON_STICKY_REMINDER_H = 24
NON_STICKY_EXPIRY_H = 48


def approvers() -> list[str]:
    """Approver handles come from config, never from code."""
    raw = os.environ.get("APPROVERS", "approver-a,approver-b")
    return [a.strip() for a in raw.split(",") if a.strip()]


# ------------------------------------------------------------------ notifiers
class Notifier(Protocol):
    def send(self, text: str) -> None: ...


class ConsoleNotifier:
    def send(self, text: str) -> None:
        print(f"  [notify] {text}")


class WebhookNotifier:
    """Posts to a chat webhook whose URL comes from the environment only."""

    def __init__(self, url: str):
        self.url = url

    def send(self, text: str) -> None:
        try:
            urllib.request.urlopen(urllib.request.Request(
                self.url, data=json.dumps({"text": text}).encode(),
                headers={"Content-Type": "application/json"}), timeout=15)
        except Exception as e:
            print(f"  notify failed: {type(e).__name__}")


class ListNotifier:
    """Collects messages; used by tests and the demo."""

    def __init__(self):
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)


def default_notifier() -> Notifier:
    url = os.environ.get("CHAT_WEBHOOK_URL")
    return WebhookNotifier(url) if url else ConsoleNotifier()


# ------------------------------------------------------------------ queue ops
def create(db: DB, agent: str, tier: int, title: str, body: str = "",
           sticky: bool = False) -> int:
    aid = db.insert("INSERT INTO approvals (agent, tier, title, body, sticky, status, created) "
                    "VALUES (?,?,?,?,?,'pending',?) RETURNING id",
                    (agent, tier, title, body, sticky, now_iso()))
    log_event(db, agent, "approval_requested", str(aid),
              {"tier": tier, "sticky": sticky}, {"title": title})
    return aid


def post_pending(db: DB, notifier: Notifier) -> int:
    rows = db.query("SELECT * FROM approvals WHERE status = 'pending' AND notified = ? "
                    "ORDER BY id", (False,))
    who = approvers()[0]
    for a in rows:
        notifier.send(
            f"Approval #{a['id']} (Tier {a['tier']}{', STICKY' if a['sticky'] else ''}) "
            f"from {a['agent']}: {a['title']}. Reply `approve {a['id']}` or "
            f"`reject {a['id']}`. cc {who}"
            + (" -- sticky: this will keep escalating and will NOT expire" if a["sticky"] else ""))
        db.execute("UPDATE approvals SET notified = ? WHERE id = ?", (True, a["id"]))
    db.commit()
    return len(rows)


def ladder_due_h(escalations: int) -> float:
    """Cumulative age at which escalation number `escalations + 1` is due."""
    steps = LADDER_H + [LADDER_H[-1]] * max(0, escalations + 1 - len(LADDER_H))
    return float(sum(steps[:escalations + 1]))


def escalate(db: DB, notifier: Notifier) -> tuple[int, int]:
    """Re-escalate sticky items on the ladder; remind then expire non-sticky ones."""
    names = approvers()
    escalated = expired = 0
    for a in db.query("SELECT * FROM approvals WHERE status = 'pending'"):
        age_h, esc = hours_since(a["created"]), int(a["escalations"])
        if not a["sticky"]:
            if age_h >= NON_STICKY_EXPIRY_H:
                db.execute("UPDATE approvals SET status = 'expired' WHERE id = ?", (a["id"],))
                log_event(db, "approvals", "approval_expired", str(a["id"]), {},
                          {"title": a["title"], "age_h": round(age_h, 1),
                           "note": "never executed - the system does not proceed on silence"})
                notifier.send(f"Approval #{a['id']} EXPIRED after {age_h:.0f}h: "
                              f"{a['title']}. Not executed.")
                expired += 1
            elif age_h >= NON_STICKY_REMINDER_H and esc == 0:
                db.execute("UPDATE approvals SET escalations = 1 WHERE id = ?", (a["id"],))
                notifier.send(f"Reminder: approval #{a['id']} ({age_h:.0f}h old): {a['title']}")
                escalated += 1
            continue
        if age_h >= ladder_due_h(esc):
            who = names[(esc + 1) % len(names)]
            db.execute("UPDATE approvals SET escalations = escalations + 1 WHERE id = ?",
                       (a["id"],))
            log_event(db, "approvals", "approval_escalated", str(a["id"]),
                      {"escalation": esc + 1, "to": who}, {"age_h": round(age_h, 1)})
            notifier.send(f"STICKY approval #{a['id']} still open after {age_h:.0f}h: "
                          f"{a['title']} -- cc {who} (alternating approver)")
            escalated += 1
    db.commit()
    return escalated, expired


def decide(db: DB, aid: int, decision: str, by: str, notifier: Notifier | None = None) -> bool:
    if decision not in ("approved", "rejected"):
        raise ValueError("decision must be 'approved' or 'rejected'")
    row = db.one("SELECT title FROM approvals WHERE id = ? AND status = 'pending'", (aid,))
    if row is None:
        return False                        # already decided or expired: no resurrection
    db.execute("UPDATE approvals SET status = ?, decided_by = ?, decided_at = ? WHERE id = ?",
               (decision, by, now_iso(), aid))
    log_event(db, "approvals", "approval_decided", str(aid), {"by": by},
              {"decision": decision, "title": row["title"]})
    if notifier:
        notifier.send(f"Approval #{aid} {decision} by {by}: {row['title']}")
    return True


def is_approved(db: DB, aid: int) -> bool:
    """The only question an executor may ask. Pending, expired and rejected are all 'no'."""
    return db.scalar("SELECT status FROM approvals WHERE id = ?", (aid,)) == "approved"


def summary(db: DB) -> dict[str, int]:
    return {r["status"]: int(r["n"]) for r in
            db.query("SELECT status, COUNT(*) AS n FROM approvals GROUP BY status")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--post", action="store_true")
    ap.add_argument("--escalate", action="store_true")
    ap.add_argument("--decide", type=int)
    ap.add_argument("decision", nargs="?", choices=["approve", "reject"])
    ap.add_argument("--by", default="cli")
    a = ap.parse_args(argv)
    notifier = default_notifier()
    with connect() as db:
        if a.decide:
            ok = decide(db, a.decide, "approved" if a.decision == "approve" else "rejected",
                        a.by, notifier)
            print(f"  approval {a.decide}: {'applied' if ok else 'not pending'}")
        if a.post:
            print(f"  posted {post_pending(db, notifier)} pending item(s)")
        if a.escalate:
            e, x = escalate(db, notifier)
            print(f"  escalated {e}, expired {x}")
        print("  queue: " + (", ".join(f"{k}={v}" for k, v in summary(db).items()) or "empty"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
