-- governed-agent-ops: Postgres DDL (structure only, no data).
-- The local demo uses schema/sqlite.sql, which mirrors this file.
-- Agents connect as a read-only role that can SELECT the *_agg views and the
-- ops tables, never the base lead tables (see "PII boundary" in README).

CREATE TABLE IF NOT EXISTS jobs (
    id                   text PRIMARY KEY,           -- e.g. 'finance.daily_rollup'
    role                 text NOT NULL,              -- concurrency is 1 per role
    kind                 text NOT NULL,              -- 'agent' | 'script' | 'sensor'
    command              text NOT NULL,
    schedule             text NOT NULL,              -- '5m', '1h', '24h', '7d'
    outcome_check        text,                       -- a check id in verifier/checks.py
    enabled              boolean NOT NULL DEFAULT true,
    next_run             timestamptz,
    last_status          text,
    consecutive_failures integer NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS job_runs (
    id             bigserial PRIMARY KEY,
    job            text NOT NULL REFERENCES jobs(id),
    started        timestamptz NOT NULL,
    ended          timestamptz,
    status         text NOT NULL,                    -- running|ok|suspicious|failed|budget_exhausted|dry-run
    events_written integer,
    detail         text
);

CREATE TABLE IF NOT EXISTS events (
    id       bigserial PRIMARY KEY,
    ts       timestamptz NOT NULL,
    agent    text NOT NULL,
    action   text NOT NULL,
    target   text,
    params   jsonb NOT NULL DEFAULT '{}',
    result   jsonb NOT NULL DEFAULT '{}',
    cost_usd numeric(12,6)
);
CREATE INDEX IF NOT EXISTS events_ts_idx ON events (ts);
CREATE INDEX IF NOT EXISTS events_agent_idx ON events (agent, ts);

CREATE TABLE IF NOT EXISTS verifier_results (
    id          bigserial PRIMARY KEY,
    run_id      text NOT NULL,
    ran_at      timestamptz NOT NULL,
    check_id    text NOT NULL,
    job         text NOT NULL,
    passed      boolean NOT NULL,
    soft        boolean NOT NULL DEFAULT false,
    observed    text,
    expected    text,
    detail      text,
    duration_ms integer
);

CREATE TABLE IF NOT EXISTS approvals (
    id             bigserial PRIMARY KEY,
    agent          text NOT NULL,
    tier           integer NOT NULL,
    title          text NOT NULL,
    body           text,
    sticky         boolean NOT NULL DEFAULT false,
    status         text NOT NULL DEFAULT 'pending',  -- pending|approved|rejected|expired
    created        timestamptz NOT NULL,
    escalations    integer NOT NULL DEFAULT 0,
    notified       boolean NOT NULL DEFAULT false,
    decided_by     text,
    decided_at     timestamptz
);

CREATE TABLE IF NOT EXISTS budgets (
    month        date NOT NULL,
    agent        text NOT NULL,
    category     text NOT NULL DEFAULT 'llm',
    envelope_usd numeric(10,2) NOT NULL,
    PRIMARY KEY (month, agent, category)
);

CREATE TABLE IF NOT EXISTS metrics_daily (
    day       date NOT NULL,
    metric    text NOT NULL,
    dimension text NOT NULL DEFAULT '',
    value     double precision NOT NULL,
    source    text NOT NULL,
    pulled_at timestamptz NOT NULL,
    PRIMARY KEY (day, metric, dimension)
);

CREATE TABLE IF NOT EXISTS memory (
    role    text NOT NULL,
    key     text NOT NULL,
    value   jsonb NOT NULL,
    updated timestamptz NOT NULL,
    PRIMARY KEY (role, key)
);

CREATE TABLE IF NOT EXISTS model_evals (
    id                    bigserial PRIMARY KEY,
    model                 text NOT NULL,
    role                  text NOT NULL,
    eval_version          text NOT NULL,
    ran_at                timestamptz NOT NULL,
    f1_fabrications       integer,
    f2_tool_valid_pct     numeric(5,1),
    f2_json_valid_pct     numeric(5,1),
    f2_hallucinated_tools integer,
    f3_violations         integer,
    f4_score              numeric(4,2),
    f5_repeats            integer,
    f6_flip_pct           numeric(5,2),
    f7_cost_median        numeric(12,6),
    f7_p95_ms             integer,
    detail                jsonb,
    pass                  boolean                   -- NULL while running
);

CREATE TABLE IF NOT EXISTS model_eval_runs (
    id         bigserial PRIMARY KEY,
    eval_id    bigint NOT NULL REFERENCES model_evals(id),
    family     text NOT NULL,
    case_id    text NOT NULL,
    passed     boolean NOT NULL,
    detail     jsonb,
    cost_usd   numeric(12,6),
    latency_ms integer
);

-- Mock provider key store. In production the per-role keys live at the LLM
-- gateway (OpenRouter); this table only backs the offline mock.
CREATE TABLE IF NOT EXISTS provider_keys (
    name      text PRIMARY KEY,
    limit_usd numeric(10,4) NOT NULL,
    usage_usd numeric(12,6) NOT NULL DEFAULT 0,
    disabled  boolean NOT NULL DEFAULT false
);

-- ------------------------------------------------------------- lead routing
CREATE TABLE IF NOT EXISTS leads (
    id                   bigserial PRIMARY KEY,
    created_at           timestamptz NOT NULL,
    first_name           text,
    last_name            text,
    email                text,
    phone                text,
    ssn                  text,                       -- never logged, never sent to a ping
    state                text,
    credit_tier          text,
    loan_amount          integer,
    loan_purpose         text,
    has_checking_account boolean,
    consent_contact      boolean NOT NULL DEFAULT false,
    trustedform_cert     text
);

CREATE TABLE IF NOT EXISTS buyers (
    id              bigserial PRIMARY KEY,
    name            text NOT NULL,
    kind            text NOT NULL,                   -- 'lender' | 'network' | 'offer_wall'
    status          text NOT NULL DEFAULT 'active',
    licensed_states jsonb NOT NULL DEFAULT '[]',
    payload_schema  text NOT NULL,                   -- adapter module name
    ssn_required    boolean NOT NULL DEFAULT false,
    contract_ref    text,
    endpoint_ping   text
);

CREATE TABLE IF NOT EXISTS buyer_filters (
    buyer_id             bigint PRIMARY KEY REFERENCES buyers(id),
    tiers                jsonb,
    states               jsonb,
    min_amount           integer,
    max_amount           integer,
    purposes             jsonb,
    requires_trustedform boolean NOT NULL DEFAULT true,
    requires_checking    boolean NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS route_floors (
    tier  text NOT NULL,
    state text NOT NULL,                              -- '*' = default for the tier
    floor numeric(8,2) NOT NULL,
    PRIMARY KEY (tier, state)
);

CREATE TABLE IF NOT EXISTS lead_offers (
    id             bigserial PRIMARY KEY,
    lead_id        bigint NOT NULL REFERENCES leads(id),
    buyer_id       bigint NOT NULL REFERENCES buyers(id),
    responded_at   timestamptz NOT NULL,
    bid            numeric(8,2),
    exclusive      boolean,
    accepted       boolean NOT NULL,
    decline_reason text,
    latency_ms     integer,
    shadow         boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS lead_sales (
    id       bigserial PRIMARY KEY,
    lead_id  bigint NOT NULL REFERENCES leads(id),
    buyer_id bigint NOT NULL REFERENCES buyers(id),
    price    numeric(8,2),
    shadow   boolean NOT NULL DEFAULT true,
    sold_at  timestamptz NOT NULL
);

-- Free-text customer feedback. Deliberately contains PII in the demo seed so
-- the runtime PII filter has something to catch (see recent_feedback below).
CREATE TABLE IF NOT EXISTS feedback (
    id         bigserial PRIMARY KEY,
    created_at timestamptz NOT NULL,
    comment    text NOT NULL
);

-- ------------------------------------------------------------- agent views
-- Agents read views, never base tables. Two barriers: the DB grant AND the
-- allow-list in runtime/tools.py.
CREATE OR REPLACE VIEW leads_agg AS
    SELECT CAST(created_at AS date) AS day, state, credit_tier, COUNT(*) AS n,
           SUM(CASE WHEN consent_contact THEN 1 ELSE 0 END) AS consented
    FROM leads GROUP BY 1, 2, 3;

-- A view that "regressed": someone added the free-text column. The runtime
-- PII filter is the second barrier that catches this.
CREATE OR REPLACE VIEW recent_feedback AS
    SELECT created_at, comment FROM feedback;
