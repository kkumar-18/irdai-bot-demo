from irdai_bot.nodes.analysis.grounding import find_ungrounded

RESULTS = [[{"fy": "2025-26", "value": 793870700000.0}]]


def test_grounded_value_passes():
    assert find_ungrounded("The total premium was 793870700000.0 INR.", RESULTS) == []


def test_bare_fiscal_year_is_not_flagged():
    # Real bug found in the 2026-09-13 smoke test: "FY 2025-26" in prose was
    # extracted as the number 2025.0 and flagged as an unverified claim,
    # causing the agent to loop until it exhausted MAX_TURNS on a query that
    # was actually correct.
    assert find_ungrounded("HDFC Life's total premium for FY 2025-26 was 793870700000.0 INR.", RESULTS) == []


def test_fabricated_number_is_flagged():
    ungrounded = find_ungrounded("The total premium was 999999999999.0 INR.", RESULTS)
    assert 999999999999.0 in ungrounded


def test_grounds_numbers_nested_inside_a_dict_shaped_tool_result():
    # get_product_rules-style tools return a dict of sub-lists (not a plain
    # list of rows like run_sql), e.g. {"premium_bands": [{"min_amount": 5000}]}
    # — find_ungrounded must still recognize 5000 as grounded.
    nested = [{"premium_bands": [{"min_amount": 5000, "max_amount": 999999}], "product": []}]
    assert find_ungrounded("The minimum premium is 5000.", nested) == []
    assert 12345.0 in find_ungrounded("The minimum premium is 12345.", nested)


def test_collect_turn_fetch_jobs_returns_only_background_jobs_from_this_turn():
    import json

    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from irdai_bot.nodes.analysis.grounding import collect_turn_fetch_jobs

    def fetch_result(payload, call_id):
        return ToolMessage(json.dumps(payload), name="fetch_disclosures", tool_call_id=call_id)

    old_job = {"status": "fetching_in_background", "job_id": "old", "insurer": "hdfc_life", "fetching": ["2024-25 L-4"]}
    job = {"status": "fetching_in_background", "job_id": "j1", "insurer": "hdfc_life", "fetching": ["2024-25 L-1-A-RA"]}
    messages = [
        HumanMessage("earlier question"),
        fetch_result(old_job, "c0"),
        AIMessage("earlier answer"),
        HumanMessage("this question"),
        fetch_result(job, "c1"),
        fetch_result(job, "c2"),  # a second call joined the same running job
        fetch_result({"status": "no_fetch_needed", "insurer": "axis_max_life"}, "c3"),
        ToolMessage(json.dumps([{"value": 1}]), name="run_sql", tool_call_id="c4"),
        AIMessage("answer"),
    ]
    assert collect_turn_fetch_jobs(messages) == [job]
