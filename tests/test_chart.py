from irdai_bot.nodes.analysis.chart import derive_chart_spec, derive_chart_specs, infer_single_insurer_from_query

# Row shapes matching what facts/the derived views actually return
# (db/schema.sql, db/views.sql) — verified against the real warehouse.

TREND_ROWS = [
    {"insurer": "hdfc_life", "fy": "2018-19", "value": 291860241000.0},
    {"insurer": "hdfc_life", "fy": "2019-20", "value": 327068938000.0},
    {"insurer": "hdfc_life", "fy": "2020-21", "value": 385835000000.0},
]

TWO_INSURER_TWO_YEAR_ROWS = [
    {"insurer": "hdfc_life", "fy": "2023-24", "value": 630764800000.0},
    {"insurer": "hdfc_life", "fy": "2024-25", "value": 710449100000.0},
    {"insurer": "axis_max_life", "fy": "2023-24", "value": 239827000000.0},
    {"insurer": "axis_max_life", "fy": "2024-25", "value": 314489000000.0},
]

EXPENSE_RATIO_ROWS = [
    {"insurer": "hdfc_life", "fy": "2023-24", "period": "FY", "segment": "grand_total",
     "operating_expenses": 769091000.0, "premiums_earned_net": 7731522000.0, "expense_ratio": 0.0995},
    {"insurer": "hdfc_life", "fy": "2024-25", "period": "FY", "segment": "grand_total",
     "operating_expenses": 800000000.0, "premiums_earned_net": 8100000000.0, "expense_ratio": 0.0988},
]


def test_single_insurer_trend_is_a_line_chart():
    spec = derive_chart_spec([TREND_ROWS])
    assert spec is not None
    assert spec["type"] == "line"
    assert spec["x_key"] == "fy"
    # Only one distinct insurer, so it's not used to SPLIT into multiple
    # series — but it's still used as that single series' identity (not the
    # generic metric name), so this trend colors the same as HDFC Life does
    # in every other chart this session (color follows the entity).
    assert [s["key"] for s in spec["series"]] == ["hdfc_life"]
    assert len(spec["data"]) == 3
    assert spec["data"][0]["fy"] == "2018-19"
    assert spec["data"][0]["hdfc_life"] == 291860241000.0


def test_two_insurer_comparison_has_both_series():
    spec = derive_chart_spec([TWO_INSURER_TWO_YEAR_ROWS])
    assert spec is not None
    assert {s["key"] for s in spec["series"]} == {"hdfc_life", "axis_max_life"}
    assert len(spec["data"]) == 2  # two fiscal years, one record each with both series
    row_2024 = next(r for r in spec["data"] if r["fy"] == "2024-25")
    assert row_2024["hdfc_life"] == 710449100000.0
    assert row_2024["axis_max_life"] == 314489000000.0


def test_derived_ratio_view_prefers_ratio_column():
    spec = derive_chart_spec([EXPENSE_RATIO_ROWS])
    assert spec is not None
    assert spec["title"] == "Expense Ratio"
    assert spec["y_label"] == "Expense Ratio"


def test_merges_multiple_tool_calls_in_one_turn():
    hdfc_only = [TWO_INSURER_TWO_YEAR_ROWS[0], TWO_INSURER_TWO_YEAR_ROWS[1]]
    axis_only = [TWO_INSURER_TWO_YEAR_ROWS[2], TWO_INSURER_TWO_YEAR_ROWS[3]]
    spec = derive_chart_spec([hdfc_only, axis_only])
    assert spec is not None
    assert {s["key"] for s in spec["series"]} == {"hdfc_life", "axis_max_life"}


def test_final_query_shape_wins_over_earlier_differently_shaped_attempts():
    # Real bug (2nd occurrence, live): a turn's queries progressively change
    # shape — an exploratory query with extra columns, then a final CTE
    # query aliasing the value column differently and adding a YoY column —
    # and naive merging across all of them collapsed the common-keys
    # intersection to nothing. The FINAL query's shape should win, using
    # only rows sharing that shape.
    exploratory = [
        {"fy": "2018-19", "value": 291860241000.0, "unit": "INR", "declared_scale": "lakh"},
    ]
    broader_exploratory = [
        {"fy": "2018-19", "value": 291860241000.0, "unit": "INR", "declared_scale": "lakh",
         "is_restated": False, "source_locator": "page=9"},
    ]
    final = [
        {"fy": "2018-19", "total_premium": 291860241000.0, "yoy_growth_pct": None},
        {"fy": "2019-20", "total_premium": 327068938000.0, "yoy_growth_pct": 12.06},
    ]
    spec = derive_chart_spec([exploratory, broader_exploratory, final])
    assert spec is not None
    assert spec["x_key"] == "fy"
    assert len(spec["data"]) == 2
    assert spec["data"][1]["fy"] == "2019-20"


