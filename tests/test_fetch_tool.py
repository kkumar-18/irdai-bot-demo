from pathlib import Path

from irdai_bot.config import Config
from irdai_bot.nodes.analysis import tools


def _cfg() -> Config:
    return Config(
        openai_api_key="x",
        mapping_model="m",
        ollama_base_url="http://localhost:11434",
        narration_model="n",
        data_dir=Path("."),
        database_url="postgresql://unused",
    )


def test_rejects_out_of_scope_years_and_forms_without_fetching(monkeypatch):
    monkeypatch.setattr(tools, "request_fetch", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not fetch")))
    fetch = tools.make_fetch_disclosures_tool(_cfg())
    result = fetch.invoke({"insurer": "hdfc_life", "fiscal_years": ["2010-12"], "forms": ["L-99"]})
    assert "error" in result and "2010-12" in result["error"] and "L-99" in result["error"]


def test_any_real_year_and_form_is_accepted_and_normalized(monkeypatch):
    # Real complaint: a FY2012-13 channel-mix question (L-37/L-38/L-30) was
    # refused as "outside the dataset" instead of being fetched.
    seen = {}
    monkeypatch.setattr(tools, "request_fetch", lambda cfg, **k: seen.update(k) or {"ok": True})
    fetch = tools.make_fetch_disclosures_tool(_cfg())
    fetch.invoke({"insurer": "axis_max_life", "fiscal_years": ["2012-13"], "forms": ["L-38", "l-37", "FORM L-30", "L-1-A-RA"]})
    assert seen["fiscal_years"] == ["2012-13"]
    assert seen["forms"] == ["L-1", "L-30", "L-37", "L-38"]


def test_fetch_failures_come_back_as_a_result_not_an_exception(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("site unreachable")

    monkeypatch.setattr(tools, "request_fetch", boom)
    fetch = tools.make_fetch_disclosures_tool(_cfg())
    result = fetch.invoke({"insurer": "axis_max_life", "fiscal_years": ["2024-25"], "forms": ["L-1-A-RA"]})
    assert result == {"error": "Couldn't start fetching from the insurer's website: site unreachable"}


def test_empty_years_and_forms_mean_everything_in_scope(monkeypatch):
    seen = {}
    monkeypatch.setattr(tools, "request_fetch", lambda cfg, **k: seen.update(k) or {"ok": True})
    tools.make_fetch_disclosures_tool(_cfg()).invoke({"insurer": "hdfc_life", "fiscal_years": [], "forms": []})
    assert seen["fiscal_years"] == list(tools.FISCAL_YEARS)
    assert seen["forms"] == ["L-1", "L-4", "L-22", "L-32", "L-40", "L-41"]


def test_every_view_has_a_source_form():
    assert set(tools.VIEW_SOURCE_FORMS) == tools.ALLOWED_RELATIONS - {"facts", "disclosure_values"}
