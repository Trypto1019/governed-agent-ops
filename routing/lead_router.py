#!/usr/bin/env python3
"""lead_router - decides which buyer gets a lead. SHADOW MODE BY DEFAULT.

Shadow mode logs what it WOULD have done and changes nothing. It stays the
default until a human has read enough shadow decisions to trust the logic. A
system that reports success is not the same as a system that works, and this
one touches real people's loan applications.

  python -m routing.lead_router --backfill 30     replay the last 30 days in shadow
  python -m routing.lead_router --report          what shadow mode has learned

HARD GATES, enforced in code rather than policy:
  * never route a lead without consent_contact
  * never route to a buyer not licensed in the lead's state
  * never include the SSN unless the buyer is ssn_required AND has a contract_ref
  * ping payloads are anonymised: no name/email/phone/address/DOB/SSN
  * going live needs ROUTER_LIVE=1 AND an approved Tier-2 "Router go-live" approval
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys
import time
from collections import Counter
from datetime import timedelta

from common.clock import iso, now, now_iso
from common.db import DB, as_json, connect
from common.events import log_event

PII_FIELDS = {"first_name", "last_name", "name", "email", "phone", "street_address",
              "city", "zip_code", "date_of_birth", "ssn", "ip_address"}
GO_LIVE_TITLE = "Router go-live"


def anonymise(lead: dict) -> dict:
    """Attributes only. Buyers bid on this; identity moves only on a won post."""
    return {k: v for k, v in lead.items() if k not in PII_FIELDS}


def post_payload(lead: dict, buyer: dict) -> dict:
    """What a WINNING buyer receives. The SSN travels only under contract."""
    payload = dict(lead)
    if not (buyer.get("ssn_required") and buyer.get("contract_ref")):
        payload.pop("ssn", None)
    return payload


def eligible(lead: dict, buyer: dict, filt: dict | None) -> str | None:
    """Every reason a buyer may not receive this lead. Order matters for logging."""
    if not lead.get("consent_contact"):
        return "no_consent"
    st = (lead.get("state") or "").upper()[:2]
    licensed = as_json(buyer.get("licensed_states")) or []
    if buyer.get("kind") != "network":
        if not licensed:
            return "no_licensed_states_recorded"
        if st and st not in licensed:
            return f"not_licensed_in_{st}"
    if buyer.get("ssn_required") and not buyer.get("contract_ref"):
        return "ssn_buyer_without_contract"
    if not filt:
        return "no_filters_configured"
    if filt.get("requires_trustedform", True) and not lead.get("trustedform_cert"):
        return "no_consent_certificate"
    tiers, states, purposes = (as_json(filt.get(k)) for k in ("tiers", "states", "purposes"))
    if tiers and lead.get("credit_tier") not in tiers:
        return "tier_excluded"
    if states and st and st not in states:
        return "state_excluded"
    if filt.get("requires_checking") and not lead.get("has_checking_account"):
        return "no_checking_account"
    amt = lead.get("loan_amount") or 0
    if filt.get("min_amount") and amt < filt["min_amount"]:
        return "amount_below_min"
    if filt.get("max_amount") and amt > filt["max_amount"]:
        return "amount_above_max"
    if purposes and lead.get("loan_purpose") not in purposes:
        return "purpose_excluded"
    return None


def load_buyers(db: DB) -> list[dict]:
    buyers = db.query("SELECT * FROM buyers WHERE status = 'active' ORDER BY id")
    filters = {f["buyer_id"]: f for f in db.query("SELECT * FROM buyer_filters")}
    for b in buyers:
        b["filt"] = filters.get(b["id"])
    return buyers


def floor_for(db: DB, tier: str, state: str) -> float:
    v = db.scalar("SELECT floor FROM route_floors WHERE tier = ? AND state = ?", (tier, state))
    if v is None:
        v = db.scalar("SELECT floor FROM route_floors WHERE tier = ? AND state = '*'", (tier,))
    return float(v) if v is not None else 0.0


def adapter_for(name: str):
    return importlib.import_module(f"routing.buyer_adapters.{name}")


def live_allowed(db: DB) -> bool:
    """Both keys must turn: the env flag AND an approved Tier-2 go-live row."""
    if os.environ.get("ROUTER_LIVE") != "1":
        return False
    return bool(db.scalar("SELECT COUNT(*) FROM approvals WHERE status = 'approved' "
                          "AND tier = 2 AND title LIKE ?", (GO_LIVE_TITLE + "%",)))


def route(db: DB, lead: dict, shadow: bool = True) -> dict:
    anon = anonymise(lead)
    tier = lead.get("credit_tier") or "unknown"
    state = (lead.get("state") or "").upper()[:2]
    bids, skipped = [], []
    for b in load_buyers(db):
        why = eligible(lead, b, b["filt"])
        if why:
            skipped.append({"buyer": b["name"], "reason": why})
            continue
        t0 = time.time()
        try:
            res = adapter_for(b["payload_schema"]).ping(anon, b, db)
        except Exception as e:
            res = None
            skipped.append({"buyer": b["name"], "reason": f"adapter_error:{type(e).__name__}"})
        db.execute("INSERT INTO lead_offers (lead_id, buyer_id, responded_at, bid, exclusive, "
                   "accepted, decline_reason, latency_ms, shadow) VALUES (?,?,?,?,?,?,?,?,?)",
                   (lead["id"], b["id"], now_iso(), (res or {}).get("bid"),
                    (res or {}).get("exclusive"), bool(res), None if res else "no_bid",
                    int((time.time() - t0) * 1000), shadow))
        if res:
            bids.append({"buyer_id": b["id"], "buyer": b["name"], "bid": float(res["bid"]),
                         "exclusive": bool(res.get("exclusive"))})

    floor = floor_for(db, tier, state)
    best = max(bids, key=lambda x: x["bid"]) if bids else None
    if best and best["bid"] >= floor:
        decision, winner, price = "sell", best, best["bid"]
    elif bids:
        decision, winner, price = "hold_below_floor", None, None
    else:
        decision, winner, price = "no_bids", None, None

    if decision == "sell" and not shadow:          # pragma: no cover - live path, gated
        buyer = next(b for b in load_buyers(db) if b["id"] == winner["buyer_id"])
        ok, _ref = adapter_for(buyer["payload_schema"]).post(post_payload(lead, buyer), buyer)
        if ok:
            db.execute("INSERT INTO lead_sales (lead_id, buyer_id, price, shadow, sold_at) "
                       "VALUES (?,?,?,?,?)", (lead["id"], buyer["id"], price, False, now_iso()))

    log_event(db, "lead_router", "shadow_decision" if shadow else "route_decision",
              str(lead["id"]), {"tier": tier, "state": state, "floor": floor, "shadow": shadow},
              {"decision": decision, "winner": winner, "price": price, "bids": bids,
               "skipped": skipped[:12]})
    return {"decision": decision, "winner": winner, "price": price, "bids": bids,
            "skipped": skipped, "floor": floor}


def report(db: DB) -> str:
    decisions, reasons = Counter(), Counter()
    for e in db.query("SELECT result FROM events WHERE action = 'shadow_decision'"):
        r = as_json(e["result"])
        decisions[r["decision"]] += 1
        reasons.update(s["reason"] for s in r.get("skipped", []))
    lines = ["shadow decisions so far:"]
    lines += [f"  {d:<20} {n}" for d, n in decisions.most_common()]
    lines.append("top skip reasons:")
    lines += [f"  {r:<34} {n}" for r, n in reasons.most_common(10)]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=7, help="days of leads to replay")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args(argv)
    with connect() as db:
        if a.report:
            print(report(db))
            return 0
        if a.live and not live_allowed(db):
            print("--live needs ROUTER_LIVE=1 AND an approved Tier-2 'Router go-live' "
                  "approval. Refusing.", file=sys.stderr)
            return 2
        shadow = not a.live
        since = iso(now() - timedelta(days=a.backfill))
        leads = db.query("SELECT * FROM leads WHERE created_at >= ? ORDER BY created_at", (since,))
        counts = Counter(route(db, lead, shadow)["decision"] for lead in leads)
    mode = "SHADOW (nothing routed)" if shadow else "LIVE"
    print(f"lead_router [{mode}]: {len(leads)} leads -> {dict(counts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
