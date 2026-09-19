"""Normalize node: raw (row_label, col_label, value) cells -> canonical facts.

Unit-scale note: confirmed by a real bug during the 2026-09-13 smoke test —
HDFC Life's FY2025-26 filing declares "(₹ Lakh)" but its FY2019-20 filing
(comparative-year page) declares "(₹ '000)" for the *same form*, and a
hardcoded scale produced values ~100x too large for those years. Scale is
now detected per-column from the column's own header text (often carries
the "(₹ Lakh)"/"(₹ '000)" annotation right there, e.g. "(₹ '000) the year
ended March 31, 2019"), via units.parse_declared_scale, falling back to
DEFAULT_DECLARED_SCALE only when a column's own text doesn't say. Percentage/
ratio line items are exempted automatically (detected from a trailing '%' in
the raw token), never scaled.
"""

from __future__ import annotations

import datetime as dt
import logging

import psycopg
from langchain_openai import ChatOpenAI

from irdai_bot.graphs.state import IngestState
from irdai_bot.normalize.mapping import map_columns, map_line_items
from irdai_bot.normalize.units import coerce_value, parse_declared_scale, scale_multiplier

logger = logging.getLogger(__name__)

DEFAULT_DECLARED_SCALE = "lakh"


_FACT_KEY = ("insurer", "fy", "period", "form", "segment", "line_item", "source_sha256")


def _drop_duplicate_keys(facts: list[dict]) -> list[dict]:
    """Two different raw rows can map to the same canonical line item on the
    same page (e.g. L-1-A-RA's "(b) Reinsurance ceded" in the premiums section
    and "(b) Amount ceded in reinsurance" under benefits both resolve to
    reinsurance_ceded). They share a facts primary key, so the load upsert
    would silently keep whichever came LAST. Keep the first occurrence
    instead — extraction emits rows top-down, and a form's primary line for
    an item comes before later look-alikes."""
    seen: dict[tuple, float] = {}
    kept: list[dict] = []
    for f in facts:
        key = tuple(f[k] for k in _FACT_KEY)
        if key in seen:
            if seen[key] == f["value"]:
                continue  # the same figure printed twice (HDFC's L-4 repeats "Total Premiums")
            logger.warning(
                "Dropping duplicate %s/%s %s from raw row %r (an earlier row already mapped there)",
                f["form"], f["segment"], f["line_item"], f["raw_row_label"],
            )
            continue
        seen[key] = f["value"]
        kept.append(f)
    return kept


def make_normalize_node(con: psycopg.Connection, mapping_llm: ChatOpenAI):
    def normalize(state: IngestState) -> dict:
        cells = state["raw_cells"]
        if not cells:
            return {"normalized_facts": [], "pending_review": []}

        by_insurer_form: dict[tuple[str, str], list[dict]] = {}
        for c in cells:
            by_insurer_form.setdefault((c["insurer"], c["form"]), []).append(c)

        normalized_facts: list[dict] = []
        pending_review: list[dict] = []

        for (insurer, form), form_cells in by_insurer_form.items():
            row_labels = [c["row_label"] for c in form_cells]
            col_labels = [c["col_label"] for c in form_cells]

            li_accepted, li_pending = map_line_items(
                con, mapping_llm, insurer=insurer, form=form, raw_labels=row_labels
            )
            col_accepted, col_pending = map_columns(
                con, mapping_llm, insurer=insurer, form=form, raw_labels=col_labels
            )

            for m in li_pending:
                pending_review.append({"kind": "line_item", "insurer": insurer, "form": form, **m.model_dump()})
            for m in col_pending:
                pending_review.append({"kind": "column", "insurer": insurer, "form": form, **m.model_dump()})

            for c in form_cells:
                li = li_accepted.get(c["row_label"])
                col = col_accepted.get(c["col_label"])
                if li is None or col is None:
                    continue  # awaiting human review for this raw label

                is_percent = c["raw_value"].strip().endswith("%")
                # Prefer the scale stated in THIS column's own header text
                # when present — confirmed real case (Axis Max Life L-4):
                # a page can carry two tables in different scales (a premium
                # table in "(Amount in Rs. Lakhs)" alongside an unrelated
                # policy-count table in "'000"), so the page-wide scale
                # (extract/pdf.py) is only a fallback default, not the
                # source of truth, when a column states its own.
                declared_scale = (
                    parse_declared_scale(c["col_label"]) or c.get("declared_scale") or DEFAULT_DECLARED_SCALE
                )
                value = coerce_value(c["raw_value"], declared_scale)
                if value is None:
                    continue
                value *= scale_multiplier(declared_scale, is_percent)

                fy = c["fy"]
                if col.is_comparative:
                    start_year = int(fy.split("-")[0]) - 1
                    fy = f"{start_year}-{str(start_year + 1)[-2:]}"

                normalized_facts.append(
                    {
                        "insurer": insurer,
                        "fy": fy,
                        "period": "FY" if col.period == "annual" else "Q4",
                        "form": form,
                        # segment is part of the facts PRIMARY KEY, so it can't be
                        # NULL even for company-wide (no-segment-breakdown) forms.
                        "segment": col.segment_id or "unspecified",
                        "line_item": li.canonical_id,
                        "value": value,
                        "unit": "INR" if not is_percent else "PERCENT",
                        "declared_scale": None if is_percent else declared_scale,
                        "is_restated": False,
                        "source_sha256": c["source_sha256"],
                        "source_locator": f"page={c['page_number']};row_top={c['row_top']:.1f};col={c['col_label']!r}",
                        "raw_row_label": c["row_label"],
                        "raw_col_label": c["col_label"],
                        "extracted_at": dt.datetime.now(dt.timezone.utc),
                        "confidence": min(li.confidence, col.confidence),
                    }
                )

        normalized_facts = _drop_duplicate_keys(normalized_facts)
        logger.info(
            "Normalized %d facts, %d labels pending review", len(normalized_facts), len(pending_review)
        )
        return {"normalized_facts": normalized_facts, "pending_review": pending_review}

    return normalize