def test_error_row_from_a_failed_query_does_not_poison_the_chart():
    # Real bug: a turn where one query succeeds and a second (e.g. a
    # follow-up YoY-growth attempt) fails — run_sql reports the failure as
    # {"error": ...} rather than raising — used to make the whole turn
    # non-chartable, because the error row's different shape shrank the
    # "keys common to every row" intersection to nothing.
    spec = derive_chart_spec([TREND_ROWS, [{"error": "Query failed: syntax error"}]])
    assert spec is not None
    assert spec["type"] == "line"


def test_single_insurer_query_without_insurer_column_still_colors_by_entity():
    # Real bug: a single-insurer question's final query filters
    # `WHERE insurer = 'hdfc_life'` without SELECTing the column at all (no
    # ambiguity to resolve within one insurer's own result set), so the rows
    # carry no insurer identity — recovered from the query text instead of
    # falling back to the generic metric name.
    rows_without_insurer_column = [{"fy": "2018-19", "value": 291860241000.0}]
    query = "SELECT fy, value FROM facts WHERE insurer = 'hdfc_life' AND line_item = 'total_premium'"
    spec = derive_chart_spec([rows_without_insurer_column], last_query=query)
    assert spec is not None
    assert [s["key"] for s in spec["series"]] == ["hdfc_life"]


def test_infer_single_insurer_from_query():
    assert infer_single_insurer_from_query("... WHERE insurer = 'hdfc_life' ...") == "hdfc_life"
    assert infer_single_insurer_from_query("... WHERE insurer IN ('hdfc_life', 'axis_max_life') ...") is None
    assert infer_single_insurer_from_query(None) is None
    assert infer_single_insurer_from_query("SELECT 1") is None


def test_non_chartable_result_returns_none():
    assert derive_chart_spec([]) is None
    assert derive_chart_spec([[{"count": 6}]]) is None  # no fy/period x-axis
    assert derive_chart_spec([[{"fy": "2025-26", "form": "L-4"}]]) is None  # no numeric value


def test_same_shaped_alternative_attempt_does_not_merge_into_final_chart():
    # Confirmed live: the agent ran one query filtered to grand_total and the
    # same query across every segment; both had identical columns, merging
    # them mixed in NULL-ratio segment rows and the chart disappeared.
    def row(insurer, fy, segment, ratio):
        return {"insurer": insurer, "fy": fy, "period": "FY", "segment": segment, "expense_ratio": ratio}

    grand_total = [row(i, fy, "grand_total", r) for i, fy, r in
                   [("hdfc_life", "2024-25", 0.0894), ("axis_max_life", "2024-25", 0.1385),
                    ("hdfc_life", "2025-26", 0.0995), ("axis_max_life", "2025-26", 0.1486)]]
    all_segments = grand_total + [row("hdfc_life", "2025-26", "par_health", None)]
    spec = derive_chart_spec([all_segments, grand_total])
    assert spec is not None
    assert {s["key"] for s in spec["series"]} == {"hdfc_life", "axis_max_life"}
    assert spec["data"][-1] == {"fy": "2025-26", "hdfc_life": 0.0995, "axis_max_life": 0.1486}


