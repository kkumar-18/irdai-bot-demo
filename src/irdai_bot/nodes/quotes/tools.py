"""The quote-comparison agent's tools: guaranteed-return life-insurance
product data (eligibility, rate lookups, illustrations, insurer scorecards)
read from the seeded warehouse (db/quotes_schema.sql + db/quotes_seed.sql,
ported from insurance-compare-agent/files/insurance_agent). Same shape as
nodes/analysis/tools.py's disclosure tools: every handler opens a read-only
Postgres session and, where the result is naturally row-shaped, returns
list[dict] so it grounds narration and charts exactly like a run_sql result
(see nodes/analysis/grounding.py, nodes/analysis/chart.py) with no extra
plumbing.

Illustrations are worked examples for one persona, not real-time quotes —
the seed data has no premium calculator behind it. rate_lookup is the one
tool that computes an actual premium, from a deterministically-loaded rate
table, and only for products/ages a table was loaded for.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Literal

import sqlglot
from langchain_core.tools import tool

from irdai_bot.db.connection import connect

logger = logging.getLogger(__name__)

# The only relations run_sql_products may read — the quote-domain analogue of
# nodes/analysis/tools.py's ALLOWED_RELATIONS/validate_query, kept as its own
# small copy rather than a shared parameterized helper: this is a distinct
# tool over a distinct schema (bound alongside, never instead of, the
# disclosure agent's own run_sql), and duplicating ~15 lines here is cheaper
# to reason about than threading a second allowlist through shared code that
# a disclosure-only test (test_sql_allowlist.py) already covers.
ALLOWED_RELATIONS = {
    "v_product_catalog",
    "v_entry_age",
    "v_illustration_summary",
    "v_insurer_scorecard",
    "v_open_conflicts",
}
DEFAULT_ROW_LIMIT = 200

SCHEMA_CARD = """
v_product_catalog(product_id, insurer, product_name, latest_uin, status, participation,
                   guarantee_level, category, channel, in_scope, scope_reason, variant_count)
  -- in_scope = 1: fully guaranteed, non-linked, non-participating savings/pension-savings —
  --   the comparison set. in_scope = 0 (participating or linked, or an annuity) is still
  --   listed, with scope_reason explaining why it's excluded from guaranteed-return comparisons.
v_entry_age(variant_id, product_id, variant_name, ppt, with_pcb, channel,
            min_age_years, max_age_years, formula, source_id)
v_illustration_summary(illustration_id, product_name, variant_name, source_kind, gender,
                        entry_age, annual_premium, ppt, pt, total_premium, total_benefit,
                        benefit_multiple, irr, timing_basis, source_url)
  -- source_kind: 'insurer_illustration' | 'aggregator_quote' (a third-party display — label it
  --   as such). Each row is one worked example for one persona, not a live quote.
v_insurer_scorecard(insurer, period, metric, value, unit, scope_note, source_url)
v_open_conflicts(conflict_id, entity, field, value_a, source_a, value_b, source_b,
                  resolution, note, url_a, url_b)
  -- Known disagreements (within a document, between UIN versions, or against an aggregator)
  -- that are still open. Check before quoting/comparing a product; mention any that apply.
-- PostgreSQL dialect. Money is INR, ex-GST. ppt = 0 is single pay, ppt = -1 means "all PPTs".
""".strip()

TOOL_CARD = """
  - "Which plans can I buy at age X with Rs Y a year?" -> check_eligibility, then
    get_product_rules for the plans that pass.
  - "Compare returns / IRR" -> compare_illustrations.
  - "Is insurer A reliable?" -> insurer_scorecard (curated reference metrics — for the
    authoritative regulatory figures from actual IRDAI filings, prefer the disclosure tools
    and run_sql over facts/the v_expense_ratio-style views instead).
  - Product details (variants, terms, benefits, bands) -> get_product_rules.
  - A specific premium from a loaded rate table -> rate_lookup.
  - Anything else over the catalogue/eligibility/illustration/scorecard/conflict views ->
    run_sql_products, with a LIMIT.
