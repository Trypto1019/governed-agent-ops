from common.db import as_json
from routing import lead_router as R
from routing.buyer_adapters import ev_marketplace, generic_pingpost_json


def lead(db, first="Alex"):
    return db.one("SELECT * FROM leads WHERE first_name = ?", (first,))


def buyer(db, name):
    b = db.one("SELECT * FROM buyers WHERE name = ?", (name,))
    b["filt"] = db.one("SELECT * FROM buyer_filters WHERE buyer_id = ?", (b["id"],))
    return b


def test_anonymised_ping_has_no_identity(seeded):
    anon = R.anonymise(lead(seeded))
    assert not set(anon) & {"first_name", "last_name", "email", "phone", "ssn"}
    assert anon["credit_tier"] == "excellent"


def test_no_consent_never_routed(seeded):
    r = R.route(seeded, lead(seeded, "Taylor"))
    assert r["decision"] == "no_bids"
    assert {s["reason"] for s in r["skipped"]} == {"no_consent"}


def test_unlicensed_state_and_unsigned_ssn_buyer_skipped(seeded):
    r = R.route(seeded, lead(seeded))                     # TX lead
    reasons = {s["buyer"]: s["reason"] for s in r["skipped"]}
    assert reasons["Westfield Loans"] == "not_licensed_in_TX"
    assert reasons["Unsigned SSN Buyer"] == "ssn_buyer_without_contract"


def test_ssn_only_to_contracted_ssn_buyer(seeded):
    l = lead(seeded)
    assert "ssn" in R.post_payload(l, buyer(seeded, "Acme Marketplace"))
    assert "ssn" not in R.post_payload(l, buyer(seeded, "Northstar Lead Network"))
    unsigned = {**buyer(seeded, "Acme Marketplace"), "contract_ref": None}
    assert "ssn" not in R.post_payload(l, unsigned)


def test_ev_bidder_declines_unknown_tier(seeded):
    assert ev_marketplace.expected_value(seeded, "excellent") == 30.0
    assert ev_marketplace.expected_value(seeded, "fair") is None
    assert ev_marketplace.ping({"credit_tier": "poor"}, {}, seeded) is None


def test_shadow_is_default_and_writes_nothing_live(seeded):
    assert R.main(["--backfill", "7"]) == 0
    assert seeded.scalar("SELECT COUNT(*) FROM lead_sales") == 0
    assert seeded.scalar("SELECT COUNT(*) FROM lead_offers WHERE shadow = ?", (False,)) == 0
    events = seeded.query("SELECT result FROM events WHERE action = 'shadow_decision'")
    assert len(events) == 8
    won = [as_json(e["result"]) for e in events if as_json(e["result"])["decision"] == "sell"]
    assert all(w["price"] >= 5 for w in won)


def test_live_needs_flag_and_approval(seeded, monkeypatch):
    assert R.main(["--live"]) == 2
    monkeypatch.setenv("ROUTER_LIVE", "1")
    assert R.main(["--live"]) == 2                         # flag alone is not enough
    from approvals.approvals import create, decide
    aid = create(seeded, "revenue", 2, "Router go-live after shadow review")
    decide(seeded, aid, "approved", "approver-a")
    assert R.live_allowed(seeded)


def test_generic_adapter_uses_injected_transport():
    b = {"endpoint_ping": "https://buyer.example.org/ping"}
    ok = generic_pingpost_json.ping({"credit_tier": "good"}, b,
                                    transport=lambda u, body, t: {"accepted": True, "price": 9})
    assert ok["bid"] == 9.0
    assert generic_pingpost_json.ping({}, b, transport=lambda *a: 1 / 0) is None