def test_single_year_breakdown_charts_categories_not_one_fy_bar():
    # Real case: FY2012-13 channel mix from disclosure_values came out as a
    # single "Value" bar at x = "2012-13".
    rows = [
        {"insurer": "hdfc_life", "fy": "2012-13", "channel": "Corporate Agents-Banks", "premium_crore": 2294.0, "share_pct": 69.64, "page_number": 80},
        {"insurer": "hdfc_life", "fy": "2012-13", "channel": "Individual agents", "premium_crore": 547.0, "share_pct": 16.61, "page_number": 80},
        {"insurer": "axis_max_life", "fy": "2012-13", "channel": "Corporate Agents-Banks", "premium_crore": 817.0, "share_pct": 47.61, "page_number": 34},
        {"insurer": "axis_max_life", "fy": "2012-13", "channel": "Individual agents", "premium_crore": 593.0, "share_pct": 34.56, "page_number": 34},
    ]
    spec = derive_chart_spec([rows])
    assert spec is not None
    assert spec["type"] == "bar" and spec["x_key"] == "channel"
    assert spec["y_label"] == "Share Pct"  # never page_number
    assert spec["data"][0] == {"channel": "Corporate Agents-Banks", "hdfc_life": 69.64, "axis_max_life": 47.61}
    assert {s["key"] for s in spec["series"]} == {"hdfc_life", "axis_max_life"}


def test_rows_mixing_measures_get_no_chart():
    # Real case: raw disclosure_values rows for one channel carried both the
    # policy count and the ₹ premium; the chart plotted whichever came last.
    rows = [
        {"insurer": "hdfc_life", "fy": "2012-13", "row_label": "Corporate Agents-Banks", "measure": "number of policies", "value": 479640.0},
        {"insurer": "hdfc_life", "fy": "2012-13", "row_label": "Corporate Agents-Banks", "measure": "premium", "value": 22940000000.0},
        {"insurer": "hdfc_life", "fy": "2012-13", "row_label": "Individual agents", "measure": "number of policies", "value": 374619.0},
        {"insurer": "hdfc_life", "fy": "2012-13", "row_label": "Individual agents", "measure": "premium", "value": 5470000000.0},
    ]
    assert derive_chart_spec([rows]) is None


def test_single_metric_entity_pivoted_columns_melt_into_a_two_series_chart():
    # Confirmed live: "compare HDFC Life and Axis Max Life" answered with one
    # row per fy and the insurers pivoted into separate named columns rather
    # than one row per (fy, insurer) — silently charted only the last column.
    rows = [
        {"fy": "2024-25", "hdfc_life_percent": 0.0894, "axis_max_life_percent": 0.1385},
        {"fy": "2025-26", "hdfc_life_percent": 0.0995, "axis_max_life_percent": 0.1486},
    ]
    spec = derive_chart_spec([rows])
    assert spec is not None
    assert spec["x_key"] == "fy"
    assert {s["key"] for s in spec["series"]} == {"hdfc_life", "axis_max_life"}
    assert spec["data"] == [
        {"fy": "2024-25", "hdfc_life": 0.0894, "axis_max_life": 0.1385},
        {"fy": "2025-26", "hdfc_life": 0.0995, "axis_max_life": 0.1486},
    ]


def test_multi_metric_entity_pivoted_columns_get_no_chart_not_a_wrong_one():
    # Same pivot shape, but two metrics per insurer (13th/61st month
    # persistency) — melting still works, but each (fy, insurer) now covers
    # two metric rows, which collide on the insurer series key. No chart
    # beats a chart that silently shows only one of the two metrics.
    rows = [
        {"fy": "2024-25", "hdfc_life_13th_month_pct": 86.9, "axis_max_life_13th_month_pct": 85.0,
         "hdfc_life_61st_month_pct": 63.5, "axis_max_life_61st_month_pct": 53.0},
        {"fy": "2025-26", "hdfc_life_13th_month_pct": 84.9, "axis_max_life_13th_month_pct": 83.0,
         "hdfc_life_61st_month_pct": 64.4, "axis_max_life_61st_month_pct": 58.0},
    ]
    assert derive_chart_spec([rows]) is None


def test_partial_entity_pivot_is_left_unmelted():
    # Only one of two numeric columns matches the {insurer}_{metric} shape —
    # melting just that one would silently drop the other from the chart.
    rows = [{"fy": "2024-25", "hdfc_life_percent": 0.0894, "facts_loaded": 756}]
    from irdai_bot.nodes.analysis.chart import _melt_entity_pivoted_columns

    assert _melt_entity_pivoted_columns(rows) is None