""".strip()


class QueryRejected(ValueError):
    pass


def validate_query(query: str) -> str:
    try:
        statements = sqlglot.parse(query, read="postgres")
    except sqlglot.errors.ParseError as e:
        raise QueryRejected(f"Could not parse SQL: {e}") from e
    if len(statements) != 1:
        raise QueryRejected("Only a single SELECT statement is allowed per call.")
    stmt = statements[0]
    if stmt is None or stmt.key != "select":
        raise QueryRejected("Only SELECT statements are allowed.")
    cte_names = {c.alias_or_name.lower() for c in stmt.find_all(sqlglot.exp.CTE)}
    tables = {t.name.lower() for t in stmt.find_all(sqlglot.exp.Table)} - cte_names
    disallowed = tables - ALLOWED_RELATIONS
    if disallowed:
        raise QueryRejected(f"Query references disallowed relation(s): {sorted(disallowed)}")
    if not stmt.args.get("limit"):
        query = f"{query.rstrip(';')} LIMIT {DEFAULT_ROW_LIMIT}"
    return query


def _json_safe(value):
    return float(value) if isinstance(value, Decimal) else value


def _q(con, sql: str, *params) -> list[dict]:
    cursor = con.execute(sql, params)
    columns = [c[0] for c in cursor.description]
    return [{c: _json_safe(v) for c, v in zip(columns, row)} for row in cursor.fetchall()]


def make_list_products_tool(database_url: str):
    @tool
    def list_products(insurer: Literal["any", "Axis Max Life", "HDFC Life"], in_scope_only: bool) -> list[dict]:
        """Product catalogue with latest UIN, participation, guarantee level and
        whether the product is in the guaranteed-savings comparison set."""
        with connect(database_url, read_only=True) as con:
            return _q(
                con,
                "SELECT * FROM v_product_catalog "
                "WHERE (%s = 'any' OR insurer LIKE %s || '%%') AND (%s = 0 OR in_scope = 1)",
                insurer,
                insurer,
                int(in_scope_only),
            )

    return list_products


def make_check_eligibility_tool(database_url: str):
    @tool
    def check_eligibility(
        age: int,
        ppt: int,
        policy_term: int,
        pay_mode: Literal["single", "annual", "half_yearly", "quarterly", "monthly"],
        instalment_premium: float,
        with_pcb: bool,
    ) -> list[dict]:
        """Check every in-scope product variant against entry age, maturity
        age, PPT/PT availability and minimum premium. ppt: 0 = single pay.
        policy_term: use -1 if unknown. instalment_premium: INR per
        instalment, ex-GST. with_pcb: policy continuance benefit option."""
        with connect(database_url, read_only=True) as con:
            out = []
            for v in _q(
                con,
                "SELECT v.variant_id, v.variant_name, p.product_name, p.product_id FROM variant v "
                "JOIN product p ON p.product_id = v.product_id WHERE p.in_scope = 1",
            ):
                reasons, unknown = [], []
                terms = _q(
                    con, "SELECT * FROM term_option WHERE variant_id = %s AND ppt = %s", v["variant_id"], ppt
                )
                if not _q(con, "SELECT 1 FROM term_option WHERE variant_id = %s", v["variant_id"]):
                    unknown.append("no term options loaded")
                elif not terms:
                    reasons.append(f"PPT {ppt} not offered")
                elif policy_term >= 0:
                    ok = any(
                        (t["pt_min"] is not None and t["pt_min"] <= policy_term <= (t["pt_max"] or 999))
                        or (t["pt_list"] and str(policy_term) in t["pt_list"].split(","))
                        or (t["pt_rule"] and t["pt_min"] is None)
                        for t in terms
                    )
                    if not ok:
                        reasons.append(f"policy term {policy_term} not offered with PPT {ppt}")
                rules = _q(
                    con,
                    "SELECT * FROM eligibility_rule WHERE variant_id = %s AND channel = 'any' "
                    "AND ppt IN (%s, -1) AND (with_pcb IS NULL OR with_pcb = %s)",
                    v["variant_id"],
                    ppt,
                    int(with_pcb),
                )
                entry = [r for r in rules if r["dimension"] == "entry_age"]
                if not entry:
                    unknown.append("entry-age rules not loaded")
                for r in entry:
                    lo = r["min_value"] / 365.0 if (r["unit"] == "days" and r["min_value"] is not None) else r["min_value"]
                    if lo is not None and age < lo:
                        reasons.append(f"entry age {age} < min {r['min_value']} {r['unit']}")
                    if r["max_value"] is not None and age > r["max_value"]:
                        reasons.append(f"entry age {age} > max {r['max_value']}" + (f" ({r['formula']})" if r["formula"] else ""))
                for r in (x for x in rules if x["dimension"] == "maturity_age"):
                    if policy_term < 0:
                        unknown.append("maturity age not checked (policy_term unknown)")
                        break
                    if r["max_value"] is not None and age + policy_term > r["max_value"]:
                        reasons.append(f"maturity age {age + policy_term} > max {r['max_value']}")
                for r in (x for x in rules if x["dimension"] == "min_premium" and x["pay_mode"] in (pay_mode, "any")):
                    if r["min_value"] is not None and instalment_premium < r["min_value"]:
                        reasons.append(f"{pay_mode} premium {instalment_premium:,.0f} < min {r['min_value']:,.0f}")
                status = "fail" if reasons else ("unknown" if unknown else "pass")
                out.append(
                    {
                        **v,
                        "status": status,
                        "reasons": sorted(set(reasons)),
                        "unknowns": sorted(set(unknown)),
                        "source_ids": sorted({r["source_id"] for r in rules}),
                    }
                )
            return out

    return check_eligibility


def make_get_product_rules_tool(database_url: str):
    @tool
    def get_product_rules(product_id: str) -> dict:
        """Variants, term options, benefit components, premium bands and
        modal factors for one product_id (from list_products/check_eligibility)."""
        with connect(database_url, read_only=True) as con:
            return {
                "product": _q(con, "SELECT * FROM v_product_catalog WHERE product_id = %s", product_id),
                "variants": _q(con, "SELECT * FROM variant WHERE product_id = %s", product_id),
                "term_options": _q(
                    con,
                    "SELECT t.* FROM term_option t JOIN variant v ON v.variant_id = t.variant_id "
                    "WHERE v.product_id = %s ORDER BY t.variant_id, t.ppt",
                    product_id,
                ),
                "benefits": _q(
                    con,
                    "SELECT b.*, s.url FROM benefit_component b JOIN variant v ON v.variant_id = b.variant_id "
                    "JOIN source_document s ON s.source_id = b.source_id WHERE v.product_id = %s",
                    product_id,
                ),
                "premium_bands": _q(con, "SELECT * FROM premium_band WHERE product_id = %s", product_id),
                "modal_factors": _q(con, "SELECT * FROM modal_factor WHERE product_id = %s", product_id),
            }

    return get_product_rules


def make_compare_illustrations_tool(database_url: str):
    @tool
    def compare_illustrations(product_ids: list[str], include_aggregator_quotes: bool) -> list[dict]:
        """Published worked examples with total premium, total benefit,
        benefit multiple, IRR and persona. Empty product_ids compares all in-scope products.
        Rows with source_kind 'aggregator_quote' are third-party displays — label them as such."""
        with connect(database_url, read_only=True) as con:
            rows = _q(
                con,
                "SELECT s.*, v.product_id FROM v_illustration_summary s "
                "JOIN illustration i ON i.illustration_id = s.illustration_id "
                "JOIN variant v ON v.variant_id = i.variant_id "
                "WHERE (%s = 1 OR s.source_kind = 'insurer_illustration') ORDER BY s.irr DESC",
                int(include_aggregator_quotes),
            )
            return [r for r in rows if not product_ids or r["product_id"] in product_ids]

    return compare_illustrations


def make_insurer_scorecard_tool(database_url: str):
    @tool
    def insurer_scorecard(period: str) -> list[dict]:
        """Curated insurer-level reference metrics (claims paid ratio,
        solvency, persistency, grievances...) captured alongside the product
        data. period '' = all periods. For the authoritative figures from
        actual IRDAI regulatory filings, prefer the disclosure agent's own
        tools (facts / v_expense_ratio etc.) instead."""
        with connect(database_url, read_only=True) as con:
            return _q(
                con,
                "SELECT * FROM v_insurer_scorecard WHERE (%s = '' OR period = %s) ORDER BY insurer, metric",
                period,
                period,
            )

    return insurer_scorecard


def make_rate_lookup_tool(database_url: str):
    @tool
    def rate_lookup(uin: str, entry_age: int, gsam: float, gender: Literal["M", "F", "U"]) -> dict:
        """Premium from a loaded rate table (premium per Rs 1,000 guaranteed
        sum assured on maturity). Excludes GST, modal loading, and underwriting
        extras. Returns an error if no rate table is loaded for this UIN/age/band."""
        with connect(database_url, read_only=True) as con:
            band = _q(
                con,
                "SELECT pb.band_no FROM premium_band pb JOIN product_version pv ON pv.product_id = pb.product_id "
                "WHERE pv.uin = %s AND pb.basis = 'gsam' AND %s >= pb.min_amount "
                "AND (pb.max_amount IS NULL OR %s <= pb.max_amount)",
                uin,
                gsam,
                gsam,
            )
            if not band:
                return {"error": "no GSAM band table loaded for this UIN"}
            rate = _q(
                con,
                "SELECT value, source_id FROM rate_factor WHERE uin = %s AND factor_type = 'premium_per_1000_gsam' "
                "AND entry_age = %s AND band_no = %s AND gender IN (%s, 'U')",
                uin,
                entry_age,
                band[0]["band_no"],
                gender,
            )
            if not rate:
                return {"error": f"no rate for age {entry_age} in band {band[0]['band_no']}"}
            return {
                "band_no": band[0]["band_no"],
                "rate_per_1000": rate[0]["value"],
                "annual_premium_ex_gst": round(rate[0]["value"] * gsam / 1000, 2),
                "source_id": rate[0]["source_id"],
            }

    return rate_lookup


def make_open_conflicts_tool(database_url: str):
    @tool
    def open_conflicts(entity_prefix: str) -> list[dict]:
        """Known data disagreements (within a document, between UIN versions,
        or against an aggregator) whose entity starts with the given prefix
        ('' = all). Call before comparing or quoting a product and mention
        any conflict that affects the answer."""
        with connect(database_url, read_only=True) as con:
            return _q(con, "SELECT * FROM v_open_conflicts WHERE entity LIKE %s || '%%'", entity_prefix)

    return open_conflicts


def make_run_sql_products_tool(database_url: str):
    @tool
    def run_sql_products(query: str) -> list[dict]:
        """Run a read-only SELECT over the quote-comparison views
        (v_product_catalog, v_entry_age, v_illustration_summary,
        v_insurer_scorecard, v_open_conflicts). Only a single SELECT
        statement is permitted; max 200 rows."""
        logger.info("run_sql_products: %s", query)
        try:
            safe_query = validate_query(query)
            with connect(database_url, read_only=True) as con:
                # No params tuple here (unlike _q's other callers): this
                # query is agent-written and may contain a literal '%' (e.g.
                # a LIKE pattern) that isn't a real placeholder — passing
                # even an empty params tuple would make psycopg require it
                # to be escaped as '%%'. Matches
                # nodes/analysis/tools.py:make_run_sql_tool exactly.
                cursor = con.execute(safe_query)
                columns = [c[0] for c in cursor.description]
                rows = [{c: _json_safe(v) for c, v in zip(columns, row)} for row in cursor.fetchall()]
                logger.info("run_sql_products -> %d rows", len(rows))
                return rows
        except QueryRejected as e:
            logger.warning("run_sql_products rejected: %s", e)
            return [{"error": str(e)}]
        except Exception as e:
            logger.warning("run_sql_products failed: %s", e)
            return [{"error": f"Query failed: {e}"}]

    return run_sql_products


def make_quote_tools(database_url: str):
    return [
        make_list_products_tool(database_url),
        make_check_eligibility_tool(database_url),
        make_get_product_rules_tool(database_url),
        make_compare_illustrations_tool(database_url),
        make_insurer_scorecard_tool(database_url),
        make_rate_lookup_tool(database_url),
        make_open_conflicts_tool(database_url),
        make_run_sql_products_tool(database_url),
    ]
