"""Buyer adapters, one per delivery spec. Each implements:

    ping(lead_anon, buyer, db) -> {"bid": float, "exclusive": bool, "ttl_s": int} | None
    post(payload, buyer)       -> (ok: bool, ref: str | None)

`lead_anon` NEVER contains name, email, phone, address, DOB or SSN. Buyers bid on
attributes; identity moves only on a won post, only to a buyer licensed for the
lead's state, and the SSN only to an `ssn_required` buyer with a contract.
"""
