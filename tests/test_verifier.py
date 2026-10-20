import os
import time
from datetime import date

from common.events import log_event
from verifier import verify as V


def ctx(db, routes=None):
    return V.Ctx(db, V.MockFetcher(routes))


def test_sql_fresh_proves_effect_not_execution(db):
    c = {"sql": "SELECT MAX(ts) FROM events WHERE action='x'", "max_age_h": 1}
    assert V.run_sql_fresh(c, ctx(db))[0] is False          # job "ran", nothing written
    log_event(db, "a", "x")
    assert V.run_sql_fresh(c, ctx(db))[0] is True


def test_sql_count_bounds(db):
    c = {"sql": "SELECT COUNT(*) FROM events", "min": 1, "max": 2}
    assert not V.run_sql_count(c, ctx(db))[0]
    log_event(db, "a", "x")
    assert V.run_sql_count(c, ctx(db))[0]


def test_file_fresh(tmp_path, db, monkeypatch):
    monkeypatch.delenv("GOV_OPS_NOW")
    p = tmp_path / "f.txt"
    assert V.run_file_fresh({"path": str(p), "max_age_h": 1}, ctx(db))[1] == "missing"
    p.write_text("x")
    assert V.run_file_fresh({"path": str(p), "max_age_h": 1}, ctx(db))[0]
    old = time.time() - 3 * 3600
    os.utime(p, (old, old))
    assert not V.run_file_fresh({"path": str(p), "max_age_h": 1}, ctx(db))[0]


def test_http_and_json_field(db):
    c = {"url": "/prequal/", "must_contain": 'id="prequal-root"', "must_not_contain": 'href="#"'}
    assert V.run_http(c, ctx(db))[0]
    broken = {"/prequal/": (200, '<div id="prequal-root"></div><a href="#">x</a>')}
    assert not V.run_http(c, ctx(db, broken))[0]
    assert not V.run_http({"url": "/missing"}, ctx(db))[0]
    j = {"url": "/api/health", "expect": {"status": "ok"}}
    assert V.run_json_field(j, ctx(db))[0]
    assert not V.run_json_field(j, ctx(db, {"/api/health": (200, '{"status":"degraded"}')}))[0]


def test_shell_expect(db):
    assert V.run_shell({"cmd": "echo OK", "expect": "OK"}, ctx(db))[0]
    assert not V.run_shell({"cmd": "echo DRIFT", "expect": "OK"}, ctx(db))[0]
    assert not V.run_shell({"cmd": "exit 3"}, ctx(db))[0]


def test_soft_until_is_dated():
    c = {"soft_until": "2026-10-05"}
    assert V.is_soft(c, date(2026, 10, 5))
    assert not V.is_soft(c, date(2026, 10, 6))              # expires on its own


def test_run_all_writes_rows_and_counts_hard_failures(db):
    checks = [{"id": "a", "job": "j", "kind": "sql_count", "sql": "SELECT 1", "min": 1},
              {"id": "b", "job": "j", "kind": "sql_count", "sql": "SELECT 0", "min": 1},
              {"id": "c", "job": "j", "kind": "sql_count", "sql": "SELECT 0", "min": 1,
               "soft": True},
              {"id": "d", "job": "j", "kind": "sql_count", "sql": "SELECT nope"}]
    results, hard = V.run_all(db, checks, V.MockFetcher())
    assert hard == 2                                          # b fails, d raises; c is soft
    assert db.scalar("SELECT COUNT(*) FROM verifier_results") == 4
    assert "FAIL" in V.report(results, hard)


def test_unknown_outcome_check(db):
    ok, detail = V.verdict_for(db, "does.not.exist")
    assert not ok and "not found" in detail


def test_router_invariants(seeded):
    from verifier.rchecks import router_ev_never_fabricated as ev
    from verifier.rchecks import router_shadow_until_approved as sh
    assert sh.check() == "OK"
    seeded.execute("INSERT INTO lead_sales (lead_id, buyer_id, price, shadow, sold_at) "
                   "VALUES (1, 1, 10, ?, '2026-10-01')", (False,))
    seeded.commit()
    assert sh.check().startswith("LIVE_WITHOUT_APPROVAL")
    seeded.execute("INSERT INTO lead_offers (lead_id, buyer_id, responded_at, bid, accepted, shadow) "
                   "VALUES (3, 2, '2026-10-01', 12.5, 1, 1)")     # fair-tier bid, no revenue
    seeded.commit()
    assert ev.check() == "FABRICATED_EV=fair"
