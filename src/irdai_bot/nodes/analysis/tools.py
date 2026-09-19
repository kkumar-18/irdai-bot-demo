"""The comparison agent's tools: a SQL runner hard-restricted to the facts
table and the derived metric views, enforced structurally (allowlist +
single-SELECT parse check + a read-only Postgres session), not just via
prompt instruction — plus fetch_disclosures, which starts a background job
that fills gaps in that data from the insurers' own websites (see
irdai_bot/fetch.py and graphs/fetch_graph.py).
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Literal

import sqlglot
from langchain_core.tools import tool

from irdai_bot.config import Config
from irdai_bot.db.connection import connect
from irdai_bot.fetch import request_fetch
from irdai_bot.normalize.canon import all_form_numbers, form_info, line_item_ids
from irdai_bot.scope import FISCAL_YEARS, FORMS, is_fiscal_year, normalize_form

# The only two forms fetch_disclosures actually loads into facts (see
# SCHEMA_CARD) — the vocabulary for every other form belongs to
# disclosure_values' free-text row_label/measure instead, so listing their
# line_item ids here would just reinforce querying facts for a form that
# will never have rows there.
_FACTS_CURATED_FORMS = ("L-1-A-RA", "L-4")

logger = logging.getLogger(__name__)

ALLOWED_RELATIONS = {
    "facts",
    "v_expense_ratio",
    "v_commission_ratio",
    "v_claims_settled_ratio",
    "v_grievance_incidence",
    "v_solvency_ratio",
    "disclosure_values",
}
DEFAULT_ROW_LIMIT = 500

SCHEMA_CARD = """
facts(insurer, fy, period, form, segment, line_item, value, unit, declared_scale,
      is_restated, source_sha256, source_locator, raw_row_label, raw_col_label,
      extracted_at, confidence)
  -- insurer: 'hdfc_life' | 'axis_max_life'; fy: '2025-26' style; period: 'FY' | 'Q4';
  -- form: 'L-1-A-RA' | 'L-4' ONLY — these are the only two forms fetch_disclosures loads
  --   into facts (a real fixed-layout parser exists for just these two). NEVER query facts
  --   (or the v_* views below, which read from it) for L-22, L-32, L-40, L-41, or any other
  --   form — they will never have rows there; go straight to disclosure_values for those.
  -- segment: L-1-A-RA rows (and the expense/commission ratio views) are split by
  --   product segment; segment = 'grand_total' is the company-wide figure.
v_expense_ratio(insurer, fy, period, segment, operating_expenses, premiums_earned_net, expense_ratio)
v_commission_ratio(insurer, fy, period, segment, commission, premiums_earned_net, commission_ratio)
v_claims_settled_ratio(insurer, fy, period, segment, claims_paid, claims_booked, claims_settled_ratio)
v_grievance_incidence(insurer, fy, period, segment, complaints_per_10000_policies)
v_solvency_ratio(insurer, fy, period, segment, solvency_ratio)
  -- v_claims_settled_ratio/v_grievance_incidence/v_solvency_ratio (L-40/L-41/L-32) are almost
  --   always empty for this reason and are likely to stay empty after fetch_disclosures — if so,
  --   query disclosure_values for the underlying L-40/L-41/L-32 figures and compute the ratio
  --   yourself in SQL instead of relying on the view.

disclosure_values(insurer, fy, schema_version, form, form_key, form_heading, table_title,
                  row_label, column_label, measure, period, period_end, unit, scale,
                  raw_value, value, source_url, page_number)
  -- EVERY other form (L-1..L-45) and fiscal year, transcribed cell-by-cell as printed
  -- in the insurer's filing (fetched on demand; only the filing's own period).
  -- form: number only, 'L-38' (not 'L-1-A-RA'); schema_version: 'v2010' (periods before
  --   Sep 2021) | 'v2021' — line items within a form differ between the two;
  -- row_label / column_label / table_title are as printed, WORDED DIFFERENTLY by insurer
  --   and year (e.g. "Persistency Ratio: By Premium: For 13th month" vs "Persistency Ratio -
  --   Premium Basis (...): For 13th month" — the duration can appear before OR after other
  --   words). NEVER guess an ILIKE pattern's word order — always run an unfiltered
  --   SELECT DISTINCT row_label, column_label, measure FROM disclosure_values WHERE
  --   insurer=... AND fy=... AND form=... probe first, read the real strings back, THEN
  --   write a second query filtered on what you actually saw. A zero-row ILIKE-filtered
  --   query proves nothing about whether the data exists — only the unfiltered probe does.
  -- period: 'quarter' | 'year_to_date' | 'as_at' | 'other' (annual = 'year_to_date' in a
  --   March filing); unit: 'INR' | 'count' | 'percent' | 'ratio'; value is in base units
  --   (rupees for INR — divide by 1e7 for crore; counts as printed).
