from datetime import timedelta

from approvals.approvals import (ListNotifier, create, decide, escalate, is_approved,
                                 ladder_due_h, post_pending)
from common.clock import iso, parse
from tests.conftest import T0


def at(monkeypatch, hours):
    monkeypatch.setenv("GOV_OPS_NOW", iso(parse(T0) + timedelta(hours=hours)))


def test_silence_is_never_approval(db, monkeypatch):
    aid = create(db, "operator", 2, "Raise envelope")
    n = ListNotifier()
    assert post_pending(db, n) == 1 and post_pending(db, n) == 0     # notified once
    at(monkeypatch, 25)
    assert escalate(db, n) == (1, 0)                                  # reminder
    assert not is_approved(db, aid)
    at(monkeypatch, 48)
    assert escalate(db, n) == (0, 1)                                  # expired
    assert db.scalar("SELECT status FROM approvals WHERE id=?", (aid,)) == "expired"
    assert not is_approved(db, aid)
    assert not decide(db, aid, "approved", "late-human")               # no resurrection
    assert db.scalar("SELECT COUNT(*) FROM events WHERE action='approval_expired'") == 1


def test_sticky_never_expires_and_alternates(db, monkeypatch):
    monkeypatch.setenv("APPROVERS", "approver-a,approver-b")
    aid = create(db, "revenue", 2, "Router go-live", sticky=True)
    n = ListNotifier()
    for hours in (24, 48, 60, 72, 84, 200):
        at(monkeypatch, hours)
        escalate(db, n)
    assert db.scalar("SELECT status FROM approvals WHERE id=?", (aid,)) == "pending"
    cc = [m.split("cc ")[1].split(" ")[0] for m in n.sent if "STICKY" in m]
    assert cc[:4] == ["approver-b", "approver-a", "approver-b", "approver-a"]


def test_ladder_schedule():
    assert [ladder_due_h(i) for i in range(6)] == [24, 48, 60, 72, 84, 96]


def test_decide_and_reject(db):
    a = create(db, "seo", 2, "Disavow")
    b = create(db, "seo", 2, "Template change")
    assert decide(db, a, "approved", "approver-a") and is_approved(db, a)
    assert decide(db, b, "rejected", "approver-b") and not is_approved(db, b)
    assert not decide(db, a, "rejected", "approver-b")                 # already decided
