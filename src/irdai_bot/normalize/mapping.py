"""LLM-assisted label mapping, cached in the Postgres label_cache table.

Two axes get mapped independently, both through the same cache-first pattern:
- line items: a raw row label -> one of the form's canonical line_item ids.
- columns: a raw (garbled, multi-word) column label -> canonical segment id
  + period ('annual'/'quarterly') + whether it's a prior-year comparative
  column embedded in the same table (see extract/pdf.py's module docstring
  for why column labels are noisy — real filings, no ruling lines).

Cached at the (insurer, form, kind, raw_label) level directly, not via a
whole-layout fingerprint hash: insurers reuse the exact same wording for a
form year over year (confirmed: HDFC's L-1/L-4/L-32/L-40/L-41 headers are
byte-identical in structure across the vintages inspected), so a direct
raw-label cache gets the same "near-zero LLM calls at steady state" property
the fingerprint approach was aiming for, with less machinery and more
surgical invalidation when a label actually does change.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

import psycopg
from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, Field

from irdai_bot.normalize.canon import line_item_ids, load_form_canon, segment_ids

CONFIDENCE_THRESHOLD = 0.85


class LineItemMapping(BaseModel):
    raw_label: str
    canonical_id: str | None = Field(description="One of the form's known line_item ids, or null if none fit")
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


class LineItemMappingBatch(BaseModel):
    mappings: list[LineItemMapping]


class ColumnMapping(BaseModel):
    raw_label: str
    segment_id: str | None = Field(description="One of the form's known segment ids, or null if none fit")
    period: Literal["annual", "quarterly", "unknown"]
    is_comparative: bool = Field(description="True if this column is prior-year comparative data")
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


class ColumnMappingBatch(BaseModel):
    mappings: list[ColumnMapping]


def _cached(con: psycopg.Connection, insurer: str, form: str, kind: str, raw_label: str) -> dict | None:
    row = con.execute(
        "SELECT canonical_id, segment_id, period, is_comparative, confidence, reasoning "
        "FROM label_cache WHERE insurer=%s AND form=%s AND kind=%s AND raw_label=%s",
        [insurer, form, kind, raw_label],
    ).fetchone()
    if row is None:
        return None
    cols = ["canonical_id", "segment_id", "period", "is_comparative", "confidence", "reasoning"]
    return dict(zip(cols, row))


def _write_cache(con: psycopg.Connection, insurer: str, form: str, kind: str, row: dict) -> None:
    con.execute(
        """
        INSERT INTO label_cache
            (insurer, form, kind, raw_label, canonical_id, segment_id, period, is_comparative, confidence, reasoning, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (insurer, form, kind, raw_label) DO UPDATE SET
            canonical_id = excluded.canonical_id,
            segment_id = excluded.segment_id,
            period = excluded.period,
            is_comparative = excluded.is_comparative,
            confidence = excluded.confidence,
            reasoning = excluded.reasoning,
            updated_at = excluded.updated_at
        """,
        [
            insurer,
            form,
            kind,
            row["raw_label"],
            row.get("canonical_id"),
            row.get("segment_id"),
            row.get("period"),
            row.get("is_comparative"),
            row["confidence"],
            row["reasoning"],
            dt.datetime.now(dt.timezone.utc),
        ],
    )


def map_line_items(
    con: psycopg.Connection,
    llm: BaseChatModel,
    *,
    insurer: str,
    form: str,
    raw_labels: list[str],
) -> tuple[dict[str, LineItemMapping], list[LineItemMapping]]:
    """Returns (accepted_mappings, low_confidence_for_review)."""
    accepted: dict[str, LineItemMapping] = {}
    pending: list[LineItemMapping] = []
    to_query: list[str] = []

    for label in dict.fromkeys(raw_labels):  # de-dupe, preserve order
        cached = _cached(con, insurer, form, "line_item", label)
        if cached is not None:
            m = LineItemMapping(
                raw_label=label,
                canonical_id=cached["canonical_id"],
                confidence=cached["confidence"],
                reasoning=cached["reasoning"] or "",
            )
            if m.confidence >= CONFIDENCE_THRESHOLD and m.canonical_id:
                accepted[label] = m
            else:
                pending.append(m)
        else:
            to_query.append(label)

    if to_query:
        known_ids = line_item_ids(form)
        canon = load_form_canon(form)
        alias_hints = "\n".join(
            f"- {li['id']}: e.g. {', '.join(li.get('aliases', [])[:3])}" for li in canon["line_items"]
        )
        prompt = (
            f"You are mapping raw row labels extracted from IRDAI regulatory form {form} "
            f"(insurer: {insurer}) to a fixed set of canonical line-item ids.\n\n"
            f"Known canonical line items (id: example aliases):\n{alias_hints}\n\n"
            f"Raw labels to map (may be truncated/garbled OCR-adjacent text from PDF extraction):\n"
            + "\n".join(f"- {l!r}" for l in to_query)
            + "\n\nFor each raw label, pick the single best-fitting canonical_id from the list above, "
            "or null if none genuinely fit (do not force a match). Set confidence honestly: "
            "1.0 only for an exact/near-exact wording match, lower for a plausible-but-uncertain guess."
        )
        structured_llm = llm.with_structured_output(LineItemMappingBatch)
        result: LineItemMappingBatch = structured_llm.invoke(prompt)
        for m in result.mappings:
            if m.canonical_id is not None and m.canonical_id not in known_ids:
                m = m.model_copy(update={"canonical_id": None, "confidence": min(m.confidence, 0.5)})
            _write_cache(
                con,
                insurer,
                form,
                "line_item",
                {
                    "raw_label": m.raw_label,
                    "canonical_id": m.canonical_id,
                    "confidence": m.confidence,
                    "reasoning": m.reasoning,
                },
            )
            if m.confidence >= CONFIDENCE_THRESHOLD and m.canonical_id:
                accepted[m.raw_label] = m
            else:
                pending.append(m)

    return accepted, pending


def map_columns(
    con: psycopg.Connection,
    llm: BaseChatModel,
    *,
    insurer: str,
    form: str,
    raw_labels: list[str],
) -> tuple[dict[str, ColumnMapping], list[ColumnMapping]]:
    accepted: dict[str, ColumnMapping] = {}
    pending: list[ColumnMapping] = []
    to_query: list[str] = []

    for label in dict.fromkeys(raw_labels):
        cached = _cached(con, insurer, form, "column", label)
        if cached is not None:
            m = ColumnMapping(
                raw_label=label,
                segment_id=cached["segment_id"],
                period=cached["period"] or "unknown",
                is_comparative=bool(cached["is_comparative"]),
                confidence=cached["confidence"],
                reasoning=cached["reasoning"] or "",
            )
            if m.confidence >= CONFIDENCE_THRESHOLD and m.period != "unknown":
                accepted[label] = m
            else:
                pending.append(m)
        else:
            to_query.append(label)

    if to_query:
        known_segments = segment_ids(form)
        seg_list = "\n".join(f"- {s}" for s in known_segments) or "(no segment breakdown for this form)"
        prompt = (
            f"You are mapping raw, noisy column-header text extracted from IRDAI regulatory form {form} "
            f"(insurer: {insurer}) onto structured metadata. Each column header was assembled by "
            "concatenating words positionally from a multi-row PDF table header without ruling lines, "
            "so expect fragments, word-order scrambling, and unrelated text bleeding in.\n\n"
            f"Known canonical segment ids for this form:\n{seg_list}\n\n"
            "Raw column labels to interpret:\n" + "\n".join(f"- {l!r}" for l in to_query)
            + "\n\nFor each raw label, infer:\n"
            "- segment_id: best-fitting id from the list above, or null if this column has no product-segment "
            "breakdown (e.g. a company-wide total) or none genuinely fit.\n"
            "- period: 'annual' if the header suggests a year-to-date/annual figure (e.g. contains "
            "'Up to the period ended', 'FOR THE YEAR ENDED', 'Up to the Quarter End'), 'quarterly' if it's "
            "explicitly quarter-only (e.g. 'For the quarter ended'), else 'unknown'.\n"
            "- is_comparative: true if the header's date suggests this is the PRIOR fiscal year's comparative "
            "column rather than the current filing's own year (e.g. an earlier calendar date than the other "
            "columns implied)."
        )
        structured_llm = llm.with_structured_output(ColumnMappingBatch)
        result: ColumnMappingBatch = structured_llm.invoke(prompt)
        for m in result.mappings:
            if m.segment_id is not None and m.segment_id not in known_segments:
                m = m.model_copy(update={"segment_id": None, "confidence": min(m.confidence, 0.5)})
            _write_cache(
                con,
                insurer,
                form,
                "column",
                {
                    "raw_label": m.raw_label,
                    "segment_id": m.segment_id,
                    "period": m.period,
                    "is_comparative": m.is_comparative,
                    "confidence": m.confidence,
                    "reasoning": m.reasoning,
                },
            )
            if m.confidence >= CONFIDENCE_THRESHOLD and m.period != "unknown":
                accepted[m.raw_label] = m
            else:
                pending.append(m)

    return accepted, pending
