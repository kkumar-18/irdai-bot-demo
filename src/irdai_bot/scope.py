"""MVP scope constants: which insurers, years, and forms this pipeline covers.

See /Users/krishnakumar/.claude/plans/breezy-zooming-fox.md for the reasoning.
"""

from __future__ import annotations

import re

INSURERS = ("hdfc_life", "axis_max_life")

# Indian fiscal years, annual filings only.
FISCAL_YEARS = (
    "2019-20",
    "2020-21",
    "2021-22",
    "2022-23",
    "2023-24",
    "2024-25",
    "2025-26",
)

# Regulatory form codes in scope, matched against link text/filenames.
#
# Correction from the source design doc: it named "L-38" for claims data,
# but per HDFC Life's own form index (tests/fixtures/hdfc_life_2025-26_annual.pdf,
# page 2) L-38 is actually "Business Acquisition through Different Channels
# (Individual)" — claims data is L-40 ("Quarterly Claims Data for Life"),
# which is what a claims-settled ratio actually needs. Using L-40 here.
FORMS = ("L-1-A-RA", "L-4", "L-22", "L-32", "L-40", "L-41")

# Of those, the forms the layout-specific pipeline (extract/pdf.py +
# normalize/canon/<form>.yaml) loads into `facts`, by form number. Every other
# form — and any fiscal year outside FISCAL_YEARS — is transcribed generically
# into `disclosure_values` on demand (extract/disclosures.py).
CURATED_FORMS_BY_NUMBER = {1: "L-1-A-RA", 4: "L-4", 22: "L-22", 32: "L-32", 40: "L-40", 41: "L-41"}

_FORM_CODE_RE = re.compile(r"^\s*(?:FORM\s*)?L\s*-?\s*0?(\d{1,2})(?![0-9])", re.IGNORECASE)
_FY_RE = re.compile(r"^(\d{4})-(\d{2})$")


def normalize_form(code: str) -> str | None:
    """'L-1-A-RA' / 'L-01' / 'l38' -> 'L-1' / 'L-1' / 'L-38'; None if not an L-1..L-45 code."""
    m = _FORM_CODE_RE.match(code or "")
    if not m or not 1 <= int(m.group(1)) <= 45:
        return None
    return f"L-{int(m.group(1))}"


def form_number(code: str) -> int | None:
    normalized = normalize_form(code)
    return int(normalized[2:]) if normalized else None


def is_fiscal_year(fy: str) -> bool:
    """'2012-13' style, consecutive years."""
    m = _FY_RE.match(fy or "")
    return bool(m) and 2000 <= int(m.group(1)) <= 2099 and (int(m.group(1)) + 1) % 100 == int(m.group(2))


# Broader regex family for matching forms in scraped text/filenames before
# narrowing to FORMS.
FORM_PATTERN = r"\bL[\s\-]?(\d{1,2})(?:[\s\-][A-Z]{1,3})*\b"
