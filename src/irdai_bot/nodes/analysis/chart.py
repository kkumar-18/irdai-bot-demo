"""Derives a chart spec from a chat turn's run_sql results, for the frontend
to render — a pure function over plain dicts/lists so it's trivially
testable against the exact row shapes facts/the derived views return (see
tests/test_chart.py), no LLM call involved: the schema is small and regular
enough (see nodes/analysis/tools.py:SCHEMA_CARD) that a deterministic
heuristic is more reliable than asking a model to describe its own chart.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal, TypedDict

X_KEY_PRIORITY = ("fy", "period")
SERIES_KEY_PRIORITY = ("insurer", "segment")
# Confirmed live: a "compare HDFC Life and Axis Max Life" question often gets
# answered with one row per fy and the two insurers pivoted into separate
# named columns (hdfc_life_percent, axis_max_life_percent) rather than one
# row per (fy, insurer) — melted back into that long shape below, before the
# x/value/series picking logic runs, so it charts like any other result.
_ENTITY_PREFIX_RE = re.compile(r"^(hdfc_life|axis_max_life)_(.+)$")
# Matches a single-insurer equality filter in a run_sql WHERE clause (not an
# IN (...) list — that means the agent needs to distinguish insurers, so the
# result rows should already carry their own `insurer` column instead).
_SINGLE_INSURER_FILTER_RE = re.compile(r"insurer\s*=\s*'([a-z_]+)'", re.IGNORECASE)


def infer_single_insurer_from_query(query: str | None) -> str | None:
    if not query:
        return None
    matches = {m.lower() for m in _SINGLE_INSURER_FILTER_RE.findall(query)}
    return matches.pop() if len(matches) == 1 else None
_NON_VALUE_KEYS = {
    "insurer", "fy", "period", "segment", "form", "unit", "declared_scale",
    "is_restated", "source_sha256", "source_locator", "raw_row_label",
    "raw_col_label", "extracted_at", "confidence", "line_item",
    # disclosure_values provenance/metadata — page_number in particular is
    # numeric and must never be charted as a value.
    "schema_version", "form_key", "form_heading", "table_title", "row_label", "column_label",
    "measure", "period_end", "scale", "raw_value", "source_url", "page_number", "extraction_method",
}
# A category axis (e.g. channel) replaces fiscal year when the rows cover a
# single year — FY2012-13 channel mix charted as one bar group per channel,
# not a single meaningless "Value" bar.
_MAX_CATEGORIES = 20


class ChartSeries(TypedDict):
    key: str
    label: str


class ChartSpec(TypedDict):
    type: Literal["line", "bar"]
    title: str
    x_key: str
    x_label: str
    y_label: str
    series: list[ChartSeries]
    data: list[dict[str, Any]]


def _melt_entity_pivoted_columns(rows: list[dict]) -> list[dict] | None:
    """None when no column matches the {insurer}_{metric} shape (caller uses
    `rows` untouched), OR when only SOME numeric columns match it (safer not
    to guess which are which than to melt part of the row and silently drop
    the rest)."""
    if not rows:
        return None
    common_keys = set.intersection(*(set(r.keys()) for r in rows))
    numeric_candidates = [
        k for k in rows[0]
        if k in common_keys and k not in _NON_VALUE_KEYS
        and all(isinstance(r.get(k), (int, float)) for r in rows)
    ]
    matches = [
        (k, m.group(1), m.group(2))
        for k in numeric_candidates
        if (m := _ENTITY_PREFIX_RE.match(k)) is not None
    ]
    matched_keys = {k for k, _, _ in matches}
    if len(matches) < 2 or matched_keys != set(numeric_candidates):
        return None
    other_keys = [k for k in rows[0] if k not in matched_keys]
    melted = [
        {**{k: row[k] for k in other_keys}, "insurer": insurer, "metric": metric, "value": row[key]}
        for row in rows
        for key, insurer, metric in matches
        if row.get(key) is not None
    ]
    return melted or None


def _flatten_dedupe(turn_results: list[list[dict]]) -> list[dict]:
    # A turn often runs several queries before landing on a final one — a
    # failed attempt (run_sql reports failure as a single {"error": ...}
    # row, see nodes/analysis/tools.py, rather than raising), an earlier
    # exploratory query, then a final query with a *different* column shape
    # once the agent adds e.g. a YoY window function. Confirmed live, twice:
    # naively merging every row from every query in the turn made the "keys
    # common to every row" intersection collapse to nothing whenever any
    # query's shape differed from the rest, killing the chart outright.
    #
    # Fix: take the LAST successful query's column shape as canonical (the
    # agent's own final answer is built from it, so it's the one that
    # matters), and merge in only the OTHER queries this turn that share
    # that exact shape — this still correctly merges e.g. two
    # same-shaped per-insurer queries into one multi-series chart, while
    # ignoring earlier, differently-shaped attempts instead of being
    # poisoned by them.
    non_error = [[r for r in rs if "error" not in r] for rs in turn_results]
    non_error = [rs for rs in non_error if rs]
    if not non_error:
        return []
    final = non_error[-1]
    target_shape = frozenset(final[0].keys())
    # Same shape isn't enough to merge: confirmed live, the agent ran the same
    # query with and without a segment filter in parallel (10 grand_total
    # rows vs 180 all-segment rows). Merging those put 18 segments under each
    # (fy, insurer), several with a NULL ratio, and the chart vanished. An
    # earlier result set is merged only if it adds rows the final one doesn't
    # already cover (e.g. a separate query per insurer), never if it's
    # another attempt at the same rows.
    dims = sorted(target_shape & _NON_VALUE_KEYS)
    final_dims = {tuple(r.get(k) for k in dims) for r in final}

    seen: set[tuple] = set()
    rows: list[dict] = []
    for result_set in non_error:
        if frozenset(result_set[0].keys()) != target_shape:
            continue
        if result_set is not final and any(tuple(r.get(k) for k in dims) in final_dims for r in result_set):
            continue
        for row in result_set:
            key = tuple(sorted(row.items()))
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    return rows


def _pick_x_key(rows: list[dict]) -> str | None:
    common_keys = set.intersection(*(set(r.keys()) for r in rows)) if rows else set()
    time_key = next((k for k in X_KEY_PRIORITY if k in common_keys), None)
    if time_key and len({r[time_key] for r in rows}) > 1:
        return time_key
    for key in rows[0]:  # the query's own column order
        if key not in common_keys or key in SERIES_KEY_PRIORITY or key in X_KEY_PRIORITY:
            continue
        values = [r[key] for r in rows]
        if all(isinstance(v, str) for v in values) and 1 < len(set(values)) <= _MAX_CATEGORIES:
            return key
    return time_key


def _pick_value_key(rows: list[dict], x_key: str) -> str | None:
    common_keys = set.intersection(*(set(r.keys()) for r in rows))
    numeric_keys = [
        k
        for k in rows[0]  # the query's column order — a set's order varies run to run
        if k in common_keys
        and k not in _NON_VALUE_KEYS and k != x_key and all(isinstance(r.get(k), (int, float)) for r in rows)
    ]
    if not numeric_keys:
        return None
    if "value" in numeric_keys:
        return "value"
    ratio_keys = [k for k in numeric_keys if k.endswith("_ratio")]
    if ratio_keys:
        return ratio_keys[-1]
    return numeric_keys[-1]


def _pick_series_key(rows: list[dict]) -> str | None:
    common_keys = set.intersection(*(set(r.keys()) for r in rows))
    for candidate in SERIES_KEY_PRIORITY:
        if candidate not in common_keys:
            continue
        distinct = {r[candidate] for r in rows if r.get(candidate) is not None}
        distinct.discard("unspecified")
        if len(distinct) > 1:
            return candidate
    return None


def _humanize(key: str) -> str:
    return key.replace("_", " ").strip().title()


def _chart_from_rows(rows: list[dict], last_query: str | None) -> ChartSpec | None:
    if not rows:
        return None
    rows = _melt_entity_pivoted_columns(rows) or rows

    x_key = _pick_x_key(rows)
    if x_key is None:
        return None
    value_key = _pick_value_key(rows, x_key)
    if value_key is None:
        return None
    series_key = _pick_series_key(rows)
    # A single-insurer trend still carries "insurer" as a column even though
    # _pick_series_key declined it (only one distinct value, nothing to
    # split on) — use that value as the series identity anyway, rather than
    # falling back to the metric name, so the frontend colors it by that
    # insurer consistently with every multi-insurer chart in the same
    # session (color must follow the entity, never depend on whether this
    # particular question happened to compare insurers or not).
    single_insurer_fallback: str | None = None
    if not series_key:
        if rows[0].get("insurer"):
            single_insurer_fallback = str(rows[0]["insurer"])
        else:
            # Confirmed live: a single-insurer question's final query often
            # filters `WHERE insurer = 'hdfc_life'` without selecting the
            # column at all (no ambiguity to resolve within the result set),
            # so the rows carry no insurer identity to fall back to above —
            # recover it from the query text itself instead.
            single_insurer_fallback = infer_single_insurer_from_query(last_query)

    # Pivot long rows into one record per distinct x value, one column per series.
    pivoted: dict[Any, dict[str, Any]] = {}
    series_seen: dict[str, str] = {}  # key -> label, insertion order
    for row in rows:
        x_val = row.get(x_key)
        if x_val is None:
            continue
        record = pivoted.setdefault(x_val, {x_key: x_val})
        active_key = series_key
        s_key = str(row[active_key]) if active_key else (single_insurer_fallback or value_key)
        s_label = s_key if (active_key or single_insurer_fallback) else _humanize(value_key)
        if s_key in record and record[s_key] != row[value_key]:
            # Two different values for the same point: the rows mix measures
            # (confirmed live — disclosure_values rows carry policy counts and
            # ₹ premium under the same channel label) or dimensions the chart
            # can't show. Plotting whichever came last is a wrong chart; no
            # chart is better.
            return None
        series_seen.setdefault(s_key, s_label)
        record[s_key] = row[value_key]

    # Time axes sort chronologically; a category axis keeps the query's ORDER BY.
    data = [pivoted[x] for x in (sorted(pivoted) if x_key in X_KEY_PRIORITY else pivoted)]
    distinct_x = len(data)
    chart_type: Literal["line", "bar"] = "line" if x_key == "fy" and distinct_x >= 2 else "bar"

    return ChartSpec(
        type=chart_type,
        title=_humanize(value_key),
        x_key=x_key,
        x_label=_humanize(x_key),
        y_label=_humanize(value_key),
        series=[ChartSeries(key=k, label=v) for k, v in series_seen.items()],
        data=data,
    )


def derive_chart_spec(turn_results: list[list[dict]], last_query: str | None = None) -> ChartSpec | None:
    return _chart_from_rows(_flatten_dedupe(turn_results), last_query)


# A turn with several exploratory queries could otherwise flood one reply
# with charts; this is a generous cap for genuine multi-breakdown requests
# ("plot regular-premium and single-premium, by premium and by policy count"
# is exactly 4), not a realistic ceiling on normal use.
MAX_CHARTS_PER_TURN = 4


def _rows_fingerprint(rows: list[dict]) -> frozenset[str]:
    """A hashable, order-insensitive identity for one run_sql result set.

    Serialised rather than built out of the raw values: a cell can hold
    anything psycopg hands back, including an unhashable list (a Postgres
    array column, or a json/jsonb value) — `frozenset(tuple(r.items()) ...)`
    crashed on exactly that, confirmed live. A frozenset of per-row strings
    also sidesteps sorting rows against each other, which would compare a
    None (e.g. a regex-extracted column that didn't match for one row)
    against a sibling row's string — another crash confirmed live.
    `default=str` covers Decimal/date/UUID and anything else non-JSON.
    """
    return frozenset(json.dumps(r, sort_keys=True, default=str) for r in rows)


def derive_chart_specs(turn_results: list[list[dict]], last_query: str | None = None) -> list[ChartSpec]:
    """One chart per run_sql call this turn — for a "plot this in multiple
    graphs" request, where the agent (per SYSTEM_PROMPT) runs one
    self-contained query per breakdown. Deliberately NOT merged/deduped by
    row shape the way derive_chart_spec's single-chart path is: two
    genuinely different breakdowns (e.g. "by premium" vs. "by policy count"
    persistency) routinely produce IDENTICAL column shapes and even
    identical (insurer, duration) dimension values — shape/dims alone can't
    tell that apart from a duplicate retry of the SAME question, so trying
    to merge by shape here silently discarded 3 of 4 real breakdowns as
    "duplicates" of each other (confirmed live). The only dedup that's safe
    without that ambiguity: an EXACT repeat of an earlier call's rows (a
    parallel accidental re-issue of the same query), skipped so it isn't
    charted twice."""
    specs: list[ChartSpec] = []
    seen_fingerprints: set[frozenset[str]] = set()
    for result_set in turn_results:
        rows = [r for r in result_set if "error" not in r]
        if not rows:
            continue
        fingerprint = _rows_fingerprint(rows)
        if fingerprint in seen_fingerprints:
            continue
        seen_fingerprints.add(fingerprint)
        spec = _chart_from_rows(rows, last_query)
        if spec is not None:
            specs.append(spec)
        if len(specs) >= MAX_CHARTS_PER_TURN:
            break
    return specs
