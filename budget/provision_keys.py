#!/usr/bin/env python3
"""provision_keys.py - one inference key per role, spend limit = its envelope.

Enforcement is AT THE KEY, not in software. When a role exhausts its envelope
the gateway (OpenRouter) returns HTTP 402 and the run is recorded as
`budget_exhausted`. There is no soft limit, and a role cannot under-report its
own spend: metering agents by self-report is unenforceable.

The management key that creates these keys is held by a human and never placed
on the agent host. The agent runtime only ever sees its own per-role key.

Provisioning is also the last model gate: a role whose role.json names a model
without a passing, in-date (<90 days) eval scorecard does not get a key.

Usage:
  python -m budget.provision_keys --list
  python -m budget.provision_keys --sync [--dry-run]
  python -m budget.provision_keys --sync --mock      # offline, DB-backed fake gateway
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path
from typing import Callable, Protocol

from common.clock import iso, now
from common.db import DB, connect
from common.llm import BudgetExhausted

ROLES_DIR = Path(__file__).resolve().parent.parent / "roles"
KEY_PREFIX = "acme-"
QUALIFICATION_DAYS = 90


class KeyProvider(Protocol):
    def list_keys(self) -> dict[str, dict]: ...
    def create(self, name: str, limit_usd: float) -> str | None: ...
    def update(self, name: str, limit_usd: float) -> None: ...


# ------------------------------------------------------------------ real gateway
class OpenRouterKeyProvider:
    API = "https://openrouter.ai/api/v1/keys"

    def __init__(self, management_key: str):
        self.key = management_key

    def _call(self, method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(
            url, method=method, data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def list_keys(self) -> dict[str, dict]:
        st, d = self._call("GET", self.API)
        if st != 200:
            raise RuntimeError(f"cannot list keys: HTTP {st}")
        return {k.get("name"): k for k in d.get("data") or []}

    def create(self, name: str, limit_usd: float) -> str | None:
        st, d = self._call("POST", self.API, {"name": name, "limit": limit_usd,
                                              "limit_reset": "monthly"})
        return d.get("key") if st in (200, 201) else None

    def update(self, name: str, limit_usd: float) -> None:
        h = self.list_keys()[name]["hash"]
        self._call("PATCH", f"{self.API}/{h}", {"limit": limit_usd, "limit_reset": "monthly"})


# ------------------------------------------------------------------ mock gateway
class MockKeyProvider:
    """DB-backed fake gateway. `charger(name)` returns the 402-raising meter."""

    def __init__(self, db: DB):
        self.db = db

    def list_keys(self) -> dict[str, dict]:
        return {r["name"]: {"limit": r["limit_usd"], "usage": r["usage_usd"],
                            "disabled": bool(r["disabled"])}
                for r in self.db.query("SELECT * FROM provider_keys")}

    def create(self, name: str, limit_usd: float) -> str | None:
        self.db.execute("INSERT INTO provider_keys (name, limit_usd) VALUES (?,?)",
                        (name, limit_usd))
        self.db.commit()
        return f"mock-key-for-{name}"

    def update(self, name: str, limit_usd: float) -> None:
        self.db.execute("UPDATE provider_keys SET limit_usd = ? WHERE name = ?",
                        (limit_usd, name))
        self.db.commit()

    def charger(self, name: str) -> Callable[[float], None]:
        def charge(cost: float) -> None:
            row = self.db.one("SELECT * FROM provider_keys WHERE name = ?", (name,))
            if row is None or row["disabled"]:
                raise BudgetExhausted(f"no active key '{name}'")
            if float(row["usage_usd"]) + cost > float(row["limit_usd"]):
                raise BudgetExhausted(f"key '{name}' would exceed its "
                                      f"${float(row['limit_usd']):.2f} limit (HTTP 402)")
            self.db.execute("UPDATE provider_keys SET usage_usd = usage_usd + ? WHERE name = ?",
                            (cost, name))
            self.db.commit()
        return charge


# ------------------------------------------------------------------ policy
def envelopes(db: DB, month: str | None = None) -> dict[str, float]:
    """This month's LLM envelope per role, from `budgets`."""
    month = month or now().date().replace(day=1).isoformat()
    rows = db.query("SELECT agent, SUM(envelope_usd) AS total FROM budgets "
                    "WHERE month = ? AND category = 'llm' GROUP BY agent", (month,))
    return {r["agent"]: float(r["total"]) for r in rows}


def passing_models(db: DB) -> set[tuple[str, str]]:
    """(model, role) pairs with a passing scorecard in the last 90 days."""
    since = iso(now() - timedelta(days=QUALIFICATION_DAYS))
    rows = db.query("SELECT model, role FROM model_evals WHERE pass = ? AND ran_at > ?",
                    (True, since))
    return {(r["model"], r["role"]) for r in rows}


def unqualified_reason(role: str, qualified: set[tuple[str, str]],
                       roles_dir: Path = ROLES_DIR) -> str | None:
    p = roles_dir / role / "role.json"
    if not p.exists():
        return "no role.json"
    cfg = json.loads(p.read_text())
    missing = [f"{f}={cfg.get(f)}" for f in ("model", "fallback_model")
               if (cfg.get(f), role) not in qualified]
    return ("no passing eval for " + ", ".join(missing)) if missing else None


def sync(db: DB, provider: KeyProvider, roles: list[str], dry_run: bool = False,
         roles_dir: Path = ROLES_DIR) -> dict[str, str]:
    """Create/update one key per role. Returns {role: outcome}."""
    env, qualified, existing = envelopes(db), passing_models(db), provider.list_keys()
    out: dict[str, str] = {}
    for role in roles:
        name, limit = KEY_PREFIX + role, env.get(role)
        if limit is None:
            out[role] = "skipped: no budgets row this month"
        elif (why := unqualified_reason(role, qualified, roles_dir)):
            out[role] = f"refused: {why}"
        elif dry_run:
            out[role] = f"would {'update' if name in existing else 'create'} limit={limit:.2f}"
        elif name in existing:
            provider.update(name, limit)
            out[role] = f"updated limit={limit:.2f}"
        else:
            # A real gateway returns the inference key ONCE; it goes to the agent
            # host's secret store, never to stdout in production.
            provider.create(name, limit)
            out[role] = f"created limit={limit:.2f}"
    return out


def scheduled_roles(roles_dir: Path = ROLES_DIR) -> list[str]:
    roles = []
    for p in sorted(roles_dir.glob("*/role.json")):
        if json.loads(p.read_text()).get("scheduled"):
            roles.append(p.parent.name)
    return roles


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--mock", action="store_true")
    a = ap.parse_args(argv)
    with connect() as db:
        if a.mock or os.environ.get("LLM_PROVIDER", "mock") == "mock":
            provider: KeyProvider = MockKeyProvider(db)
        else:
            mk = os.environ.get("OPENROUTER_MANAGEMENT_KEY")
            if not mk:
                print("OPENROUTER_MANAGEMENT_KEY not set (held by a human, not the agent host)")
                return 2
            provider = OpenRouterKeyProvider(mk)
        if a.list:
            for n, k in sorted(provider.list_keys().items()):
                print(f"  {n:<22} limit={k.get('limit')} usage={k.get('usage')} "
                      f"disabled={k.get('disabled')}")
            return 0
        if not a.sync:
            ap.error("specify --list or --sync")
        for role, outcome in sync(db, provider, scheduled_roles(), a.dry_run).items():
            print(f"  {KEY_PREFIX + role:<22} {outcome}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
