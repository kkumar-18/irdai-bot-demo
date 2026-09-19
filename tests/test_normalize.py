import pytest

from irdai_bot.normalize.canon import line_item_ids, load_form_canon, segment_ids
from irdai_bot.normalize.units import coerce_value, parse_declared_scale, scale_multiplier
from irdai_bot.normalize.validators import validate_fact


def test_coerce_value_handles_indian_grouping_and_negatives():
    assert coerce_value("4,42,517", "lakh") == 442517.0
    assert coerce_value("(1,234)", "lakh") == -1234.0
    assert coerce_value("-", "lakh") == 0.0
    assert coerce_value("43.2%", "lakh") == 43.2


def test_parse_declared_scale_distinguishes_lakh_from_thousand():
    # Real case from the 2026-09-13 smoke test: the same form (L-4) states
    # different scales across filing years for the same insurer.
    assert parse_declared_scale("(₹ Lakh)") == "lakh"
    assert parse_declared_scale("(₹ '000) the year ended March 31, 2019") == "thousand"
    assert parse_declared_scale("(Rs. crore)") == "crore"
    assert parse_declared_scale("no scale annotation here") is None


def test_parse_declared_scale_ignores_bare_years():
    # Real bug from the same session: an unanchored "000" alternative
    # matched the "000" inside "November 15, 2000" (registration date text)
    # before reaching the real "(Amount in Rs. Lakhs)" annotation later on
    # the same page, silently producing values ~100x too small.
    text = (
        "Registration No. 104 and Date of Registration with the IRDAI: "
        "November 15, 2000\nFORM L-4-PREMIUM SCHEDULE\n(Amount in Rs. Lakhs)"
    )
    assert parse_declared_scale(text) == "lakh"
    assert parse_declared_scale("Founded in 2000, filed for FY 3000") is None


def test_scale_multiplier_never_scales_percentages():
    assert scale_multiplier("lakh", is_percent=True) == 1.0
    assert scale_multiplier("lakh", is_percent=False) == 100_000
    assert scale_multiplier("crore", is_percent=False) == 10_000_000


def test_validate_fact_flags_implausibly_large_values():
    fact = {"value": 5e13, "unit": "INR", "line_item": "total_premium"}
    issues = validate_fact(fact)
    assert any(i.severity == "warning" for i in issues)


def test_validate_fact_flags_missing_line_item():
    fact = {"value": 100.0, "unit": "INR", "line_item": None}
    issues = validate_fact(fact)
    assert any(i.severity == "error" for i in issues)


@pytest.mark.parametrize("form", ["L-1-A-RA", "L-4", "L-22", "L-32", "L-40", "L-41"])
def test_all_in_scope_forms_have_loadable_canon(form):
    canon = load_form_canon(form)
    assert canon["form"] == form
    assert len(line_item_ids(form)) > 0
    if canon.get("uses_shared_segments") or canon.get("segments"):
        assert len(segment_ids(form)) > 0


def test_duplicate_fact_keys_keep_the_first_row():
    # L-1-A-RA's premiums "(b) Reinsurance ceded" and the benefits section's
    # "(b) Amount ceded in reinsurance" both map to reinsurance_ceded; the
    # load upsert would otherwise keep the later (wrong) one.
    from irdai_bot.nodes.ingest.normalize import _drop_duplicate_keys

    base = {"insurer": "hdfc_life", "fy": "2025-26", "period": "FY", "form": "L-1-A-RA",
            "segment": "grand_total", "line_item": "reinsurance_ceded", "source_sha256": "x"}
    facts = [
        {**base, "value": -2.0, "raw_row_label": "(b) Reinsurance ceded"},
        {**base, "value": -9.0, "raw_row_label": "(b) Amount ceded in reinsurance"},
        {**base, "line_item": "commission", "value": 5.0, "raw_row_label": "Commission L-5"},
    ]
    kept = _drop_duplicate_keys(facts)
    assert [f["raw_row_label"] for f in kept] == ["(b) Reinsurance ceded", "Commission L-5"]
