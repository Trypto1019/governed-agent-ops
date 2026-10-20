"""A lending marketplace as one buyer among several, not the destination.

Its bid is its EXPECTED VALUE per submission for the lead's tier:
    trailing-60-day revenue / trailing-60-day submissions, for that tier.

Returns None, not zero, when that is unknown. A guessed EV would make the router
systematically prefer or avoid this buyer for no evidential reason. The
Verifier check `router.ev_never_fabricated` asserts this invariant.
"""
from __future__ import annotations

from datetime import timedelta

from common.clock import now


def expected_value(db, tier: str) -> float | None:
    since = (now().date() - timedelta(days=60)).isoformat()

    def total(metric: str, dim: str) -> float:
        v = db.scalar("SELECT COALESCE(SUM(value), 0) FROM metrics_daily "
                      "WHERE metric = ? AND dimension = ? AND day >= ?", (metric, dim, since))
        return float(v or 0)

    rev = total("revenue_usd", f"marketplace:{tier}")
    subs = total("marketplace_submitted", tier)
    if rev <= 0 or subs <= 0:
        return None                      # unknown, deliberately not 0
    return round(rev / subs, 4)


def ping(lead_anon: dict, buyer: dict, db=None) -> dict | None:
    ev = expected_value(db, lead_anon.get("credit_tier") or "unknown")
    if ev is None:
        return None
    return {"bid": ev, "exclusive": True, "ttl_s": 600, "ev_basis": "trailing_60d"}


def post(payload: dict, buyer: dict) -> tuple[bool, str | None]:
    # In production the marketplace submission happens in the funnel's own API;
    # the router never double-submits, it only decides whether this buyer wins.
    return True, "handled_by_funnel"
