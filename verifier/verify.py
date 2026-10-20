#!/usr/bin/env python3
"""The Verifier: proves effects, not execution.

Design constraints:
  * Shares NO code with the agents it audits (only the DB adapter and clock).
  * No LLM. Every assertion is deterministic with an explicit threshold.
  * Writes one row per check to `verifier_results`, so its own liveness can be
    asserted on OUTPUT ("wrote results in the last 24h"), never process health.
  * Exit code 1 if any hard check fails, so an external monitor can page.

The dispatcher asks the Verifier for a verdict on each job's `outcome_check`.
They share the check definition, not the implementation of the doing.

  python -m verifier.verify               run all checks, write results, print report
  python -m verifier.verify --dry-run     run and print, do not write
  python -m verifier.verify --only router
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Protocol

from common.clock import hours_since, now, now_iso
from common.db import DB, connect
from verifier.checks import CHECKS, SURFACES

ROOT = Path(__file__).resolve().parent.parent
UA = "AcmeVerifier/1.0"


def site_base() -> str:
    return os.environ.get("SITE_BASE_URL", "https://acme-lending.example.com")


# ------------------------------------------------------------------ HTTP (pluggable)
class Fetcher(Protocol):
    def get(self, url: str) -> tuple[int | None, str]: ...


class UrllibFetcher:
    def get(self, url: str) -> tuple[int | None, str]:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, ""
        except Exception as e:
            return None, type(e).__name__


class MockFetcher:
    """Offline site. Routes are matched on path; unknown paths are 404."""

    DEFAULT_ROUTES = {
        "/": (200, "<html><title>Acme Lending</title></html>"),
        "/prequal/": (200, '<div id="prequal-root"><form></form></div>'),
        "/api/health": (200, json.dumps({"status": "ok"})),
    }

    def __init__(self, routes: dict[str, tuple[int, str]] | None = None):
        self.routes = dict(self.DEFAULT_ROUTES if routes is None else routes)

    def get(self, url: str) -> tuple[int | None, str]:
        path = "/" + url.split("://", 1)[-1].split("/", 1)[-1] if "://" in url else url
        return self.routes.get(path, (404, ""))


def default_fetcher() -> Fetcher:
    return UrllibFetcher() if os.environ.get("VERIFIER_HTTP") == "live" else MockFetcher()


# ------------------------------------------------------------------ runners
@dataclass
class Ctx:
    db: DB
    fetcher: Fetcher


Verdict = tuple[bool, str, str]          # passed, observed, expected


def run_sql_fresh(c: dict, ctx: Ctx) -> Verdict:
    ts = ctx.db.scalar(c["sql"])
    if ts is None:
        return False, "NULL", f"<= {c['max_age_h']}h old"
    age_h = hours_since(ts)
    return age_h <= c["max_age_h"], f"{age_h:.1f}h old", f"<= {c['max_age_h']}h"


def run_sql_count(c: dict, ctx: Ctx) -> Verdict:
    n = int(ctx.db.scalar(c["sql"]) or 0)
    lo, hi = c.get("min"), c.get("max")
    return (lo is None or n >= lo) and (hi is None or n <= hi), str(n), f"min={lo} max={hi}"


def run_file_fresh(c: dict, ctx: Ctx) -> Verdict:
    p = Path(c["path"])
    p = p if p.is_absolute() else ROOT / p
    if not p.exists():
        return False, "missing", f"exists, <= {c['max_age_h']}h"
    age_h = (now().timestamp() - p.stat().st_mtime) / 3600
    return age_h <= c["max_age_h"], f"{max(age_h, 0):.1f}h old", f"<= {c['max_age_h']}h"


def _url(c: dict) -> str:
    return c["url"] if c["url"].startswith("http") else site_base() + c["url"]


def _as_list(v) -> list[str]:
    return [v] if isinstance(v, str) else list(v or [])


def run_http(c: dict, ctx: Ctx) -> Verdict:
    want = c.get("expect_status", 200)
    status, body = ctx.fetcher.get(_url(c))
    if status != want:
        return False, f"status {status}", f"status {want}"
    for s in _as_list(c.get("must_contain")):
        if s not in body:
            return False, f"missing {s!r}", f"body contains {s!r}"
    for s in _as_list(c.get("must_not_contain")):
        if s in body:
            return False, f"found {s!r}", f"body lacks {s!r}"
    return True, f"status {status}", f"status {want} + content"


def run_json_field(c: dict, ctx: Ctx) -> Verdict:
    """Parse, don't substring-match: compact vs pretty JSON is a false negative."""
    expected = ", ".join(f"{k}={v!r}" for k, v in c["expect"].items())
    status, body = ctx.fetcher.get(_url(c))
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return False, f"status {status}, not JSON", expected
    bad = [f"{k}={data.get(k)!r}" for k, v in c["expect"].items() if data.get(k) != v]
    return (not bad), (", ".join(bad) or "all fields match"), expected


