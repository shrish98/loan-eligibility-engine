-- Loan Eligibility Engine: database schema.
-- Runs automatically when the Postgres container starts with an empty volume.
-- On AWS, run this same file once against the RDS instance.

-- One row per uploaded CSV. Lets every later step work on "just this upload"
-- instead of scanning all users, and gives us a status to follow the pipeline.
CREATE TABLE upload_batches (
    batch_id        UUID PRIMARY KEY,
    s3_key          TEXT        NOT NULL,
    status          TEXT        NOT NULL DEFAULT 'processing'
                    CHECK (status IN ('processing', 'ingested', 'matched', 'notified', 'failed')),
    row_count       INT,
    rejected_count  INT,
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE users (
    user_id            UUID PRIMARY KEY,
    name               TEXT,
    email              TEXT          NOT NULL,
    monthly_income     NUMERIC(12,2) NOT NULL CHECK (monthly_income >= 0),
    credit_score       INT           NOT NULL CHECK (credit_score BETWEEN 300 AND 900),
    employment_status  TEXT          NOT NULL,
    age                INT           NOT NULL CHECK (age BETWEEN 18 AND 100),
    -- The upload that most recently inserted/updated this user.
    batch_id           UUID          NOT NULL REFERENCES upload_batches(batch_id),
    created_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ   NOT NULL DEFAULT now()
);

-- Workflow B fetches "the users from this upload".
CREATE INDEX idx_users_batch ON users (batch_id);

-- Filled by Workflow A (crawler). Hard criteria are stored as real columns so the
-- first matching stage can be a plain SQL join; anything that can't be expressed
-- as a number goes into eligibility_notes for the later (LLM) stage.
CREATE TABLE loan_products (
    product_id           SERIAL PRIMARY KEY,
    lender               TEXT          NOT NULL,
    product_name         TEXT          NOT NULL,
    source_url           TEXT          NOT NULL,
    interest_rate_min    NUMERIC(5,2),             -- % per annum
    interest_rate_max    NUMERIC(5,2),
    min_monthly_income   NUMERIC(12,2),            -- NULL = no stated minimum
    min_credit_score     INT,
    max_credit_score     INT,
    min_age              INT,
    max_age              INT,
    allowed_employment   TEXT[],                   -- NULL = any employment type
    eligibility_notes    TEXT,                     -- free-text / qualitative rules
    content_hash         TEXT,                     -- hash of the crawled page; unchanged page => skip re-extraction
    is_active            BOOLEAN       NOT NULL DEFAULT TRUE,
    last_crawled_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    UNIQUE (lender, product_name)
);

-- Filled by Workflow B (matcher), read by Workflow C (notifier).
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
