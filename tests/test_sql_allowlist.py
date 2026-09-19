import pytest

from irdai_bot.nodes.analysis.tools import QueryRejected, validate_query


def test_allows_select_on_facts():
    q = validate_query("SELECT insurer, value FROM facts WHERE form = 'L-4'")
    assert q.startswith("SELECT")
    assert "LIMIT" in q  # auto-appended when the query has none


def test_allows_select_on_derived_views():
    validate_query("SELECT * FROM v_expense_ratio")
    validate_query("SELECT * FROM v_solvency_ratio LIMIT 10")


def test_rejects_non_select_statements():
    with pytest.raises(QueryRejected):
        validate_query("DELETE FROM facts")
    with pytest.raises(QueryRejected):
        validate_query("DROP TABLE facts")
    with pytest.raises(QueryRejected):
        validate_query("INSERT INTO facts VALUES (1)")


def test_rejects_multiple_statements():
    with pytest.raises(QueryRejected):
        validate_query("SELECT 1; DROP TABLE facts")


def test_rejects_disallowed_relations():
    with pytest.raises(QueryRejected):
        validate_query("SELECT * FROM ingest_manifest")
    with pytest.raises(QueryRejected):
        validate_query("SELECT * FROM label_cache")
    with pytest.raises(QueryRejected):
        validate_query("SELECT * FROM facts f JOIN label_cache l ON 1=1")


def test_allows_ctes_referencing_only_allowed_relations():
    # Real bug: a CTE's own name (e.g. "annual") shows up as a table
    # reference everywhere it's used, and got rejected as a disallowed
    # relation — a natural pattern for YoY/window-function questions, so
    # this broke the agent constantly in practice.
    q = (
        "WITH annual AS (SELECT fy, MAX(value) AS v FROM facts GROUP BY fy) "
        "SELECT fy, v, 100.0 * (v / LAG(v) OVER (ORDER BY fy) - 1) FROM annual ORDER BY fy"
    )
    validate_query(q)


def test_still_rejects_cte_that_reads_a_disallowed_relation():
    q = "WITH sneaky AS (SELECT * FROM label_cache) SELECT * FROM sneaky"
    with pytest.raises(QueryRejected):
        validate_query(q)


def test_preserves_existing_limit():
    q = validate_query("SELECT * FROM facts LIMIT 5")
    assert q.count("LIMIT") == 1


def test_empty_filtered_disclosure_values_query_is_flagged_for_nonexistence():
    from irdai_bot.nodes.analysis import tools

    class _FakeCursor:
        description = [("fy",)]

        def fetchall(self):
            return []

    class _FakeCon:
        def execute(self, q):
            return _FakeCursor()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import unittest.mock as mock

    with mock.patch.object(tools, "connect", return_value=_FakeCon()):
        run_sql = tools.make_run_sql_tool("postgresql://unused")
        filtered = run_sql.invoke(
            {"query": "SELECT fy FROM disclosure_values WHERE row_label ILIKE '%foo%' AND fy='2012-13'"}
        )
        assert filtered == [{"hint": tools._DISCLOSURE_PROBE_HINT}]

        # An unfiltered probe (the recommended follow-up) reports plainly —
        # never wrapped in the same hint, or the model would loop forever.
        probe = run_sql.invoke(
            {"query": "SELECT DISTINCT row_label, column_label, measure FROM disclosure_values WHERE fy='2012-13'"}
        )
        assert probe == []
