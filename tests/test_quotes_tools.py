import unittest.mock as mock

import pytest

from irdai_bot.nodes.quotes import tools


def test_allows_select_on_quote_views():
    q = tools.validate_query("SELECT product_id, insurer FROM v_product_catalog")
    assert q.startswith("SELECT")
    assert "LIMIT" in q  # auto-appended when the query has none


def test_preserves_existing_limit():
    q = tools.validate_query("SELECT * FROM v_product_catalog LIMIT 5")
    assert q.count("LIMIT") == 1


def test_rejects_non_select_statements():
    with pytest.raises(tools.QueryRejected):
        tools.validate_query("DELETE FROM product")
    with pytest.raises(tools.QueryRejected):
        tools.validate_query("DROP TABLE product")


def test_rejects_multiple_statements():
    with pytest.raises(tools.QueryRejected):
        tools.validate_query("SELECT 1; DROP TABLE product")


def test_rejects_disallowed_relations():
    with pytest.raises(tools.QueryRejected):
        tools.validate_query("SELECT * FROM product")  # base table, not a v_* view
    with pytest.raises(tools.QueryRejected):
        tools.validate_query("SELECT * FROM eligibility_rule")
    with pytest.raises(tools.QueryRejected):
        tools.validate_query("SELECT * FROM v_product_catalog p JOIN product r ON 1=1")


def test_allows_ctes_referencing_only_allowed_relations():
    q = (
        "WITH scored AS (SELECT insurer, value FROM v_insurer_scorecard) "
        "SELECT insurer, value FROM scored ORDER BY value DESC"
    )
    tools.validate_query(q)


def test_still_rejects_cte_that_reads_a_disallowed_relation():
    q = "WITH sneaky AS (SELECT * FROM product) SELECT * FROM sneaky"
    with pytest.raises(tools.QueryRejected):
        tools.validate_query(q)


class _FakeCursor:
    description = [("product_id",), ("insurer",)]

    def fetchall(self):
        return [("AX-SWAG", "Axis Max Life Insurance Ltd")]


class _FakeCon:
    def __init__(self):
        self.last_query = None
        self.last_params = None

    def execute(self, q, params=None):
        self.last_query = q
        self.last_params = params
        return _FakeCursor()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_run_sql_products_passes_no_params_tuple_for_arbitrary_agent_sql():
    # A literal '%' in agent-written SQL (e.g. a LIKE pattern) must not be
    # treated as a placeholder needing '%%' escaping — run_sql_products must
    # call execute() with no params argument at all, exactly like
    # nodes/analysis/tools.py's run_sql, rather than routing through _q's
    # generic (sql, *params) helper.
    fake_con = _FakeCon()
    with mock.patch.object(tools, "connect", return_value=fake_con):
        run_sql_products = tools.make_run_sql_products_tool("postgresql://unused")
        result = run_sql_products.invoke({"query": "SELECT product_id, insurer FROM v_product_catalog"})
    assert result == [{"product_id": "AX-SWAG", "insurer": "Axis Max Life Insurance Ltd"}]
    assert fake_con.last_params is None


def test_run_sql_products_rejects_disallowed_relation_without_hitting_the_db():
    run_sql_products = tools.make_run_sql_products_tool("postgresql://unused")
    result = run_sql_products.invoke({"query": "SELECT * FROM product"})
    assert result == [{"error": mock.ANY}]
    assert "disallowed relation" in result[0]["error"]
