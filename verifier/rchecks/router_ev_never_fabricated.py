"""An expected-value bidder may not bid on a tier with no revenue history.

A guessed EV makes the router prefer or avoid a buyer for no evidential reason.
Generalises to: every winning bid traces to a real adapter response, never a
default.
"""
from __future__ import annotations

from common.db import connect


def check() -> str:
    with connect() as db:
        rows = db.query(
            "SELECT DISTINCT l.credit_tier AS tier FROM lead_offers o "
            "JOIN leads l ON l.id = o.lead_id JOIN buyers b ON b.id = o.buyer_id "
            "WHERE o.bid > 0 AND b.payload_schema = 'ev_marketplace' "
            "AND l.credit_tier IS NOT NULL AND NOT EXISTS ("
            "  SELECT 1 FROM metrics_daily m WHERE m.metric = 'revenue_usd' "
            "  AND m.dimension = 'marketplace:' || l.credit_tier)")
    bad = sorted(r["tier"] for r in rows)
    return "FABRICATED_EV=" + ",".join(bad) if bad else "OK"


if __name__ == "__main__":
    print(check())
