-- Loan Eligibility Engine: database schema.
-- Runs automatically when the local Postgres container starts with an empty volume.
-- On AWS, scripts/init-rds.sh runs this same file once against the RDS instance.

-- ---------------------------------------------------------------------------
-- Ingestion
-- ---------------------------------------------------------------------------

-- One row per uploaded CSV. Lets every later step work on "just this upload"
-- instead of scanning all users, and gives the UI a status to poll.
--   pending    -> upload URL handed out, file not received yet
--   processing -> ingest Lambda is parsing the file
--   ingested   -> users stored, n8n Workflow B notified
--   matching   -> Workflow B running
--   matched    -> matches stored, Workflow C notified
--   notified   -> emails sent
CREATE TABLE upload_batches (
    batch_id        UUID PRIMARY KEY,
    s3_key          TEXT        NOT NULL,
    original_name   TEXT,
    status          TEXT        NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'processing', 'ingested', 'matching',
                                      'matched', 'notified', 'failed')),
    row_count       INT,                       -- valid rows stored
    rejected_count  INT,                       -- rows that failed validation
    error_message   TEXT,
    stats           JSONB       NOT NULL DEFAULT '{}'::jsonb,  -- per-stage numbers written by each step
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE users (
    user_id            UUID PRIMARY KEY,
    name               TEXT,
    email              TEXT          NOT NULL,
    monthly_income     NUMERIC(12,2) NOT NULL CHECK (monthly_income >= 0),
    credit_score       INT           NOT NULL CHECK (credit_score BETWEEN 300 AND 900),
    employment_status  TEXT          NOT NULL,   -- normalised to Salaried / Self-Employed / Business
    age                INT           NOT NULL CHECK (age BETWEEN 18 AND 100),
    -- The upload that most recently inserted/updated this user.
    batch_id           UUID          NOT NULL REFERENCES upload_batches(batch_id),
    created_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ   NOT NULL DEFAULT now()
);

-- Workflow B fetches "the users from this upload".
CREATE INDEX idx_users_batch ON users (batch_id);

-- ---------------------------------------------------------------------------
-- Crawling (Workflow A)
-- ---------------------------------------------------------------------------

-- One row per crawled web page. The hash of the page's cleaned text lets the
-- crawler skip the LLM entirely when a page has not changed since last time.
CREATE TABLE source_pages (
    url              TEXT PRIMARY KEY,
    site             TEXT        NOT NULL,     -- bankbazaar / paisabazaar / tatacapital
    content_hash     TEXT,
    product_count    INT         NOT NULL DEFAULT 0,
    last_crawled_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_changed_at  TIMESTAMPTZ
);

-- Hard criteria are stored as real columns so the first matching stage is a
-- plain SQL join. Anything that can't be expressed as a number goes into
-- eligibility_notes, which is the only thing the LLM looks at during matching.
CREATE TABLE loan_products (
    product_id           SERIAL PRIMARY KEY,
    lender               TEXT          NOT NULL,
    product_name         TEXT          NOT NULL,
    source_url           TEXT          NOT NULL REFERENCES source_pages(url),
    interest_rate_min    NUMERIC(5,2),             -- % per annum
    interest_rate_max    NUMERIC(5,2),
    min_monthly_income   NUMERIC(12,2),            -- NULL = no stated minimum
    min_credit_score     INT,
    max_credit_score     INT,
    min_age              INT,
    max_age              INT,
    allowed_employment   TEXT[],                   -- NULL = any employment type
    eligibility_notes    TEXT,                     -- qualitative rules; NULL = numeric rules say it all
    is_active            BOOLEAN       NOT NULL DEFAULT TRUE,
    last_seen_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),   -- last crawl that still found it
    updated_at           TIMESTAMPTZ   NOT NULL DEFAULT now(),
    UNIQUE (lender, product_name)
);

-- ---------------------------------------------------------------------------
-- Matching (Workflow B)
-- ---------------------------------------------------------------------------

-- Users are grouped into coarse "profiles" for the LLM stage: the LLM is asked
-- once per (product, profile) instead of once per (product, user). A profile is
-- employment type + income band + credit-score band, e.g. 'Salaried|30-50k|700-749'.
-- Age is left out on purpose: age limits are numeric and already checked in SQL.
-- At most 3 x 5 x 3 = 45 profiles exist, so one LLM call covers a whole product.
CREATE OR REPLACE FUNCTION profile_key(employment TEXT, income NUMERIC, score INT)
RETURNS TEXT
LANGUAGE sql IMMUTABLE
AS $$
    SELECT employment
        || '|' || CASE WHEN income <  30000 THEN '<30k'
                       WHEN income <  50000 THEN '30-50k'
                       WHEN income <  75000 THEN '50-75k'
                       WHEN income < 100000 THEN '75-100k'
                       ELSE '100k+' END
        || '|' || CASE WHEN score < 700 THEN '<700'
                       WHEN score < 750 THEN '700-749'
                       ELSE '750+' END
$$;

-- Pairs that passed the SQL pre-filter but whose product has qualitative rules.
-- Lives only while Workflow B runs for a batch; emptied at the end.
CREATE TABLE match_candidates (
    batch_id     UUID NOT NULL REFERENCES upload_batches(batch_id) ON DELETE CASCADE,
    user_id      UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    product_id   INT  NOT NULL REFERENCES loan_products(product_id) ON DELETE CASCADE,
    profile_key  TEXT NOT NULL,
    PRIMARY KEY (batch_id, user_id, product_id)
);

-- Cache of LLM verdicts per (product, profile). notes_md5 ties a verdict to the
-- exact wording of the product's rules, so a re-crawl that changes the rules
-- automatically invalidates old verdicts. Later uploads reuse cached verdicts
-- and make no LLM calls for profiles already seen.
CREATE TABLE llm_decisions (
    product_id   INT     NOT NULL REFERENCES loan_products(product_id) ON DELETE CASCADE,
    profile_key  TEXT    NOT NULL,
    notes_md5    TEXT    NOT NULL,
    eligible     BOOLEAN NOT NULL,
    reason       TEXT,
    model        TEXT,
    decided_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (product_id, profile_key, notes_md5)
);

-- Filled by Workflow B, read by Workflow C.
CREATE TABLE matches (
    match_id     BIGSERIAL PRIMARY KEY,
    user_id      UUID        NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    product_id   INT         NOT NULL REFERENCES loan_products(product_id) ON DELETE CASCADE,
    batch_id     UUID        NOT NULL REFERENCES upload_batches(batch_id),
    match_stage  TEXT        NOT NULL CHECK (match_stage IN ('rules', 'llm')),  -- which stage approved it
    reason       TEXT,                                                         -- short explanation (shown in email)
    notified_at  TIMESTAMPTZ,                                                  -- NULL = not emailed yet
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, product_id)   -- re-running matching never creates duplicates
);

-- Workflow C looks for "matches in this batch that haven't been emailed".
CREATE INDEX idx_matches_pending ON matches (batch_id) WHERE notified_at IS NULL;
