"""The offer wall as a real buyer with a price, not a consolation prize.

Bid = measured payout per impression for the tier. Until payouts are measured
it cannot bid, so it returns None.
"""
from __future__ import annotations


def ping(lead_anon: dict, buyer: dict, db=None) -> dict | None:
    return None                          # no measured payout yet -> cannot bid


def post(payload: dict, buyer: dict) -> tuple[bool, str | None]:
    return True, "offer_wall"
