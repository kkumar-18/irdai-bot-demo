"""Unit/scale coercion: parse a filing's declared scale and a raw numeric
token into a base-unit float, so 'lakh' and 'crore' figures never get mixed
silently — confirmed real filings state it inline, e.g. "(₹ Lakh)" on
tests/fixtures/hdfc_life_2025-26_annual.pdf. Base unit here is plain INR
(rupees), i.e. lakh values are multiplied by 100,000 and crore by 10,000,000.
"""

from __future__ import annotations

import re

SCALE_MULTIPLIERS = {
    "lakh": 100_000,
    "crore": 10_000_000,
    "thousand": 1_000,
    "actual": 1,
    "unit": 1,
    "percent": 1,  # ratios/percentages are not rupee-scaled at all
}

# Confirmed real bug #1 (2026-09-13 smoke test): older HDFC Life filings
# state "(₹ '000)" instead of "(₹ Lakh)" for the SAME form — a hardcoded
# scale silently produced values ~100x too large for those years. Detect the
# scale from each column's own header text instead.
#
# Confirmed real bug #2 (same session): a bare, unanchored "000" alternative
# here matched the "000" inside an unrelated 4-digit year ("...November 15,
# 2000") elsewhere on the page, before the real "(Amount in Rs. Lakhs)"
# annotation — silently producing values ~100x too SMALL. The "'000" variant
# now requires its leading apostrophe (the actual distinguishing mark in
# every real filing seen), so a bare year can never match it.
_SCALE_HEADER_RE = re.compile(
    r"\(?\s*(?:rs\.?|₹|inr)?\s*(?:in\s+)?"
    r"(lakh|lakhs|crore|crores|'000s?|thousands?)"
    r"\s*\)?",
    re.IGNORECASE,
)
_SCALE_ALIASES = {
    "lakh": "lakh", "lakhs": "lakh",
    "crore": "crore", "crores": "crore",
    "thousand": "thousand", "thousands": "thousand",
    "'000": "thousand", "'000s": "thousand",
}


def parse_declared_scale(header_text: str) -> str | None:
    m = _SCALE_HEADER_RE.search(header_text or "")
    if not m:
        return None
    return _SCALE_ALIASES.get(m.group(1).lower())


def coerce_value(raw_value: str, declared_scale: str | None) -> float | None:
    """Parse an Indian-grouped accounting token ('4,42,517', '(1,234)', '-',
    '43.2%') into a float in the declared scale's units (caller applies the
    scale multiplier separately via SCALE_MULTIPLIERS — kept apart from this
    function so percentage/ratio line items, which must NOT be scaled, are
    handled correctly by the caller instead of silently multiplied)."""
    text = raw_value.strip()
    if text in ("-", "", "NA", "N.A", "N.A."):
        return 0.0
    is_percent = text.endswith("%")
    text = text.rstrip("%")
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    text = text.replace(",", "")
    try:
        value = float(text)
    except ValueError:
        return None
    if negative:
        value = -value
    return value if is_percent else value


def scale_multiplier(declared_scale: str | None, is_percent: bool) -> float:
    if is_percent or declared_scale is None:
        return 1.0
    return SCALE_MULTIPLIERS.get(declared_scale, 1.0)
