#!/usr/bin/env python3
"""Dispatcher - the single cron line.

    */5 * * * *  python -m dispatcher.dispatcher

Reads `jobs` where enabled AND next_run <= now, launches them with concurrency
1 per role / 3 total, records `job_runs`, and counts the `events` each run
produced.

  * A run that exits 0 but writes ZERO events is `suspicious`, not `ok`. A job
    that executes and causes nothing is a failure, not a success.
  * Each job's `outcome_check` is delegated to the Verifier. A passing verdict
    is itself written as a `job_outcome_verified` event, so the success signal
    is an effect, never "the process exited 0".
  * Two consecutive non-ok runs auto-disable the job and log `suspend_job`.
  * A run that hits its key's spend limit (exit 4) is `budget_exhausted`: the
    envelope did its job; that is not counted as a job defect.

Usage: python -m dispatcher.dispatcher [--dry-run] [--now ISO]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from common.clock import iso, interval, now, now_iso
from common.db import DB, connect, database_url
from common.events import log_event

ROOT = Path(__file__).resolve().parent.parent
MAX_TOTAL = 3
SUSPEND_AFTER = 2
JOB_TIMEOUT_S = 1800
EXIT_BUDGET = 4


@dataclass
class RunOutcome:
    job: str
    status: str
    rc: int
    events: int
    suspended: bool = False


def due_jobs(db: DB) -> list[dict]:
    return db.query("SELECT id, role, kind, command, schedule FROM jobs "
                    "WHERE enabled = ? AND (next_run IS NULL OR next_run <= ?) "
                    "ORDER BY next_run IS NOT NULL, next_run, id", (True, now_iso()))


def select_batch(due: list[dict], max_total: int = MAX_TOTAL) -> list[dict]:
    """Concurrency 1 per role, `max_total` overall. The rest wait for next tick."""
    seen, batch = set(), []
    for job in due:
        if job["role"] in seen:
            continue
        if len(batch) >= max_total:
            break
        seen.add(job["role"])
        batch.append(job)
    return batch


def count_events_since(db: DB, role: str, job_id: str, since: str) -> int:
    """Events attributable to this run: filed under the ROLE (agents) or the JOB
    id (scripts that name themselves). Counting only one of them marks every
    script job suspicious forever, which is how a status field gets ignored."""
    return int(db.scalar(
        "SELECT COUNT(*) FROM events WHERE ts >= ? AND (agent = ? OR agent = ? OR target = ?)",
        (since, role, job_id, job_id)))


def outcome_verdict(db: DB, job_id: str) -> tuple[bool, str]:
    check_id = db.scalar("SELECT outcome_check FROM jobs WHERE id = ?", (job_id,))
    if not check_id:
        return False, "no outcome_check defined"
    try:
        from verifier.verify import verdict_for
        return verdict_for(db, check_id)
    except Exception as e:
        return False, f"outcome_check error: {type(e).__name__}: {e}"


def run_job(job: dict, dry: bool = False, url: str | None = None) -> RunOutcome:
    jid, role, schedule = job["id"], job["role"], job["schedule"]
    db = connect(url)
    started = now_iso()
    run_id = db.insert("INSERT INTO job_runs (job, started, status) VALUES (?,?,'running') "
                       "RETURNING id", (jid, started))
    db.commit()

    if dry:
        db.execute("UPDATE job_runs SET ended = ?, status = 'dry-run' WHERE id = ?",
                   (now_iso(), run_id))
        db.commit(); db.close()
        return RunOutcome(jid, "dry-run", 0, 0)

    cmd = job["command"].format(python=sys.executable)
    env = {**os.environ, "DATABASE_URL": url or database_url(), "AGENT_ROLE": role, "JOB_ID": jid}
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=JOB_TIMEOUT_S, cwd=ROOT, env=env)
        rc, err = r.returncode, (r.stderr or "")[-2000:]
    except subprocess.TimeoutExpired:
        rc, err = 124, f"timeout after {JOB_TIMEOUT_S}s"

    # The event we write IS the outcome verdict. A job whose check fails writes
    # no success event and cannot look healthy.
    passed, detail = outcome_verdict(db, jid)
    if passed:
        log_event(db, jid, "job_outcome_verified", jid, {"run_id": run_id},
                  {"passed": True, "detail": detail})

    written = count_events_since(db, role, jid, started)
    if rc == EXIT_BUDGET:
        status = "budget_exhausted"
    elif rc != 0:
        status = "failed"
    elif written == 0:
        status = "suspicious"          # executed cleanly and caused nothing
    else:
        status = "ok"

    db.execute("UPDATE job_runs SET ended = ?, status = ?, events_written = ?, detail = ? "
               "WHERE id = ?", (now_iso(), status, written, (err or detail)[:2000], run_id))
    next_run = iso(now() + interval(schedule))
    suspended = False
    if status in ("ok", "budget_exhausted"):
        db.execute("UPDATE jobs SET last_status = ?, consecutive_failures = 0, next_run = ? "
                   "WHERE id = ?", (status, next_run, jid))
    else:
        db.execute("UPDATE jobs SET last_status = ?, consecutive_failures = "
                   "consecutive_failures + 1, next_run = ? WHERE id = ?", (status, next_run, jid))
        fails = int(db.scalar("SELECT consecutive_failures FROM jobs WHERE id = ?", (jid,)))
        if fails >= SUSPEND_AFTER:
            db.execute("UPDATE jobs SET enabled = ? WHERE id = ?", (False, jid))
            log_event(db, "dispatcher", "suspend_job", jid, {},
                      {"reason": f"{fails} consecutive non-ok runs (last: {status})"})
            suspended = True
    db.commit(); db.close()
    return RunOutcome(jid, status, rc, written, suspended)


def tick(dry: bool = False, url: str | None = None, quiet: bool = False) -> list[RunOutcome]:
    with connect(url) as db:
        due = due_jobs(db)
    if not due:
        if not quiet:
            print("dispatcher: nothing due")
        return []
    batch = select_batch(due)
    if not quiet:
        print(f"dispatcher: {len(due)} job(s) due, running {len(batch)}")
    results: list[RunOutcome] = []
    lock = threading.Lock()

    def worker(job: dict) -> None:
        out = run_job(job, dry, url)
        with lock:
            results.append(out)

    threads = [threading.Thread(target=worker, args=(j,)) for j in batch]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if not quiet:
        for o in sorted(results, key=lambda r: r.job):
            print(f"  {o.job:<26} rc={o.rc:<3} status={o.status:<16} events={o.events}"
                  + ("  -> SUSPENDED" if o.suspended else ""))
    return results


def drain(dry: bool = False, url: str | None = None, quiet: bool = False) -> list[RunOutcome]:
    """Run ticks until nothing is due at this instant (used by the demo)."""
    out: list[RunOutcome] = []
    while True:
        batch = tick(dry, url, quiet=quiet)
        if not batch:
            return out
        out += batch


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--now", help="pin the clock (ISO-8601); children inherit it")
    a = ap.parse_args(argv)
    if a.now:
        os.environ["GOV_OPS_NOW"] = a.now
    tick(a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
