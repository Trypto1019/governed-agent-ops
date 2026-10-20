"""Verifier check definitions: one OUTCOME assertion per scheduled job.

Each check proves the EFFECT a job is supposed to cause, never that the job
"ran". A job that executes cleanly and produces nothing must FAIL here.

Kinds:
  sql_fresh   query returns one timestamp; passes if newer than max_age_h
  sql_count   query returns one integer; passes if >= min (and <= max if set)
  file_fresh  file mtime newer than max_age_h
  http        URL returns expect_status; optional must_contain / must_not_contain
  json_field  URL returns JSON whose fields equal `expect`
  shell       command exits 0 (or its last stdout line equals `expect`)

`max_age_h` carries the cadence: a weekly job gets 8 days, not 24 hours.
`soft_until: YYYY-MM-DD` is a DATED grace period. A permanent soft flag rots;
a dated one expires on its own and the check becomes hard.

This file is a sample of the pattern (the production set had 72 checks).
"""
from __future__ import annotations

CHECKS: list[dict] = [
    # ------------------------------------------------------------ agent jobs
    dict(id="finance.rollup_fresh", job="finance.daily_rollup", kind="sql_fresh",
         sql="SELECT MAX(ts) FROM events WHERE action = 'finance_rollup'", max_age_h=26,
         why="Daily. The finance agent must have written a rollup event, not just run."),
    dict(id="operator.digest_written", job="operator.weekly_digest", kind="sql_fresh",
         sql="SELECT MAX(ts) FROM events WHERE action = 'weekly_digest'", max_age_h=8 * 24,
         why="Weekly. The digest is the operator's product; no digest, no week."),

    # ------------------------------------------------------------ script jobs
    dict(id="seo.page_scan_fresh", job="seo.scan_pages", kind="sql_fresh",
         sql="SELECT MAX(ts) FROM events WHERE action = 'page_scan'", max_age_h=48,
         why="A scanner that exits 0 and records nothing is the classic silent failure."),
    dict(id="nurture.sends_fresh", job="nurture.sequence_send", kind="sql_fresh",
         sql="SELECT MAX(ts) FROM events WHERE action = 'sequence_send'", max_age_h=26,
         why="Daily. Sequences must actually send."),
    dict(id="sensor.health_rows_fresh", job="sensor.health", kind="sql_fresh",
         sql="SELECT MAX(pulled_at) FROM metrics_daily WHERE source = 'health_sensor'",
         max_age_h=2,
         why="KPIs are read from sensors agents cannot write; a stale sensor blinds them."),
    dict(id="sensor.health_not_zeroed", job="sensor.health", kind="sql_count",
         sql="SELECT COUNT(*) FROM metrics_daily WHERE source = 'health_sensor' AND value > 0",
         min=1,
         why="A row is not enough; it must carry real values (a job once wrote zeros for months)."),

    # ------------------------------------------------------------ lead router invariants
    dict(id="router.shadow_decisions_flowing", job="router.shadow", kind="sql_count",
         sql="SELECT COUNT(*) FROM events WHERE action = 'shadow_decision'", min=1,
         why="Shadow mode exists to accumulate decisions a human can read."),
    dict(id="router.shadow_until_approved", job="router.shadow", kind="shell",
         cmd="{python} -m verifier.rchecks.router_shadow_until_approved", expect="OK",
         why="The router may only leave shadow mode with an approved Tier-2 trail."),
    dict(id="router.ev_never_fabricated", job="router.shadow", kind="shell",
         cmd="{python} -m verifier.rchecks.router_ev_never_fabricated", expect="OK",
         why="Every winning bid traces to evidence, never a default."),
    dict(id="consent.rate_not_collapsing", job="funnel", kind="shell",
         cmd="{python} -m verifier.rchecks.consent_rate_not_collapsing", expect="OK",
         why="A form regression that drops consent makes every lead unsellable, silently."),

    # ------------------------------------------------------------ governance invariants
    dict(id="evals.charter_models_qualified", job="model_eval", kind="shell",
         cmd="{python} -m evals.model_eval --check-roles", expect="OK",
         why="A role may only name a model with a passing scorecard < 90 days old."),
    dict(id="compliance.gate_precision_recall", job="compliance", kind="shell",
         cmd="{python} -m compliance.compliance_check --eval-set --no-llm",
         why="The deterministic gate must hold >= 0.95 precision and recall on its labels."),
    dict(id="watchdog.state_fresh", job="watchdog", kind="file_fresh",
         path="var/watchdog_state.json", max_age_h=1,
         why="The watchdog proves it ran by writing state, not by being scheduled."),
]

# User-facing surfaces: each must serve and render its markup.
SURFACES: list[dict] = [
    dict(id="surface.home", path="/", must_contain="Acme Lending"),
    dict(id="surface.prequal_form", path="/prequal/", must_contain='id="prequal-root"',
         must_not_contain='href="#"'),
    dict(id="surface.health_api", path="/api/health", kind="json_field",
         expect={"status": "ok"}),
]
