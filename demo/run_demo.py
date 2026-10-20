#!/usr/bin/env python3
"""End-to-end offline demo. No API keys, no network.

  1. seed a fresh SQLite database with synthetic data
  2. qualify each scheduled role's models with the eval harness (mock models)
  3. provision per-role, spend-capped keys (mock gateway)
  4. run the dispatcher: one job causes nothing (suspicious), one fails twice
     (auto-suspended), one exhausts its key (402 -> budget_exhausted), one
     agent hits a regressed view (PII blocked) and an undeclared tool (denied)
  5. run the watchdog against a staged incident (broken nginx config, dead form)
  6. run the Verifier
  7. jump 49 hours ahead: the unanswered approval expires, never executed
  8. print a summary
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from approvals.approvals import ListNotifier, escalate, post_pending, summary  # noqa: E402
from budget.provision_keys import MockKeyProvider, scheduled_roles, sync  # noqa: E402
from common.clock import iso, now  # noqa: E402
from common.db import as_json, connect  # noqa: E402
from demo.seed import seed  # noqa: E402
from dispatcher.dispatcher import drain  # noqa: E402
from evals.model_eval import build_llm, evaluate, report  # noqa: E402
from verifier.verify import report as verifier_report, run_all  # noqa: E402
from watchdog.watchdog import Config, MockProbes, Watchdog  # noqa: E402


def banner(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 66 - len(title)))


def main() -> int:
    db_path = ROOT / "var" / "demo.sqlite"
    db_path.parent.mkdir(exist_ok=True)
    for p in (db_path, ROOT / "var" / "watchdog_state.json"):
        p.unlink(missing_ok=True)
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ.setdefault("LLM_PROVIDER", "mock")
    t0 = now().replace(microsecond=0)
    os.environ["GOV_OPS_NOW"] = iso(t0)

    banner("1. seed (synthetic data only)")
    with connect() as db:
        seed(db)
        print(f"  leads={db.scalar('SELECT COUNT(*) FROM leads')} "
              f"buyers={db.scalar('SELECT COUNT(*) FROM buyers')} "
              f"jobs={db.scalar('SELECT COUNT(*) FROM jobs')}")

    banner("2. model evals (mock models; cost never affects pass)")
    with connect() as db:
        candidates = set()
        for role in scheduled_roles():
            cfg = json.loads((ROOT / "roles" / role / "role.json").read_text())
            candidates |= {(cfg["model"], role), (cfg["fallback_model"], role)}
        candidates.add(("vendor-x/sloppy-candidate", "finance"))
        for model, role in sorted(candidates):
            ok = evaluate(db, model, role, build_llm(model), n=2, quiet=True)
            print(f"  {model:<28} {role:<11} {'PASS' if ok else 'FAIL'}")
        md = report(db, "mock")
        (ROOT / "var" / "MODEL_SCORECARD.md").write_text(md)
        fail_lines = [ln for ln in md.splitlines() if ln.startswith("- `")]
        for ln in fail_lines:
            print("  " + ln)

    banner("3. provision per-role keys (mock gateway, limit = envelope)")
    with connect() as db:
        for role, outcome in sync(db, MockKeyProvider(db), scheduled_roles()).items():
            print(f"  acme-{role:<12} {outcome}")

    banner("4. dispatcher, tick at T0")
    drain()
    banner("4b. dispatcher, tick at T0 + 10 minutes")
    os.environ["GOV_OPS_NOW"] = iso(t0 + timedelta(minutes=10))
    drain()

    banner("5. watchdog (staged incident: broken nginx config + dead form)")
    probes = MockProbes(has_snapshot=True)
    notes = ListNotifier()
    wd = Watchdog(probes, notes, Config())
    wd.run("liveness")                        # healthy: snapshot known-good config
    probes.nginx_ok = False                   # someone ships a bad config...
    probes.forms_ok["/prequal/"] = False      # ...and the form stops rendering
    wd.run("liveness")
    wd.run("browser")
    wd.run("daily")
    for msg in notes.sent:
        print(f"  {msg}")
    print(f"  probe actions: {probes.actions}")

    banner("6. verifier (proves effects, not execution)")
    with connect() as db:
        results, hard = run_all(db)
    print(verifier_report(results, hard))

    banner("7. approvals at T0 + 49h (no human answered)")
    os.environ["GOV_OPS_NOW"] = iso(t0 + timedelta(hours=49))
    with connect() as db:
        n = ListNotifier()
        post_pending(db, n)
        escalate(db, n)
        for msg in n.sent:
            print(f"  {msg}")

    banner("8. summary")
    with connect() as db:
        runs = Counter(r["status"] for r in db.query("SELECT status FROM job_runs"))
        suspended = [r["target"] for r in db.query(
            "SELECT target FROM events WHERE action = 'suspend_job'")]
        blocks = db.query("SELECT agent, params FROM events WHERE action = 'compliance_block'")
        denied = [as_json(r["result"]).get("denied_tools") for r in db.query(
            "SELECT result FROM events WHERE action = 'run'")]
        shadow = Counter(as_json(r["result"])["decision"] for r in db.query(
            "SELECT result FROM events WHERE action = 'shadow_decision'"))
        live_sales = db.scalar("SELECT COUNT(*) FROM lead_sales")
        spend = db.query("SELECT name, limit_usd, usage_usd FROM provider_keys ORDER BY name")
        print(f"  dispatcher runs:        {dict(runs)}")
        print(f"  auto-suspended jobs:    {suspended}")
        print(f"  PII blocks:             {[(b['agent'], as_json(b['params'])['pattern']) for b in blocks]}")
        print(f"  denied tool calls:      {[d for d in denied if d]}")
        print(f"  shadow decisions:       {dict(shadow)}  (live sales: {live_sales})")
        print(f"  key spend (usd):        "
              + ", ".join(f"{k['name']}={float(k['usage_usd']):.4f}/{float(k['limit_usd']):.2f}"
                          for k in spend))
        passed = sum(1 for r in results if r["passed"])
        print(f"  verifier:               {passed}/{len(results)} checks passing, "
              f"{hard} hard failure(s)")
        print(f"  approvals:              {summary(db)}")
        print(f"  scorecard:              var/MODEL_SCORECARD.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
