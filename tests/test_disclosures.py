import datetime as dt
from pathlib import Path

import pdfplumber

from irdai_bot.extract.disclosures import filing_period_end, locate_form_pages, printed_on_page, schema_version
from irdai_bot.normalize.canon import form_info
from irdai_bot.scope import is_fiscal_year, normalize_form

FIXTURES = Path(__file__).parent / "fixtures"


def test_schema_version_is_keyed_on_period_end_not_fiscal_year():
    # v2021 framework applies to periods ending on/after Sep 30, 2021.
    assert schema_version(filing_period_end("2020-21")) == "v2010"  # March 31, 2021
    assert schema_version(filing_period_end("2021-22")) == "v2021"  # March 31, 2022
    assert schema_version(dt.date(2021, 6, 30)) == "v2010"  # FY2021-22 Q1 was still the old format
    assert schema_version(dt.date(2021, 9, 30)) == "v2021"


def test_forms_added_in_2021_have_no_earlier_version():
    assert form_info(44)["versions"] == ["v2021"]
    assert "v2010" in form_info(38)["versions"]


def test_form_codes_normalize_to_their_number():
    assert normalize_form("L-1-A-RA") == normalize_form("L-1") == normalize_form("L-01") == "L-1"
    assert normalize_form("FORM L - 38") == "L-38"
    assert normalize_form("L-46") is None
    assert is_fiscal_year("2012-13") and not is_fiscal_year("2012-14")


def test_printed_on_page_accepts_kerning_split_numbers_and_rejects_misreads():
    page = "1Individual agents 1 88,043   199\n Total (A)  4 26,530  1,279"
    assert printed_on_page("188,043", page)  # "1 88,043" split by wide kerning
    assert printed_on_page("426,530", page)
    assert printed_on_page("-", page)
    assert not printed_on_page("188,034", page)  # transposed digits
    assert not printed_on_page("5,470", page)


def test_locate_form_pages_routes_by_page_heading_and_skips_the_index():
    with pdfplumber.open(FIXTURES / "hdfc_life_2025-26_annual.pdf") as pdf:
        pages = locate_form_pages(pdf, {4, 38})
    assert {p.form_number for p in pages} == {4, 38}
    assert all(p.page_number != 2 for p in pages)  # page 2 is the "List of Website Disclosure" index
    l38 = [p for p in pages if p.form_number == 38]
    assert l38 and "L-38" in l38[0].heading.replace(" ", "")
