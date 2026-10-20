-- SQLite mirror of schema/ddl.sql for the offline demo and tests.
-- Booleans are INTEGER 0/1, JSON is TEXT, timestamps are ISO-8601 TEXT (UTC).

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, role TEXT NOT NULL, kind TEXT NOT NULL, command TEXT NOT NULL,
    schedule TEXT NOT NULL, outcome_check TEXT, enabled INTEGER NOT NULL DEFAULT 1,
    next_run TEXT, last_status TEXT, consecutive_failures INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL REFERENCES jobs(id),
    started TEXT NOT NULL, ended TEXT, status TEXT NOT NULL, events_written INTEGER, detail TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, agent TEXT NOT NULL,
    action TEXT NOT NULL, target TEXT, params TEXT NOT NULL DEFAULT '{}',
    result TEXT NOT NULL DEFAULT '{}', cost_usd REAL
);
CREATE INDEX IF NOT EXISTS events_ts_idx ON events (ts);
CREATE TABLE IF NOT EXISTS verifier_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, ran_at TEXT NOT NULL,
    check_id TEXT NOT NULL, job TEXT NOT NULL, passed INTEGER NOT NULL,
    soft INTEGER NOT NULL DEFAULT 0, observed TEXT, expected TEXT, detail TEXT, duration_ms INTEGER
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT NOT NULL, tier INTEGER NOT NULL,
    title TEXT NOT NULL, body TEXT, sticky INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending', created TEXT NOT NULL,
    escalations INTEGER NOT NULL DEFAULT 0, notified INTEGER NOT NULL DEFAULT 0,
    decided_by TEXT, decided_at TEXT
);
CREATE TABLE IF NOT EXISTS budgets (
    month TEXT NOT NULL, agent TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'llm',
    envelope_usd REAL NOT NULL, PRIMARY KEY (month, agent, category)
);
CREATE TABLE IF NOT EXISTS metrics_daily (
    day TEXT NOT NULL, metric TEXT NOT NULL, dimension TEXT NOT NULL DEFAULT '',
    value REAL NOT NULL, source TEXT NOT NULL, pulled_at TEXT NOT NULL,
    PRIMARY KEY (day, metric, dimension)
);
CREATE TABLE IF NOT EXISTS memory (
    role TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, updated TEXT NOT NULL,
    PRIMARY KEY (role, key)
);
CREATE TABLE IF NOT EXISTS model_evals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, model TEXT NOT NULL, role TEXT NOT NULL,
    eval_version TEXT NOT NULL, ran_at TEXT NOT NULL, f1_fabrications INTEGER,
    f2_tool_valid_pct REAL, f2_json_valid_pct REAL, f2_hallucinated_tools INTEGER,
    f3_violations INTEGER, f4_score REAL, f5_repeats INTEGER, f6_flip_pct REAL,
    f7_cost_median REAL, f7_p95_ms INTEGER, detail TEXT, pass INTEGER
);
CREATE TABLE IF NOT EXISTS model_eval_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, eval_id INTEGER NOT NULL REFERENCES model_evals(id),
    family TEXT NOT NULL, case_id TEXT NOT NULL, passed INTEGER NOT NULL, detail TEXT,
    cost_usd REAL, latency_ms INTEGER
);
CREATE TABLE IF NOT EXISTS provider_keys (
    name TEXT PRIMARY KEY, limit_usd REAL NOT NULL, usage_usd REAL NOT NULL DEFAULT 0,
    disabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, first_name TEXT,
    last_name TEXT, email TEXT, phone TEXT, ssn TEXT, state TEXT, credit_tier TEXT,
    loan_amount INTEGER, loan_purpose TEXT, has_checking_account INTEGER,
    consent_contact INTEGER NOT NULL DEFAULT 0, trustedform_cert TEXT
);
CREATE TABLE IF NOT EXISTS buyers (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active', licensed_states TEXT NOT NULL DEFAULT '[]',
    payload_schema TEXT NOT NULL, ssn_required INTEGER NOT NULL DEFAULT 0,
    contract_ref TEXT, endpoint_ping TEXT
);
CREATE TABLE IF NOT EXISTS buyer_filters (
    buyer_id INTEGER PRIMARY KEY REFERENCES buyers(id), tiers TEXT, states TEXT,
    min_amount INTEGER, max_amount INTEGER, purposes TEXT,
    requires_trustedform INTEGER NOT NULL DEFAULT 1, requires_checking INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS route_floors (
    tier TEXT NOT NULL, state TEXT NOT NULL, floor REAL NOT NULL, PRIMARY KEY (tier, state)
);
CREATE TABLE IF NOT EXISTS lead_offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER NOT NULL REFERENCES leads(id),
    buyer_id INTEGER NOT NULL REFERENCES buyers(id), responded_at TEXT NOT NULL, bid REAL,
    exclusive INTEGER, accepted INTEGER NOT NULL, decline_reason TEXT, latency_ms INTEGER,
    shadow INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS lead_sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER NOT NULL REFERENCES leads(id),
    buyer_id INTEGER NOT NULL REFERENCES buyers(id), price REAL,
    shadow INTEGER NOT NULL DEFAULT 1, sold_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, comment TEXT NOT NULL
);
CREATE VIEW IF NOT EXISTS leads_agg AS
    SELECT substr(created_at, 1, 10) AS day, state, credit_tier, COUNT(*) AS n,
           SUM(consent_contact) AS consented
    FROM leads GROUP BY 1, 2, 3;
CREATE VIEW IF NOT EXISTS recent_feedback AS
    SELECT created_at, comment FROM feedback;
