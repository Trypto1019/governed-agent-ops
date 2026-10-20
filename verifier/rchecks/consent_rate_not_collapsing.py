"""A form regression that silently drops consent makes leads unsellable with no
error anywhere: the checkboxes are required and validated, and the payload
drops them. Compare the 7-day consent rate with the 30-day rate.
"""
from __future__ import annotations

from datetime import timedelta

from common.clock import iso, now
from common.db import connect


def _rate(db, days: int) -> float | None:
    since = iso(now() - timedelta(days=days))
    row = db.one("SELECT COUNT(*) AS n, SUM(CASE WHEN consent_contact THEN 1 ELSE 0 END) AS c "
                 "FROM leads WHERE created_at > ?", (since,))
    if not row or not row["n"]:
        return None
    return 100.0 * float(row["c"] or 0) / float(row["n"])


def check() -> str:
    with connect() as db:
        r7, r30 = _rate(db, 7), _rate(db, 30)
    if r7 is None or r30 is None:
        return "OK"                      # no traffic is a different check's problem
    return "OK" if r7 >= r30 - 10 else f"COLLAPSE r7={r7:.0f}% r30={r30:.0f}%"


if __name__ == "__main__":
    print(check())
