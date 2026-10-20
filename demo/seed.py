"""Seed a fresh database with SYNTHETIC data for the offline demo.

Every person, phone, email and ID here is fictional: phones use the reserved
555-01xx range, emails use example.com, and SSN-shaped values use the 900
area number, which is never issued.
"""
from __future__ import annotations

import json
from datetime import timedelta

from common.clock import iso, now
from common.db import DB

PEOPLE = [
    ("Alex", "Rivera", "TX", "excellent", 8000, True, True),
    ("Jordan", "Lee", "OH", "good", 5000, True, True),
    ("Casey", "Morgan", "GA", "fair", 3000, True, True),
    ("Riley", "Chen", "AZ", "poor", 1500, True, True),
    ("Taylor", "Brooks", "TX", "good", 12000, False, True),     # no consent
    ("Morgan", "Patel", "CA", "excellent", 20000, True, True),
    ("Jamie", "Fox", "OH", "fair", 2500, True, False),          # no consent certificate
    ("Drew", "Kim", "GA", "excellent", 6000, True, True),
]

JOBS = [
    # id, role, kind, command, schedule, outcome_check
    ("finance.daily_rollup", "finance", "agent",
     "{python} -m runtime.agent_loop finance 'daily rollup' --mock", "24h", "finance.rollup_fresh"),
    ("operator.weekly_digest", "operator", "agent",
     "{python} -m runtime.agent_loop operator 'weekly digest' --mock", "7d",
     "operator.digest_written"),
    ("sensor.health", "sensor", "sensor", "{python} -m sensors.health_sensor", "1h",
     "sensor.health_rows_fresh"),
    ("router.shadow", "lead_router", "script", "{python} -m routing.lead_router --backfill 7", "1h",
     "router.shadow_decisions_flowing"),
    ("seo.scan_pages", "seo", "script", "{python} -m demo.jobs silent", "24h",
     "seo.page_scan_fresh"),
    ("nurture.sequence_send", "nurture", "script", "{python} -m demo.jobs broken", "5m",
     "nurture.sends_fresh"),
    ("paid.bid_review", "paid", "agent",
     "{python} -m runtime.agent_loop paid 'review bids' --mock", "24h", None),
]

BUDGETS = {"operator": 40.0, "compliance": 30.0, "finance": 20.0, "paid": 0.0}


def seed(db: DB) -> None:
    db.init_schema()
    t0 = now()
    month = t0.date().replace(day=1).isoformat()

    for i, (first, last, st, tier, amount, consent, cert) in enumerate(PEOPLE, start=1):
        db.execute(
            "INSERT INTO leads (created_at, first_name, last_name, email, phone, ssn, state, "
            "credit_tier, loan_amount, loan_purpose, has_checking_account, consent_contact, "
            "trustedform_cert) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (iso(t0 - timedelta(hours=6 * i)), first, last,
             f"{first.lower()}.{last.lower()}@example.com", f"+1-555-01{i:02d}",
             f"900-00-{i:04d}", st, tier, amount, "debt_consolidation", True, consent,
             f"cert-demo-{i:03d}" if cert else None))

    buyers = [
        # name, kind, licensed, adapter, ssn_required, contract_ref, filters
        ("Northstar Lead Network", "network", [], "mock_network", False, None,
         {"tiers": ["excellent", "good", "fair"]}),
        ("Acme Marketplace", "lender", ["TX", "OH", "GA", "AZ"], "ev_marketplace", True,
         "contract-demo-001", {}),
        ("Offer Wall", "offer_wall", ["TX", "OH", "GA", "AZ", "CA"], "affiliate_redirect",
         False, None, {}),
        ("Unsigned SSN Buyer", "lender", ["TX", "OH"], "mock_network", True, None, {}),
        ("Westfield Loans", "lender", ["CA"], "mock_network", False, "contract-demo-002",
         {"min_amount": 10000}),
    ]
    for name, kind, licensed, adapter, ssn_req, contract, filt in buyers:
        bid = db.insert("INSERT INTO buyers (name, kind, licensed_states, payload_schema, "
                        "ssn_required, contract_ref) VALUES (?,?,?,?,?,?) RETURNING id",
                        (name, kind, json.dumps(licensed), adapter, ssn_req, contract))
        db.execute("INSERT INTO buyer_filters (buyer_id, tiers, min_amount, requires_trustedform) "
                   "VALUES (?,?,?,?)", (bid, json.dumps(filt.get("tiers")) if "tiers" in filt
                                        else None, filt.get("min_amount"), True))
    for tier, floor in (("excellent", 20), ("good", 15), ("fair", 10), ("poor", 5)):
        db.execute("INSERT INTO route_floors (tier, state, floor) VALUES (?, '*', ?)", (tier, floor))

    # 30 days of sensor history. Marketplace revenue exists only for two tiers,
    # so the EV bidder must decline the others rather than guess.
    for d in range(1, 31):
        day = (t0.date() - timedelta(days=d)).isoformat()
        rows = [("leads", "", 8 + d % 5), ("llm_cost_usd", "", 0.4),
                ("revenue_usd", "marketplace:excellent", 90.0),
                ("marketplace_submitted", "excellent", 3),
                ("revenue_usd", "marketplace:good", 40.0),
                ("marketplace_submitted", "good", 4)]
        for metric, dim, value in rows:
            db.execute("INSERT INTO metrics_daily (day, metric, dimension, value, source, "
                       "pulled_at) VALUES (?,?,?,?,?,?)",
                       (day, metric, dim, value, "seed", iso(t0 - timedelta(days=d))))

    for agent, env in BUDGETS.items():
        db.execute("INSERT INTO budgets (month, agent, category, envelope_usd) VALUES (?,?,?,?)",
                   (month, agent, "llm", env))

    # A free-text row that a "regressed" view exposes; the PII filter must catch it.
    db.execute("INSERT INTO feedback (created_at, comment) VALUES (?,?)",
               (iso(t0 - timedelta(hours=3)),
                "Please call me back, my email is jordan.lee@example.com"))

    for jid, role, kind, cmd, sched, check in JOBS:
        db.execute("INSERT INTO jobs (id, role, kind, command, schedule, outcome_check, enabled) "
                   "VALUES (?,?,?,?,?,?,?)", (jid, role, kind, cmd, sched, check, True))
    db.commit()