def run_shell(c: dict, ctx: Ctx) -> Verdict:
    cmd = c["cmd"].format(python=sys.executable)
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180, cwd=ROOT)
    lines = (r.stdout or "").strip().splitlines()
    out = lines[-1].strip() if lines else ""
    if "expect" in c:
        return out == c["expect"], out or "(no output)", c["expect"]
    return r.returncode == 0, f"rc={r.returncode}", "rc=0"


RUNNERS: dict[str, Callable[[dict, Ctx], Verdict]] = {
    "sql_fresh": run_sql_fresh, "sql_count": run_sql_count, "file_fresh": run_file_fresh,
    "http": run_http, "json_field": run_json_field, "shell": run_shell}


def surface_to_check(s: dict) -> dict:
    base = dict(id=s["id"], job="surface_registry", kind=s.get("kind", "http"), url=s["path"],
                why="A registered user-facing surface must serve and render its markup.")
    if base["kind"] == "json_field":
        return {**base, "expect": s["expect"]}
    return {**base, "expect_status": s.get("expect_status", 200),
            "must_contain": s.get("must_contain"), "must_not_contain": s.get("must_not_contain")}


def all_checks() -> list[dict]:
    return list(CHECKS) + [surface_to_check(s) for s in SURFACES]


def is_soft(c: dict, today: date | None = None) -> bool:
    today = today or now().date()
    su = c.get("soft_until")
    return bool(c.get("soft")) or bool(su and today.isoformat() <= su)


def run_one(c: dict, ctx: Ctx) -> dict:
    t0 = time.time()
    try:
        passed, observed, expected = RUNNERS[c["kind"]](c, ctx)
        detail = None
    except Exception as e:
        passed, observed, expected = False, f"EXC {type(e).__name__}", "check to run"
        detail = str(e)[:400]
    return dict(check_id=c["id"], job=c["job"], passed=bool(passed), observed=observed,
                expected=expected, detail=detail, ms=int((time.time() - t0) * 1000),
                soft=is_soft(c), why=c.get("why", ""))


def verdict_for(db: DB, check_id: str, fetcher: Fetcher | None = None) -> tuple[bool, str]:
    """Used by the dispatcher: run one named check and return (passed, detail)."""
    match = next((c for c in all_checks() if c["id"] == check_id), None)
    if match is None:
        return False, f"outcome_check '{check_id}' not found in verifier/checks.py"
    r = run_one(match, Ctx(db, fetcher or default_fetcher()))
    return r["passed"], f"{check_id}: observed={r['observed']} expected={r['expected']}"


def run_all(db: DB, checks: list[dict] | None = None, fetcher: Fetcher | None = None,
            write: bool = True) -> tuple[list[dict], int]:
    ctx = Ctx(db, fetcher or default_fetcher())
    results = [run_one(c, ctx) for c in (checks if checks is not None else all_checks())]
    hard = sum(1 for r in results if not r["passed"] and not r["soft"])
    if write:
        run_id, ran_at = str(uuid.uuid4()), now_iso()
        for r in results:
            db.execute("INSERT INTO verifier_results (run_id, ran_at, check_id, job, passed, soft, "
                       "observed, expected, detail, duration_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (run_id, ran_at, r["check_id"], r["job"], r["passed"], r["soft"],
                        r["observed"], r["expected"], r["detail"], r["ms"]))
        db.commit()
    return results, hard


def report(results: list[dict], hard: int) -> str:
    width = max(len(r["check_id"]) for r in results) + 2
    lines = ["VERIFIER RUN", "=" * (width + 46)]
    for r in sorted(results, key=lambda x: (x["passed"], x["job"])):
        mark = "PASS" if r["passed"] else ("WARN" if r["soft"] else "FAIL")
        lines.append(f"  {mark}  {r['check_id']:<{width}} {r['observed']:<22} "
                     f"(want {r['expected']})")
    passed = sum(1 for r in results if r["passed"])
    lines += ["=" * (width + 46), f"  {passed}/{len(results)} passed   hard failures: {hard}"]
    failing = [r for r in results if not r["passed"] and not r["soft"]]
    if failing:
        lines.append("\nFAILING CHECKS - why they exist:")
        lines += [f"  [{r['check_id']}] {r['why']}" for r in failing]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only")
    a = ap.parse_args(argv)
    checks = all_checks()
    if a.only:
        checks = [c for c in checks if a.only in c["id"] or a.only in c["job"]]
    with connect() as db:
        results, hard = run_all(db, checks, write=not a.dry_run)
    print(report(results, hard))
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
