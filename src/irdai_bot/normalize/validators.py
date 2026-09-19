"""Sanity checks applied to normalized facts before they're loaded into the
warehouse. Deliberately conservative: these catch clear extraction failures
(a value that's absurdly large for a lakh-scale insurer figure, a required
field missing) without trying to assert accounting identities the extractor
can't yet reliably reconstruct column-by-column (see extract/pdf.py).
"""

from __future__ import annotations

from dataclasses import dataclass

# HDFC Life and Axis Max Life are each multi-billion-rupee insurers; a single
# line item beyond ~5 lakh crore (5e12 rupees) in base units is almost
# certainly a unit-scale or column-assignment bug, not a real figure.
_MAX_PLAUSIBLE_VALUE = 5e12


@dataclass(frozen=True)
class ValidationIssue:
    severity: str  # 'error' | 'warning'
    message: str
    context: dict


def validate_fact(fact: dict) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if fact.get("value") is None:
        issues.append(ValidationIssue("error", "value is null", {"fact": fact}))
        return issues
    if fact["unit"] == "INR" and abs(fact["value"]) > _MAX_PLAUSIBLE_VALUE:
        issues.append(
            ValidationIssue(
                "warning",
                f"value {fact['value']!r} exceeds plausible range for a single line item — "
                "likely a unit-scale or column-assignment error",
                {"fact": fact},
            )
        )
    if not fact.get("line_item"):
        issues.append(ValidationIssue("error", "missing canonical line_item", {"fact": fact}))
    return issues
