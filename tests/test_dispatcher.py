from datetime import timedelta

from common.clock import iso, parse
from dispatcher.dispatcher import drain, select_batch, tick
from tests.conftest import T0


def job_status(db, jid):
    return db.one("SELECT enabled, last_status, consecutive_failures FROM jobs WHERE id = ?", (jid,))


def add_job(db, jid, role, cmd, check, schedule="5m"):
    db.execute("INSERT INTO jobs (id, role, kind, command, schedule, outcome_check, enabled) "
               "VALUES (?,?,?,?,?,?,?)", (jid, role, "script", cmd, schedule, check, True))
    db.commit()


def test_concurrency_one_per_role_three_total():
    due = [{"id": f"j{i}", "role": r} for i, r in enumerate("aabcde")]
    batch = select_batch(due)
    assert [j["id"] for j in batch] == ["j0", "j2", "j3"]


def test_zero_events_is_suspicious_not_ok(db):
    add_job(db, "seo.scan_pages", "seo", "{python} -m demo.jobs silent", "seo.page_scan_fresh")
    [out] = tick(quiet=True)
    assert out.status == "suspicious" and out.rc == 0
    assert job_status(db, "seo.scan_pages")["consecutive_failures"] == 1


def test_two_failures_auto_disable(db, monkeypatch):
    add_job(db, "nurture.sequence_send", "nurture", "{python} -m demo.jobs broken",
            "nurture.sends_fresh")
    assert tick(quiet=True)[0].status == "failed"
    assert job_status(db, "nurture.sequence_send")["enabled"] == 1
    monkeypatch.setenv("GOV_OPS_NOW", iso(parse(T0) + timedelta(minutes=10)))
    out = tick(quiet=True)[0]
    assert out.status == "failed" and out.suspended
    assert job_status(db, "nurture.sequence_send")["enabled"] == 0
    assert db.scalar("SELECT COUNT(*) FROM events WHERE action='suspend_job'") == 1
    # disabled jobs are never picked again
    monkeypatch.setenv("GOV_OPS_NOW", iso(parse(T0) + timedelta(hours=1)))
    assert tick(quiet=True) == []


def test_full_drain_statuses(seeded):
    from budget.provision_keys import MockKeyProvider
    keys = MockKeyProvider(seeded)
    keys.create("acme-finance", 20.0)
    keys.create("acme-operator", 40.0)
    out = {o.job: o for o in drain(quiet=True)}
    assert out["sensor.health"].status == "ok"
    assert out["router.shadow"].status == "ok"
    assert out["finance.daily_rollup"].status == "ok"
    assert out["operator.weekly_digest"].status == "ok"
    assert out["seo.scan_pages"].status == "suspicious"
    assert out["nurture.sequence_send"].status == "failed"
    # no provisioned key -> the role does not run (exit 3), recorded as a failure
    assert out["paid.bid_review"].status == "failed" and out["paid.bid_review"].rc == 3
    assert seeded.scalar("SELECT COUNT(*) FROM events WHERE action='job_outcome_verified'") == 4


def test_budget_exhausted_is_its_own_status(seeded):
    from budget.provision_keys import MockKeyProvider
    MockKeyProvider(seeded).create("acme-paid", 0.0)
    seeded.execute("UPDATE jobs SET enabled = ? WHERE id <> 'paid.bid_review'", (False,))
    seeded.commit()
    [out] = tick(quiet=True)
    assert out.status == "budget_exhausted" and out.rc == 4
    assert job_status(seeded, "paid.bid_review")["consecutive_failures"] == 0


def test_dry_run_changes_nothing(db):
    add_job(db, "x.job", "x", "{python} -m demo.jobs broken", None)
    [out] = tick(dry=True, quiet=True)
    assert out.status == "dry-run"
    assert job_status(db, "x.job")["consecutive_failures"] == 0
