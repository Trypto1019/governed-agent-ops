#!/usr/bin/env python3
"""health_sensor - endpoint status per registered surface into metrics_daily.

Sensors are the other half of "agents are judged on numbers they cannot
write": each role's KPIs come from rows like these, written by code with no
model in it. This one reuses the Verifier's pluggable fetcher, so it is offline
by default (VERIFIER_HTTP=live for real requests).
"""
from __future__ import annotations

import os
import sys

from common.clock import now, now_iso
from common.db import DB, connect
from common.events import log_event
from verifier.verify import default_fetcher, site_base

SOURCE = "health_sensor"
SURFACES = ["/", "/prequal/", "/api/health"]


def upsert(db: DB, day: str, metric: str, dim: str, value: float) -> None:
    db.execute("INSERT INTO metrics_daily (day, metric, dimension, value, source, pulled_at) "
               "VALUES (?,?,?,?,?,?) ON CONFLICT (day, metric, dimension) "
               "DO UPDATE SET value = excluded.value, pulled_at = excluded.pulled_at",
               (day, metric, dim, value, SOURCE, now_iso()))


def pull(db: DB) -> dict:
    fetcher, day, down = default_fetcher(), now().date().isoformat(), []
    for path in SURFACES:
        status, _ = fetcher.get(site_base() + path)
        upsert(db, day, "endpoint_status", path, float(status or 0))
        if status not in (200, 301, 302):
            down.append(f"{path}={status}")
    db.commit()
    agent = os.environ.get("AGENT_ROLE", "sensor")
    log_event(db, agent, "sensor_pull", "metrics_daily", {"surfaces": len(SURFACES)},
              {"rows_written": len(SURFACES), "down": down})
    return {"rows": len(SURFACES), "down": down}


def main() -> int:
    with connect() as db:
        r = pull(db)
    print(f"health_sensor: {r['rows']} rows" + (f", DOWN {r['down']}" if r["down"] else ", all healthy"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
