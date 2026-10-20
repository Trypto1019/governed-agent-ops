"""Generic JSON ping-post over HTTP. Most lead networks speak some version of this.

`transport(url, body) -> dict` is injectable so tests never touch the network.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Callable

Transport = Callable[[str, dict, float], dict]


def http_transport(url: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def ping(lead_anon: dict, buyer: dict, db=None, transport: Transport = http_transport) -> dict | None:
    if not buyer.get("endpoint_ping"):
        return None
    try:
        d = transport(buyer["endpoint_ping"], {"lead": lead_anon}, 3.0)
    except Exception:
        return None
    if not d.get("accepted"):
        return None
    return {"bid": float(d.get("price", 0)), "exclusive": bool(d.get("exclusive")),
            "ttl_s": int(d.get("ttl", 300))}


def post(payload: dict, buyer: dict, transport: Transport = http_transport) -> tuple[bool, str | None]:
    if not buyer.get("endpoint_post"):
        return False, None
    try:
        d = transport(buyer["endpoint_post"], {"lead": payload}, 10.0)
    except Exception:
        return False, None
    return bool(d.get("accepted")), d.get("lead_id")
