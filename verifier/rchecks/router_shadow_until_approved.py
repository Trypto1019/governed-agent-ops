"""The router may only leave shadow mode with an approved Tier-2 trail.

A live router with no approval is the failure this design exists to prevent: a
system acting on real people's loan applications because a flag was flipped.
"""
from __future__ import annotations

from common.db import connect


def check() -> str:
    with connect() as db:
        sales = int(db.scalar("SELECT COUNT(*) FROM lead_sales WHERE shadow = ?", (False,)))
        offers = int(db.scalar("SELECT COUNT(*) FROM lead_offers WHERE shadow = ?", (False,)))
        approved = int(db.scalar(
            "SELECT COUNT(*) FROM approvals WHERE status = 'approved' AND tier = 2 "
            "AND title LIKE 'Router go-live%'"))
    if (sales == 0 and offers == 0) or approved > 0:
        return "OK"
    return f"LIVE_WITHOUT_APPROVAL sales={sales} offers={offers}"


if __name__ == "__main__":
    print(check())
