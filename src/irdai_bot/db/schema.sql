-- Long-format facts warehouse. See /Users/krishnakumar/.claude/plans/breezy-zooming-fox.md.

CREATE TABLE IF NOT EXISTS facts (
    insurer         TEXT NOT NULL,
    fy              TEXT NOT NULL,          -- '2025-26'
    period          TEXT NOT NULL,          -- 'Q4' | 'FY' (annual/YTD)
    form            TEXT NOT NULL,          -- 'L-1-A-RA'
    segment         TEXT NOT NULL,          -- 'nonpar_life' | 'grand_total' | 'unspecified'
    line_item       TEXT NOT NULL,          -- canonical id
    value           DOUBLE PRECISION,
    unit            TEXT NOT NULL,          -- 'INR'
    declared_scale  TEXT,                   -- 'lakh' | 'crore' as filed
    is_restated     BOOLEAN DEFAULT FALSE,
    source_sha256   TEXT NOT NULL,
    source_locator  TEXT NOT NULL,          -- 'page=3;row_top=210.4;col=NON-PARTICIPATING LIFE'
    raw_row_label   TEXT NOT NULL,
    raw_col_label   TEXT NOT NULL,
    extracted_at    TIMESTAMPTZ NOT NULL,
    confidence      DOUBLE PRECISION,
    PRIMARY KEY (insurer, fy, period, form, segment, line_item, source_sha256)
);

-- Tracks per-document ingest status so a resumed run skips completed files.
CREATE TABLE IF NOT EXISTS ingest_manifest (
    source_sha256   TEXT PRIMARY KEY,
    insurer         TEXT NOT NULL,
    fy              TEXT NOT NULL,
    url             TEXT NOT NULL,
    status          TEXT NOT NULL,          -- 'downloaded' | 'extracted' | 'loaded' | 'failed'
    updated_at      TIMESTAMPTZ NOT NULL
);

-- (insurer, fy, form) combinations the background fetch workflow
-- (graphs/fetch_graph.py) has tried, so data that genuinely isn't extractable
-- doesn't re-scrape the insurer's site on every question. One row per key.
CREATE TABLE IF NOT EXISTS fetch_log (
    insurer         TEXT NOT NULL,
    fy              TEXT NOT NULL,
    form            TEXT NOT NULL,
    attempted_at    TIMESTAMPTZ NOT NULL,
    facts_available INTEGER NOT NULL,      -- > 0 if facts existed for this key right after the attempt
    PRIMARY KEY (insurer, fy, form)
);

-- Background fetch jobs started by the chat agent (irdai_bot/fetch.py); the
-- frontend polls these via GET /api/fetch-jobs/{job_id}.
CREATE TABLE IF NOT EXISTS fetch_jobs (
    job_id          UUID PRIMARY KEY,
    insurer         TEXT NOT NULL,
    fiscal_years    TEXT[] NOT NULL,        -- requested
    forms           TEXT[] NOT NULL,        -- requested
    fetching        TEXT[] NOT NULL,        -- 'fy form' pairs actually missing when the job started
    status          TEXT NOT NULL,          -- 'running' | 'succeeded' | 'failed'
    result          JSONB,
    error           TEXT,
    started_at      TIMESTAMPTZ NOT NULL,
    finished_at     TIMESTAMPTZ,
    heartbeat_at    TIMESTAMPTZ,            -- bumped while the job's thread is alive (fetch.py)
    progress        JSONB                   -- {"stage", "filings_done", "filings_total"}
);
ALTER TABLE fetch_jobs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ;
ALTER TABLE fetch_jobs ADD COLUMN IF NOT EXISTS progress JSONB;

-- Any disclosure form, any fiscal year, transcribed cell-by-cell as printed
-- (extract/disclosures.py) — everything the curated `facts` pipeline doesn't
-- cover. Only the filing's own period (prior-year comparative columns are
-- skipped); every value's digits were verified to be printed on its page.
CREATE TABLE IF NOT EXISTS disclosure_values (
    insurer           TEXT NOT NULL,
    fy                TEXT NOT NULL,
    schema_version    TEXT NOT NULL,        -- 'v2010' | 'v2021', by the filing's period end
    form              TEXT NOT NULL,        -- 'L-38' (number only; the heading as printed is form_heading)
    form_key          TEXT NOT NULL,        -- canonical, normalize/canon/forms.yaml
    form_heading      TEXT NOT NULL,
    table_title       TEXT NOT NULL,
    row_label         TEXT NOT NULL,
    column_label      TEXT NOT NULL,
    measure           TEXT NOT NULL,
    period            TEXT NOT NULL,        -- 'quarter' | 'year_to_date' | 'as_at' | 'other'
    period_end        DATE,
    unit              TEXT NOT NULL,        -- 'INR' | 'count' | 'percent' | 'ratio' | 'other'
    scale             TEXT NOT NULL,        -- as declared: 'units' | 'thousand' | 'lakh' | 'crore' | 'million'
    raw_value         TEXT NOT NULL,
    value             DOUBLE PRECISION,     -- base units: rupees for INR; counts/percent as printed
    source_sha256     TEXT NOT NULL,
    source_url        TEXT NOT NULL,
    page_number       INTEGER NOT NULL,
    extraction_method TEXT NOT NULL,
    extracted_at      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (source_sha256, page_number, table_title, row_label, column_label)
);
CREATE INDEX IF NOT EXISTS disclosure_values_lookup ON disclosure_values (insurer, form, fy);

-- One row per filing page run through extract/disclosures.py, with the
-- rendered page text kept for provenance/debugging.
CREATE TABLE IF NOT EXISTS disclosure_pages (
    source_sha256             TEXT NOT NULL,
    page_number               INTEGER NOT NULL,
    insurer                   TEXT NOT NULL,
    fy                        TEXT NOT NULL,
    form                      TEXT NOT NULL,
    heading                   TEXT NOT NULL,
    page_text                 TEXT NOT NULL,
    status                    TEXT NOT NULL,   -- 'extracted' | 'failed'
    values_extracted          INTEGER NOT NULL,
    values_dropped_unverified INTEGER NOT NULL,
    error                     TEXT,
    extracted_at              TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (source_sha256, page_number)
);

-- Label-mapping cache: (insurer, form, kind, raw_label) -> resolved mapping,
-- so a repeat ingest of the same insurer/form (same wording year over year)
-- costs zero LLM calls. kind = 'line_item' | 'column'.
CREATE TABLE IF NOT EXISTS label_cache (
    insurer         TEXT NOT NULL,
    form            TEXT NOT NULL,
    kind            TEXT NOT NULL,
    raw_label       TEXT NOT NULL,
    canonical_id    TEXT,                   -- line_item id (kind='line_item')
    segment_id      TEXT,                   -- (kind='column')
    period          TEXT,                   -- 'annual' | 'quarterly' | 'unknown' (kind='column')
    is_comparative  BOOLEAN,                -- prior-year column embedded in the same table (kind='column')
    confidence      DOUBLE PRECISION NOT NULL,
    reasoning       TEXT,
    updated_at      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (insurer, form, kind, raw_label)
);