-- PostgreSQL dialect. value/ratio columns are double precision, so ROUND needs
-- a numeric cast: ROUND(expense_ratio::numeric, 4).
""".strip()


def _build_line_item_vocab() -> str:
    # Confirmed real reliability gap: with no guidance beyond column names,
    # the agent sometimes guessed plausible-but-wrong exact string values
    # for line_item/segment (e.g. 'Total Premium' instead of the actual
    # 'total_premium'), silently returning zero rows for an otherwise
    # perfectly good question. line_item values ARE the canonical ids
    # already defined once in normalize/canon/*.yaml (used for the ingest
    # label-mapping LLM) — surfaced here instead of duplicating the list.
    lines = []
    for form in _FACTS_CURATED_FORMS:
        try:
            ids = line_item_ids(form)
        except FileNotFoundError:
            continue
        lines.append(f"  {form}: {', '.join(ids)}")
    return "\n".join(lines)


LINE_ITEM_VOCAB = _build_line_item_vocab()


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
    # A CTE's own name shows up as an exp.Table wherever the query
    # references it (e.g. `WITH annual AS (...) SELECT ... FROM annual`) —
    # it's a query-local alias, not a real relation, so it must not be
    # checked against ALLOWED_RELATIONS. Confirmed live: without this
    # exclusion, any CTE-using query was rejected outright (a natural
    # pattern for YoY/window-function questions, so this broke often).
    cte_names = {c.alias_or_name.lower() for c in stmt.find_all(sqlglot.exp.CTE)}
    tables = {t.name.lower() for t in stmt.find_all(sqlglot.exp.Table)} - cte_names
    disallowed = tables - ALLOWED_RELATIONS
    if disallowed:
        raise QueryRejected(f"Query references disallowed relation(s): {sorted(disallowed)}")
    if not stmt.args.get("limit"):
        query = f"{query.rstrip(';')} LIMIT {DEFAULT_ROW_LIMIT}"
    return query


# A filtered disclosure_values query returning 0 rows proves nothing (row/
# column labels are worded differently by insurer and year — see
# SCHEMA_CARD above) but confirmed live: the model doesn't reliably follow
# that instruction from the system prompt alone across every turn. This is
# a second, structural line of defense — a hint injected directly into the
# TOOL RESULT the model reads next, which models act on far more reliably
# than a rule stated once, several thousand tokens earlier, in the system
# prompt.
_DISCLOSURE_VALUES_RE = re.compile(r"\bdisclosure_values\b", re.IGNORECASE)
_DISCLOSURE_FILTER_RE = re.compile(r"\b(row_label|column_label|measure)\s*(=|ilike|in\s*\()", re.IGNORECASE)
_DISCLOSURE_PROBE_HINT = (
    "0 rows — but this proves nothing on its own: disclosure_values row_label/column_label "
    "text is worded differently by insurer and year, so a filtered guess can easily miss real "
    "data. Before concluding it's missing, run an UNFILTERED probe: SELECT DISTINCT row_label, "
    "column_label, measure FROM disclosure_values WHERE insurer=... AND fy=... AND form=... "
    "(no row_label/column_label/measure condition), read the exact strings it returns, THEN "
    "write a new query filtered on those. Only if that probe ALSO returns 0 rows is the data "
    "actually missing (call fetch_disclosures for it)."
)


# Mirrors the disclosure_values hint below: `facts` returning 0 rows for L-22/
# L-32/L-40/L-41 (still listed for fetch_disclosures/VIEW_SOURCE_FORMS purposes
# — their figures DO exist, just not in facts) proves nothing about whether
# the data exists, only that it isn't in facts. Confirmed live: without this,
# the model sometimes retried facts. rather than ever trying disclosure_values.
_NON_CURATED_FORM_LITERAL_RE = re.compile(r"'(L-22|L-32|L-40|L-41)'", re.IGNORECASE)
_NON_CURATED_FORM_HINT = (
    "0 rows — expected: facts only ever holds L-1-A-RA and L-4 data (see the schema above), so "
    "this form will never have rows there no matter how the query is written. Query "
    "disclosure_values instead (start with the unfiltered DISTINCT probe described in its "
    "schema), or, if it's genuinely never been fetched, call fetch_disclosures for it."
)


def _json_safe(value):
    # Postgres returns NUMERIC results (ROUND(x::numeric, 2), numeric
    # arithmetic) as Decimal, which json.dumps can't serialize — ToolNode would
    # then fall back to str() and the grounding check / chart derivation, which
    # json.loads the tool result, would silently see no rows at all.
    return float(value) if isinstance(value, Decimal) else value


def make_run_sql_tool(database_url: str):
    @tool
    def run_sql(query: str) -> list[dict]:
        """Run a read-only SELECT against the facts table or one of the
        derived metric views (v_expense_ratio, v_commission_ratio,
        v_claims_settled_ratio, v_grievance_incidence, v_solvency_ratio).
        Only a single SELECT statement is permitted."""
        # Caught here (not left to ToolNode's default error handling, whose
        # catch-vs-reraise behavior isn't guaranteed stable across langgraph
        # versions — confirmed live: an earlier QueryRejected here crashed
        # the whole HTTP request instead of reaching the agent) so a bad
        # query always comes back as a normal tool result the agent can see
        # and self-correct from, never a 500.
        logger.info("run_sql: %s", query)
        try:
            safe_query = validate_query(query)
            with connect(database_url, read_only=True) as con:
                cursor = con.execute(safe_query)
                columns = [c[0] for c in cursor.description]
                rows = [{c: _json_safe(v) for c, v in zip(columns, row)} for row in cursor.fetchall()]
                logger.info("run_sql -> %d rows", len(rows))
                if not rows and _DISCLOSURE_VALUES_RE.search(safe_query) and _DISCLOSURE_FILTER_RE.search(safe_query):
                    return [{"hint": _DISCLOSURE_PROBE_HINT}]
                if not rows and not _DISCLOSURE_VALUES_RE.search(safe_query) and _NON_CURATED_FORM_LITERAL_RE.search(safe_query):
                    return [{"hint": _NON_CURATED_FORM_HINT}]
                return rows
        except QueryRejected as e:
            logger.warning("run_sql rejected: %s", e)
            return [{"error": str(e)}]
        except Exception as e:  # postgres errors (bad column name, syntax, etc.)
            logger.warning("run_sql failed: %s", e)
            return [{"error": f"Query failed: {e}"}]

    return run_sql


# Which form each derived view is computed from — what fetch_disclosures needs
# to be asked for when a view comes back empty.
VIEW_SOURCE_FORMS = {
    "v_expense_ratio": "L-1-A-RA",
    "v_commission_ratio": "L-1-A-RA",
    "v_claims_settled_ratio": "L-40",
    "v_grievance_incidence": "L-41",
    "v_solvency_ratio": "L-32",
}


FORMS_CARD = "\n".join(
    f"  L-{n}: {form_info(n)['title']}" + ("  (only from FY2021-22)" if form_info(n)["versions"] == ["v2021"] else "")
    for n in all_form_numbers()
)


def make_fetch_disclosures_tool(cfg: Config):
    @tool
    def fetch_disclosures(
        insurer: Literal["hdfc_life", "axis_max_life"],
        fiscal_years: list[str],
        forms: list[str],
    ) -> dict:
        """Start fetching IRDAI public-disclosure data from the insurer's own
        website into the database, in the background: any form L-1..L-45,
        any fiscal year the website publishes (HDFC Life from FY2005-06,
        Axis Max Life from FY2010-11). Use when run_sql found the data in
        neither facts/views nor disclosure_values. fiscal_years look like
        '2012-13'; forms are form codes such as 'L-38' or 'L-1-A-RA'.
        Returns immediately — the data is NOT available during this turn.
        status 'fetching_in_background': only what's listed under `fetching`
        is being fetched — tell the user that and that they can press Reload
        shortly; answer with whatever is already available. With either
        status, anything under unavailable_already_checked_on_website was
        recently looked for and isn't published in a form this system can
        read — say it's unavailable (not that it's being fetched); anything
        under already_available can be queried with run_sql now; anything
        under form_did_not_exist_that_year was not part of the disclosure
        framework for that year — say so."""
        # Returns a dict, never a list: the grounding check and chart
        # derivation treat list-shaped tool results as run_sql rows.
        fiscal_years = fiscal_years or list(FISCAL_YEARS)
        forms = forms or list(FORMS)
        invalid = sorted(fy for fy in set(fiscal_years) if not is_fiscal_year(fy)) + sorted(
            f for f in set(forms) if normalize_form(f) is None
        )
        if invalid:
            return {
                "error": f"Not a fiscal year ('2012-13' style) or form code (L-1..L-45): {invalid}."
            }
        try:
            return request_fetch(
                cfg,
                insurer=insurer,
                fiscal_years=sorted(set(fiscal_years)),
                forms=sorted({normalize_form(f) for f in forms}, key=lambda c: int(c[2:])),
            )
        except Exception as e:  # database unavailable, etc. — scrape failures surface on the job itself
            logger.exception("fetch_disclosures failed")
            return {"error": f"Couldn't start fetching from the insurer's website: {e}"}

    return fetch_disclosures
