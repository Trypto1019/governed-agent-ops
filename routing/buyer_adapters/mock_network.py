"""Offline stand-in for a ping-post lead network: deterministic bids by tier."""
from __future__ import annotations

BIDS = {"excellent": 42.0, "good": 28.0, "fair": 14.0}


def ping(lead_anon: dict, buyer: dict, db=None) -> dict | None:
    bid = BIDS.get(lead_anon.get("credit_tier") or "")
    if bid is None:
        return None                      # this network does not buy poor-tier leads
    return {"bid": bid, "exclusive": False, "ttl_s": 300}


def post(payload: dict, buyer: dict) -> tuple[bool, str | None]:
    return True, f"mock-{buyer['id']}-{payload.get('id')}"
