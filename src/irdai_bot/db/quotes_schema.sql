-- Quote-comparison warehouse: guaranteed-return product data (eligibility,
-- rate tables, illustrations, insurer scorecards), ported unchanged from
-- insurance-compare-agent/files/insurance_agent/sql/schema.sql (already
-- Postgres 14+ compatible per that package's own README) except CREATE
-- TABLE/VIEW/INDEX are made idempotent to match db/schema.sql's convention
-- of being safe to re-run on every connect().

-- Guaranteed-return product warehouse: analytical, long-format, provenance on every fact.
-- Portable subset of SQL: runs unchanged on PostgreSQL 14+ and SQLite 3.35+.
-- Conventions
--   * booleans are INTEGER 0/1
--   * ppt = 0 means single pay; ppt = -1 means "applies to every PPT"
--   * ages are age-last-birthday in years unless unit = 'days'
--   * money is INR, ex-GST
--   * every fact row carries source_id; derived rows carry method text instead

CREATE TABLE IF NOT EXISTS insurer (
    insurer_id      TEXT PRIMARY KEY,               -- 'AXISMAX', 'HDFCLIFE'
    name            TEXT NOT NULL,
    irdai_reg_no    TEXT NOT NULL UNIQUE,           -- first 3 chars of every UIN
    website         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_document (
    source_id       TEXT PRIMARY KEY,               -- 'SRC0001'
    insurer_id      TEXT REFERENCES insurer (insurer_id),
    url             TEXT NOT NULL,
    doc_type        TEXT NOT NULL CHECK (doc_type IN (
                        'product_page','prospectus','brochure','leaflet','rate_table',
                        'policy_wording','policy_at_glance','cis','public_disclosure',
                        'campaign_page','bank_partner_page','aggregator_quote','other')),
    authority_rank  INTEGER NOT NULL,               -- 1 = most authoritative (see agent prompt)
    version_label   TEXT,                           -- ARN / 'Prospectus V17 May 2026'
    fetched_at      TEXT NOT NULL,                  -- ISO-8601
    content_sha256  TEXT,                           -- NULL for manually curated rows
    http_status     INTEGER,
    robots_allowed  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS product (
    product_id      TEXT PRIMARY KEY,               -- slug, stable across UIN versions
    insurer_id      TEXT NOT NULL REFERENCES insurer (insurer_id),
    uin_base        TEXT UNIQUE,                    -- '104N124' (UIN minus Vxx); NULL if unknown
    product_name    TEXT NOT NULL,
    linkage         TEXT NOT NULL CHECK (linkage IN ('linked','non_linked')),
    participation   TEXT NOT NULL CHECK (participation IN ('par','non_par')),
    category        TEXT NOT NULL CHECK (category IN ('savings','pension_savings','annuity','protection','ulip')),
    guarantee_level TEXT NOT NULL CHECK (guarantee_level IN ('full','partial','none')),
    channel         TEXT NOT NULL CHECK (channel IN ('all','online','pos','bancassurance')),
    in_scope        INTEGER NOT NULL,               -- 1 = fully guaranteed savings comparison set
    scope_reason    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS product_version (
    uin             TEXT PRIMARY KEY,               -- '104N124V17'
    product_id      TEXT NOT NULL REFERENCES product (product_id),
    version_no      INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('active','superseded','withdrawn','unverified')),
    is_latest_seen  INTEGER NOT NULL,
    seen_in_source  TEXT NOT NULL REFERENCES source_document (source_id),
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS variant (
    variant_id      TEXT PRIMARY KEY,               -- '<product_id>:<code>'
    product_id      TEXT NOT NULL REFERENCES product (product_id),
    rules_uin       TEXT REFERENCES product_version (uin),   -- version the rules were read from
    variant_code    TEXT NOT NULL,
    variant_name    TEXT NOT NULL,
    payout_pattern  TEXT NOT NULL CHECK (payout_pattern IN (
                        'lump_sum','fixed_income','fixed_income_rop','lifelong_income_rop',
                        'early_income_lump_sum','lifelong_income','pension_lifelong','unknown')),
    pcb_available   INTEGER,                        -- policy continuance benefit / waiver on death
    single_pay      INTEGER
);

-- Boundary conditions, materialised per PPT when the source gives a formula such as "65 - PPT".
CREATE TABLE IF NOT EXISTS eligibility_rule (
    rule_id         INTEGER PRIMARY KEY,
    variant_id      TEXT NOT NULL REFERENCES variant (variant_id),
    dimension       TEXT NOT NULL CHECK (dimension IN (
                        'entry_age','maturity_age','min_premium','sum_assured_on_death',
                        'pt_minus_ppt','max_policy_term')),
    ppt             INTEGER NOT NULL,               -- -1 all, 0 single
    pay_mode        TEXT NOT NULL CHECK (pay_mode IN ('any','single','annual','half_yearly','quarterly','monthly')),
    with_pcb        INTEGER,                        -- NULL = not applicable / either
    channel         TEXT NOT NULL CHECK (channel IN ('any','pos','online')),
    min_value       REAL,
    max_value       REAL,
    unit            TEXT NOT NULL CHECK (unit IN ('years','days','inr','multiple_of_ap','multiple_of_sp')),
    formula         TEXT,                           -- original wording, e.g. '65 - PPT'
    condition_note  TEXT,
    source_id       TEXT NOT NULL REFERENCES source_document (source_id),
    confidence      REAL NOT NULL DEFAULT 1.0
);

-- Allowed PPT x policy-term combinations and income-timing options.
CREATE TABLE IF NOT EXISTS term_option (
    term_option_id  INTEGER PRIMARY KEY,
    variant_id      TEXT NOT NULL REFERENCES variant (variant_id),
    ppt             INTEGER NOT NULL,               -- 0 single
    pt_min          INTEGER,
    pt_max          INTEGER,
    pt_list         TEXT,                           -- explicit discrete terms, comma-separated
    pt_rule         TEXT,                           -- e.g. 'ppt + deferment + income_period'
    deferment_list  TEXT,
    income_period_list TEXT,
    income_start_list  TEXT,
    source_id       TEXT NOT NULL REFERENCES source_document (source_id)
);

CREATE TABLE IF NOT EXISTS benefit_component (
    component_id    INTEGER PRIMARY KEY,
    variant_id      TEXT NOT NULL REFERENCES variant (variant_id),
    component       TEXT NOT NULL CHECK (component IN (
                        'maturity_sam','guaranteed_addition','income','loyalty_income_booster',
                        'money_back','return_of_premium','death_benefit','accidental_death_benefit',
                        'auto_debit_booster','high_premium_enhancement','discount','female_rate')),
    basis           TEXT NOT NULL CHECK (basis IN (
                        'pct_of_income','pct_of_sam','pct_of_total_premiums','pct_of_ap',
                        'multiple_of_ap','multiple_of_sp','pct_of_premiums_paid','pct_of_sa_death','rate_table',
                        'age_setback_years','absolute_inr','text')),
    value           REAL,
    timing          TEXT,
    condition_note  TEXT,
    source_id       TEXT NOT NULL REFERENCES source_document (source_id)
);

CREATE TABLE IF NOT EXISTS premium_band (
    product_id      TEXT NOT NULL REFERENCES product (product_id),
    band_no         INTEGER NOT NULL,
    basis           TEXT NOT NULL CHECK (basis IN ('annualised_premium','gsam')),
    min_amount      REAL NOT NULL,
    max_amount      REAL,                           -- NULL = open-ended
    source_id       TEXT NOT NULL REFERENCES source_document (source_id),
    PRIMARY KEY (product_id, band_no)
);

CREATE TABLE IF NOT EXISTS modal_factor (
    product_id      TEXT NOT NULL REFERENCES product (product_id),
    pay_mode        TEXT NOT NULL CHECK (pay_mode IN ('annual','half_yearly','quarterly','monthly')),
    factor          REAL NOT NULL,
    source_id       TEXT NOT NULL REFERENCES source_document (source_id),
    PRIMARY KEY (product_id, pay_mode)
);

-- Actuarial factor grids. Parsed deterministically from rate-table PDFs, never transcribed by the LLM.
CREATE TABLE IF NOT EXISTS rate_factor (
    factor_id       INTEGER PRIMARY KEY,
    uin             TEXT NOT NULL REFERENCES product_version (uin),
    variant_id      TEXT REFERENCES variant (variant_id),
    factor_type     TEXT NOT NULL CHECK (factor_type IN (
                        'premium_per_1000_gsam','income_pct_ap','sam_pct_total_premium','ga_pct_sam')),
    gender          TEXT NOT NULL CHECK (gender IN ('M','F','U')),
    entry_age       INTEGER NOT NULL,
    ppt             INTEGER NOT NULL,               -- -1 when the table has no PPT dimension
    pt              INTEGER NOT NULL,               -- -1 when the table has no PT dimension
    band_no         INTEGER NOT NULL,
    value           REAL NOT NULL,
    source_id       TEXT NOT NULL REFERENCES source_document (source_id)
);

-- Worked examples: insurer benefit illustrations and third-party quotes, one row per persona.
CREATE TABLE IF NOT EXISTS illustration (
    illustration_id TEXT PRIMARY KEY,
    variant_id      TEXT NOT NULL REFERENCES variant (variant_id),
    source_kind     TEXT NOT NULL CHECK (source_kind IN ('insurer_illustration','aggregator_quote')),
    gender          TEXT NOT NULL CHECK (gender IN ('M','F','U')),
    entry_age       INTEGER NOT NULL,
    annual_premium  REAL NOT NULL,
    ppt             INTEGER NOT NULL,
    pt              INTEGER,
    life_cover      REAL,
    timing_basis    TEXT NOT NULL CHECK (timing_basis IN ('stated','inferred')),
    captured_on     TEXT NOT NULL,
    source_id       TEXT NOT NULL REFERENCES source_document (source_id),
    notes           TEXT
);

-- Long-format cash flows: one row per (illustration, year, flow type). Premiums negative.
CREATE TABLE IF NOT EXISTS illustration_cashflow (
    illustration_id TEXT NOT NULL REFERENCES illustration (illustration_id),
    policy_year     INTEGER NOT NULL,
    timing          TEXT NOT NULL CHECK (timing IN ('start','end')),
    flow_type       TEXT NOT NULL CHECK (flow_type IN (
                        'premium','income','loyalty_income_booster','maturity','money_back',
                        'return_of_premium','auto_debit_booster')),
    amount          REAL NOT NULL,
    PRIMARY KEY (illustration_id, policy_year, timing, flow_type)
);

-- Derived metrics. Recomputed by the loader; never extracted.
CREATE TABLE IF NOT EXISTS illustration_metric (
    illustration_id TEXT NOT NULL REFERENCES illustration (illustration_id),
    metric          TEXT NOT NULL CHECK (metric IN ('total_premium','total_benefit','benefit_multiple','irr','payback_year')),
    value           REAL,
    method          TEXT NOT NULL,
    PRIMARY KEY (illustration_id, metric)
);

-- Insurer-level quality metrics (public disclosures, insurer pages).
CREATE TABLE IF NOT EXISTS insurer_metric (
    insurer_id      TEXT NOT NULL REFERENCES insurer (insurer_id),
    period          TEXT NOT NULL,                  -- 'FY2025-26'
    metric          TEXT NOT NULL,
    value           REAL NOT NULL,
    unit            TEXT NOT NULL CHECK (unit IN ('pct','inr_cr','per_10k_policies','count','crore_lives')),
    scope_note      TEXT,
    source_id       TEXT NOT NULL REFERENCES source_document (source_id),
    PRIMARY KEY (insurer_id, period, metric)
);

-- Disagreements between or within sources. The chat layer must surface open ones.
CREATE TABLE IF NOT EXISTS data_conflict (
    conflict_id     INTEGER PRIMARY KEY,
    entity          TEXT NOT NULL,                  -- product_id / variant_id / illustration_id
    field           TEXT NOT NULL,
    value_a         TEXT NOT NULL,
    source_a        TEXT NOT NULL REFERENCES source_document (source_id),
    value_b         TEXT NOT NULL,
    source_b        TEXT NOT NULL REFERENCES source_document (source_id),
    resolution      TEXT NOT NULL CHECK (resolution IN ('open','used_a','used_b','both_valid_versions')),
    note            TEXT
);

CREATE INDEX IF NOT EXISTS ix_rule_variant ON eligibility_rule (variant_id, dimension, ppt);
CREATE INDEX IF NOT EXISTS ix_rate_lookup  ON rate_factor (uin, factor_type, gender, entry_age, band_no);
CREATE INDEX IF NOT EXISTS ix_cf_ill       ON illustration_cashflow (illustration_id);

-- ------------------------------------------------------------------ views for the chat layer
CREATE OR REPLACE VIEW v_product_catalog AS
SELECT p.product_id, i.name AS insurer, p.product_name, pv.uin AS latest_uin, pv.status,
       p.participation, p.guarantee_level, p.category, p.channel, p.in_scope, p.scope_reason,
       (SELECT COUNT(*) FROM variant v WHERE v.product_id = p.product_id) AS variant_count
FROM product p
JOIN insurer i ON i.insurer_id = p.insurer_id
LEFT JOIN product_version pv ON pv.product_id = p.product_id AND pv.is_latest_seen = 1;

CREATE OR REPLACE VIEW v_entry_age AS
SELECT v.variant_id, v.product_id, v.variant_name, r.ppt, r.with_pcb, r.channel,
       CASE WHEN r.unit = 'days' THEN r.min_value / 365.0 ELSE r.min_value END AS min_age_years,
       r.max_value AS max_age_years, r.formula, r.source_id
FROM eligibility_rule r
JOIN variant v ON v.variant_id = r.variant_id
WHERE r.dimension = 'entry_age';

CREATE OR REPLACE VIEW v_illustration_summary AS
SELECT il.illustration_id, p.product_name, v.variant_name, il.source_kind, il.gender, il.entry_age,
       il.annual_premium, il.ppt, il.pt,
       MAX(CASE WHEN m.metric = 'total_premium'    THEN m.value END) AS total_premium,
       MAX(CASE WHEN m.metric = 'total_benefit'    THEN m.value END) AS total_benefit,
       MAX(CASE WHEN m.metric = 'benefit_multiple' THEN m.value END) AS benefit_multiple,
       MAX(CASE WHEN m.metric = 'irr'              THEN m.value END) AS irr,
       il.timing_basis, sd.url AS source_url
FROM illustration il
JOIN variant v ON v.variant_id = il.variant_id
JOIN product p ON p.product_id = v.product_id
JOIN source_document sd ON sd.source_id = il.source_id
LEFT JOIN illustration_metric m ON m.illustration_id = il.illustration_id
GROUP BY il.illustration_id, p.product_name, v.variant_name, il.source_kind, il.gender, il.entry_age,
         il.annual_premium, il.ppt, il.pt, il.timing_basis, sd.url;

CREATE OR REPLACE VIEW v_insurer_scorecard AS
SELECT i.name AS insurer, m.period, m.metric, m.value, m.unit, m.scope_note, sd.url AS source_url
FROM insurer_metric m
JOIN insurer i ON i.insurer_id = m.insurer_id
JOIN source_document sd ON sd.source_id = m.source_id;

CREATE OR REPLACE VIEW v_open_conflicts AS
SELECT c.*, sa.url AS url_a, sb.url AS url_b
FROM data_conflict c
JOIN source_document sa ON sa.source_id = c.source_a
JOIN source_document sb ON sb.source_id = c.source_b
WHERE c.resolution <> 'both_valid_versions';
