import hashlib
from pathlib import Path

from irdai_bot.extract.pdf import extract_pdf

FIXTURES = Path(__file__).parent / "fixtures"


def test_extracts_all_target_forms_from_real_hdfc_filing():
    path = FIXTURES / "hdfc_life_2025-26_annual.pdf"
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    cells = extract_pdf(str(path), insurer="hdfc_life", fy="2025-26", source_sha256=sha)

    forms_found = {c.form for c in cells}
    assert forms_found == {"L-1-A-RA", "L-4", "L-22", "L-32", "L-40", "L-41"}
    assert len(cells) > 1000  # this bundle has thousands of real data points


def test_extracted_values_match_known_ground_truth():
    # Hand-verified against the raw PDF text (page 9, "FORM L-4-PREMIUM
    # SCHEDULE"): First year premiums, quarter ended March 31 2026 = 4,42,517.
    path = FIXTURES / "hdfc_life_2025-26_annual.pdf"
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    cells = extract_pdf(str(path), insurer="hdfc_life", fy="2025-26", source_sha256=sha)

    first_year_premium_cells = [c for c in cells if c.form == "L-4" and "First year premiums" in c.row_label]
    assert any(c.raw_value == "4,42,517" for c in first_year_premium_cells)
    assert any(c.raw_value == "13,87,932" for c in first_year_premium_cells)


def test_every_cell_carries_provenance():
    path = FIXTURES / "hdfc_life_2025-26_annual.pdf"
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    cells = extract_pdf(str(path), insurer="hdfc_life", fy="2025-26", source_sha256=sha)
    for c in cells[:50]:
        assert c.source_sha256 == sha
        assert c.page_number >= 1


def _revenue_account_cells(name: str, insurer: str):
    path = FIXTURES / name
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    return extract_pdf(str(path), insurer=insurer, fy="2025-26", source_sha256=sha, forms=["L-1-A-RA"])


def _grand_total_year(cells, row_label):
    return [
        c.raw_value
        for c in cells
        if c.row_label == row_label and c.col_label == "GRAND TOTAL (FOR THE YEAR ENDED, current filing year)"
    ]


def test_revenue_account_columns_map_to_the_fixed_irdai_template():
    # Real bug: the generic header scrape treated the "Registration No. ...
    # 101 dated ..." line as the first data row, so every L-1-A-RA column
    # header was an insurer-name fragment and nothing was ever loaded
    # (v_expense_ratio always empty). Values hand-checked against page 4 of
    # the PDF text ("REVENUE ACCOUNT FOR THE YEAR ENDED MARCH 31, 2026").
    cells = _revenue_account_cells("hdfc_life_2025-26_annual.pdf", "hdfc_life")
    assert {c.page_number for c in cells} == {3, 4}  # prior-year comparative pages 5-6 skipped
    assert len({c.col_label for c in cells}) == 36  # 18 columns x (quarter, year)
    assert _grand_total_year(cells, "Operating expenses related to insurance business L-6") == ["7,69,091"]
    assert _grand_total_year(cells, "(a) Premium L-4") == ["79,38,707"]
    linked_life = [
        c.raw_value
        for c in cells
        if c.row_label == "(a) Premium L-4" and c.col_label == "LINKED LIFE (FOR THE YEAR ENDED, current filing year)"
    ]
    assert linked_life == ["22,77,764"]


def test_revenue_account_sub_totals_are_qualified_by_section():
    # "Sub Total" closes both the premiums and investment-income sections;
    # unqualified, both mapped to premiums_earned_net.
    cells = _revenue_account_cells("hdfc_life_2025-26_annual.pdf", "hdfc_life")
    assert _grand_total_year(cells, "Premiums earned - net - Sub Total") == ["77,31,522"]
    assert _grand_total_year(cells, "Income from investments - Sub Total") == ["20,17,517"]


def test_revenue_account_extracts_axis_max_life_layout():
    cells = _revenue_account_cells("axis_max_life_2025-26_annual.pdf", "axis_max_life")
    assert _grand_total_year(cells, "Operating Expenses related to Insurance Business L-6") == ["566,311"]
    assert _grand_total_year(cells, "(b) Reinsurance ceded") == ["(77,277)"]


def test_merge_close_anchors_rejoins_split_columns():
    # HDFC FY2023-24 offsets "-" placeholders ~4pt left of the digits,
    # splitting each real column into two x1 clusters.
    from irdai_bot.extract.pdf import _merge_close_anchors

    assert _merge_close_anchors([312, 316, 334, 339, 356]) == [316, 339, 356]


def _premium_schedule(name: str, insurer: str):
    path = FIXTURES / name
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    cells = extract_pdf(str(path), insurer=insurer, fy="2025-26", source_sha256=sha, forms=["L-4"])
    return {(c.row_label, c.col_label): c.raw_value for c in cells}


def test_premium_schedule_reads_axis_columns_by_their_own_headers():
    # Real bug: the generic scrape stitched Axis's two-line column headers
    # ("For the Year ended" / "March 31,2026") into the wrong columns, so its
    # FY2025-26 total premium was loaded as ₹4,085 cr instead of ₹38,877 cr,
    # and the L-5 commission rows printed below on the same page leaked in.
    cells = _premium_schedule("axis_max_life_2025-26_annual.pdf", "axis_max_life")
    assert cells[("TOTAL PREMIUM", "FOR THE YEAR ENDED (current filing year)")] == "3,887,689"
    assert cells[("TOTAL PREMIUM", "FOR THE QUARTER ENDED (current filing year)")] == "1,368,191"
    assert not any("Commission" in label for label, _ in cells)  # stopped at FORM L-5
    assert {col for _, col in cells} == {
        "FOR THE YEAR ENDED (current filing year)",
        "FOR THE QUARTER ENDED (current filing year)",
    }  # prior-year comparative columns (March 31, 2025) skipped


def test_premium_schedule_handles_hdfc_date_row_headers():
    # HDFC prints each column's date as its own "March 31,2026" row, which
    # looked like a numeric data row and cut the header short.
    cells = _premium_schedule("hdfc_life_2025-26_annual.pdf", "hdfc_life")
    assert cells[("Total Premiums", "FOR THE YEAR ENDED (current filing year)")] == "79,38,707"
    assert any(v == "13,87,932" for (label, _), v in cells.items() if "First year premiums" in label)