def test_derive_chart_specs_returns_one_chart_per_distinct_query_shape():
    # Real case: "plot the above in multiple graphs" — the agent ran one
    # differently-shaped query per requested breakdown; each deserves its
    # own chart, not just the last one (derive_chart_spec's behavior).
    premium_basis = [
        {"insurer": "hdfc_life", "duration": "13th", "persistency_percent": 84.9},
        {"insurer": "axis_max_life", "duration": "13th", "persistency_percent": 83.0},
        {"insurer": "hdfc_life", "duration": "61st", "persistency_percent": 64.4},
        {"insurer": "axis_max_life", "duration": "61st", "persistency_percent": 58.0},
    ]
    count_basis = [
        {"insurer": "hdfc_life", "duration": "13th", "policy_count_percent": 82.2},
        {"insurer": "axis_max_life", "duration": "13th", "policy_count_percent": 83.0},
        {"insurer": "hdfc_life", "duration": "61st", "policy_count_percent": 55.6},
        {"insurer": "axis_max_life", "duration": "61st", "policy_count_percent": 59.0},
    ]
    specs = derive_chart_specs([premium_basis, count_basis])
    assert len(specs) == 2
    assert {s["y_label"] for s in specs} == {"Persistency Percent", "Policy Count Percent"}
    for spec in specs:
        assert {s["key"] for s in spec["series"]} == {"hdfc_life", "axis_max_life"}


def test_derive_chart_specs_dedupes_only_exact_repeat_calls():
    # Deliberately NOT deduped by shape+dims the way derive_chart_spec's
    # single-chart path is: two genuinely different breakdowns routinely
    # share a shape and even overlapping dimension values (confirmed live —
    # "by premium" vs "by policy count" persistency both come back as
    # {insurer, duration, persistency_percent} over the same durations), so
    # shape/dims can't distinguish that from a retry of the same question.
    # Only an EXACT repeat of an earlier call's rows is skipped.
    by_premium = [{"insurer": "hdfc_life", "duration": "13th", "persistency_percent": 84.9},
                  {"insurer": "axis_max_life", "duration": "13th", "persistency_percent": 83.0},
                  {"insurer": "hdfc_life", "duration": "61st", "persistency_percent": 64.4},
                  {"insurer": "axis_max_life", "duration": "61st", "persistency_percent": 58.0}]
    by_count = [{"insurer": "hdfc_life", "duration": "13th", "persistency_percent": 82.2},
                {"insurer": "axis_max_life", "duration": "13th", "persistency_percent": 83.0},
                {"insurer": "hdfc_life", "duration": "61st", "persistency_percent": 55.6},
                {"insurer": "axis_max_life", "duration": "61st", "persistency_percent": 59.0}]
    exact_repeat = list(by_premium)  # e.g. an accidental duplicate parallel call
    specs = derive_chart_specs([by_premium, by_count, exact_repeat])
    assert len(specs) == 2


def test_derive_chart_specs_skips_non_chartable_groups_without_failing_the_rest():
    chartable = [{"fy": "2024-25", "value": 1.0}, {"fy": "2025-26", "value": 2.0}]
    junk = [{"note": "not a data row"}]
    specs = derive_chart_specs([junk, chartable])
    assert len(specs) == 1
    assert specs[0]["data"][0]["fy"] == "2024-25"


def test_derive_chart_specs_caps_at_max_charts_per_turn():
    from irdai_bot.nodes.analysis.chart import MAX_CHARTS_PER_TURN

    result_sets = [
        [{"fy": "2024-25", f"metric_{i}": 1.0}, {"fy": "2025-26", f"metric_{i}": 2.0}]
        for i in range(MAX_CHARTS_PER_TURN + 3)
    ]
    assert len(derive_chart_specs(result_sets)) == MAX_CHARTS_PER_TURN


def test_derive_chart_spec_and_specs_agree_on_a_single_shape_turn():
    # derive_chart_spec (singular, still used wherever only one chart makes
    # sense) must keep behaving exactly as before the refactor.
    spec = derive_chart_spec([TREND_ROWS])
    specs = derive_chart_specs([TREND_ROWS])
    assert len(specs) == 1
    assert specs[0] == spec


def test_derive_chart_specs_handles_none_values_without_crashing():
    # Real crash: fingerprinting compared (key, value) tuples across rows,
    # and one row's None (e.g. a regex-extracted column that didn't match)
    # against a sibling row's string value raised TypeError.
    rows = [
        {"insurer": "hdfc_life", "duration": "13th", "value": 1.0},
        {"insurer": "hdfc_life", "duration": None, "value": 2.0},
    ]
    derive_chart_specs([rows])  # must not raise
