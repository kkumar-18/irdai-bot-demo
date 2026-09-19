-- Derived metric views, computed once here so the comparison agent never
-- derives ratio formulas itself (it only ever SELECTs from facts/these views).

-- Every two-sided ratio pairs figures from the SAME filing (source_sha256):
-- a fiscal year can be present from more than one filing, and matching on
-- fy alone divided one filing's numerator by another filing's denominator.

-- Net premium per L-1-A-RA filing column. HDFC prints it as the premiums
-- section's "Sub Total" row; Axis Max Life doesn't print that row, so it's
-- summed from the same components (gross premium + reinsurance ceded, stored
-- negative + reinsurance accepted) — identical to HDFC's own Sub Total where
-- both exist (e.g. FY2025-26 grand total: 79,38,707 - 2,07,185 = 77,31,522 lakh).
-- Helper only, deliberately not in run_sql's ALLOWED_RELATIONS.
CREATE OR REPLACE VIEW l1_net_premium AS
SELECT
    insurer, fy, period, segment, source_sha256,
    COALESCE(
        MAX(value) FILTER (WHERE line_item = 'premiums_earned_net'),
        CASE WHEN COUNT(*) FILTER (WHERE line_item = 'premium_income_gross') > 0 THEN
            SUM(value) FILTER (WHERE line_item IN ('premium_income_gross', 'reinsurance_ceded', 'reinsurance_accepted'))
        END
    ) AS premiums_earned_net
FROM facts
WHERE form = 'L-1-A-RA'
GROUP BY insurer, fy, period, segment, source_sha256;

CREATE OR REPLACE VIEW v_expense_ratio AS
SELECT
    e.insurer, e.fy, e.period, e.segment,
    e.value AS operating_expenses,
    p.premiums_earned_net,
    CASE WHEN p.premiums_earned_net != 0 THEN e.value / p.premiums_earned_net END AS expense_ratio
FROM facts e
JOIN l1_net_premium p
    ON e.insurer = p.insurer AND e.fy = p.fy AND e.period = p.period
   AND e.segment = p.segment AND e.source_sha256 = p.source_sha256
WHERE e.form = 'L-1-A-RA' AND e.line_item = 'operating_expenses';

CREATE OR REPLACE VIEW v_commission_ratio AS
SELECT
    c.insurer, c.fy, c.period, c.segment,
    c.value AS commission,
    p.premiums_earned_net,
    CASE WHEN p.premiums_earned_net != 0 THEN c.value / p.premiums_earned_net END AS commission_ratio
FROM facts c
JOIN l1_net_premium p
    ON c.insurer = p.insurer AND c.fy = p.fy AND c.period = p.period
   AND c.segment = p.segment AND c.source_sha256 = p.source_sha256
WHERE c.form = 'L-1-A-RA' AND c.line_item = 'commission';

CREATE OR REPLACE VIEW v_claims_settled_ratio AS
SELECT
    paid.insurer, paid.fy, paid.period, paid.segment,
    paid.value AS claims_paid,
    booked.value AS claims_booked,
    CASE WHEN booked.value != 0 THEN paid.value / booked.value END AS claims_settled_ratio
FROM facts paid
JOIN facts booked
    ON paid.insurer = booked.insurer AND paid.fy = booked.fy AND paid.period = booked.period
   AND paid.segment = booked.segment AND paid.form = booked.form
   AND paid.source_sha256 = booked.source_sha256
WHERE paid.form = 'L-40' AND paid.line_item = 'claims_paid'
  AND booked.form = 'L-40' AND booked.line_item = 'claims_booked';

-- L-41 already files this ratio directly (policy complaints per 10,000
-- policies) — passthrough rather than re-derive it, so the agent can compare
-- insurers' own reported grievance incidence.
CREATE OR REPLACE VIEW v_grievance_incidence AS
SELECT insurer, fy, period, segment, value AS complaints_per_10000_policies
FROM facts
WHERE form = 'L-41' AND line_item = 'policy_complaints_per_10000_policies';

-- L-32 already files the solvency ratio directly — passthrough.
CREATE OR REPLACE VIEW v_solvency_ratio AS
SELECT insurer, fy, period, segment, value AS solvency_ratio
FROM facts
WHERE form = 'L-32' AND line_item = 'solvency_ratio';
