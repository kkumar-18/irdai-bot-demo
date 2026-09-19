"""PDF extraction for IRDAI disclosure bundles.

Ground truth from inspecting real HDFC Life / Axis Max Life filings
(2026-09-13, see tests/fixtures/): each downloaded PDF bundles every L-form
into one document, page 2 (index 1) is a "List of Website Disclosure" index,
and the per-form tables have **no ruling lines** — pdfplumber's default table
detection (and its 'lines'/'text' strategies) both mis-segment these tables,
fusing adjacent segment columns (e.g. "LIFE PENSION HEALTH VAR. INS") into one
cell. Form headers are also inconsistently placed: sometimes the first text
line on the page ("FORM L-1-A-RA"), sometimes the second ("FORM L-4-PREMIUM
SCHEDULE" follows an insurer-name line), sometimes phrased "FORM NO. L-32 ...".

Working extractor: derive numeric column positions directly from the data,
by clustering the x1 (right edge) of every numeric token on a page — numbers
in these filings are right-aligned within a column, so their x1 stays
constant across rows even though there are no gridlines to detect. This
reliably recovers the true column count (confirmed: 18 segment columns on
HDFC's L-1 page, matching LINKED{Life,Pension,Health,VarIns,Total} +
PAR{Life,Annuity,Pension,Health,VarIns,Total} +
NONPAR{Life,Annuity,Pension,Health,VarIns,Total} + GrandTotal).

Except for L-1-A-RA, whose layout IRDAI fixes (see _REVENUE_ACCOUNT_COLUMNS),
this deliberately does NOT try to resolve which segment/period each column
represents at extraction time — that requires interpreting header text that
itself spans multiple columns (e.g. "NON-PARTICIPATING" sits over 6
sub-columns) and varies by form and insurer. Instead this module emits raw
(row_label, col_label, value) triples with full provenance, and normalization
resolves both axes via the same LLM-assisted label-mapping/cache mechanism
already used for line items — column headers are just another alias to map,
onto canonical (segment, period) pairs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import pdfplumber

from irdai_bot.normalize.units import parse_declared_scale

logger = logging.getLogger(__name__)

# Form-code -> regex that must match somewhere in the first few lines of a
# page for that page to belong to the form. Written from real headers seen
# in HDFC Life's bundle; Axis Max Life's bundle is expected to need its own
# entries once run against real Axis files (see normalize step 2 in the plan
# verification — this is exactly the kind of thing the live smoke test
# surfaces).
FORM_HEADER_PATTERNS: dict[str, re.Pattern] = {
    # HDFC's FY2023-24 bundle misprints the current-year pages as "FORM L-A-RA".
    "L-1-A-RA": re.compile(r"FORM\s+L-(?:1-)?A-RA"),
    "L-4": re.compile(r"FORM\s+L-4-PREMIUM"),
    "L-22": re.compile(r"FORM\s+L-22-ANALYTICAL|FORM\s+NO\.?\s*L-22"),
    "L-32": re.compile(r"FORM\s+L-32-SOLVENCY|FORM\s+NO\.?\s*L-32"),
    "L-40": re.compile(r"FORM\s+(NO\.?\s*)?L-40\b"),
    "L-41": re.compile(r"FORM\s+(NO\.?\s*)?L-41\b"),
}

_NUMERIC_RE = re.compile(r"^\(?-?[\d,]+\.?\d*%?\)?$|^-$|^NA$|^N\.A\.?$", re.IGNORECASE)
# Trailing-comma fragments ("31," from "March 31, 2026") and bare 4-digit
# years are date artifacts, not rupee/ratio data — real amounts in these
# filings are always either comma-grouped ("4,42,517") or, if small, still
# distinguishable from a year by not sitting in this exact shape.
_DATE_FRAGMENT_RE = re.compile(r"^\d{1,2},$")
_BARE_YEAR_RE = re.compile(r"^(19|20)\d{2}$")
_HEADER_LOOKAHEAD_LINES = 6
_ROW_TOLERANCE = 2.5  # pt, for grouping words into the same visual row
_COL_GAP = 4.0  # pt, for clustering numeric x1 into columns
_MIN_COL_MEMBERS_FRAC = 0.2  # discard column clusters smaller than this fraction of the modal cluster
_ASSIGN_TOLERANCE = 10.0  # pt, max distance from a column anchor to assign a word to it
# Sr.No./serial-number columns sit hard against the left margin in every
# observed form (~x0 60-70) while every real data column starts well past
# x0 200 — words left of this are always row-label/serial, never data.
_LABEL_REGION_X0_MAX = 135.0


def _is_data_numeric(word: dict) -> bool:
    text = word["text"]
    if word["x0"] < _LABEL_REGION_X0_MAX:
        return False
    if _DATE_FRAGMENT_RE.match(text) or _BARE_YEAR_RE.match(text):
        return False
    return bool(_NUMERIC_RE.match(text))


@dataclass(frozen=True)
class RawCell:
    insurer: str
    fy: str
    form: str
    row_label: str
    col_label: str
    raw_value: str
    source_sha256: str
    page_number: int  # 1-indexed, human-facing
    row_top: float
    # Scale annotation ('lakh'/'crore'/'thousand') read from the page's own
    # text, e.g. "(₹ Lakh)" or "(₹ '000)". Confirmed real case: the same form
    # declares different scales across filing years, and the annotation
    # doesn't always land inside a given column's own header-word cluster —
    # so it's captured once per page, from the full page text, not per
    # column. None if the page states no recognizable scale.
    declared_scale: str | None = None


# IRDAI prescribes L-1-A-RA's column layout, so — unlike the other forms —
# its columns need no header interpretation at all. Confirmed on both
# insurers' FY2025-26 and HDFC's FY2021-22 bundles: always exactly 18 numeric
# columns, in this order (verified from the x-positions of the LINKED /
# PARTICIPATING / NON-PARTICIPATING group headers over the column anchors).
# The generic header scrape below can't be used here: its "first data row"
# is the first row with any number in it, which on this form is the
# "Registration No. ... 101 dated ..." line, so every column header came out
# as insurer-name fragments ("Name of the Insurer: HDFC Life") and no column
# was ever mappable — the reason v_expense_ratio / v_commission_ratio were
# always empty.
_REVENUE_ACCOUNT_COLUMNS = (
    "LINKED LIFE", "LINKED PENSION", "LINKED HEALTH", "LINKED VAR. INS", "LINKED TOTAL",
    "PARTICIPATING LIFE", "PARTICIPATING ANNUITY", "PARTICIPATING PENSION",
    "PARTICIPATING HEALTH", "PARTICIPATING VAR. INS", "PARTICIPATING TOTAL",
    "NON-PARTICIPATING LIFE", "NON-PARTICIPATING ANNUITY", "NON-PARTICIPATING PENSION",
    "NON-PARTICIPATING HEALTH", "NON-PARTICIPATING VAR. INS", "NON-PARTICIPATING TOTAL",
    "GRAND TOTAL",
)
# "REVENUE ACCOUNT FOR THE YEAR ENDED MARCH 31, 2026" / "...QUARTER ENDED MARCH 31,2026"
_PERIOD_TITLE_RE = re.compile(
    r"(?:FOR|UP\s*TO)\s+THE\s+(QUARTER|YEAR|PERIOD)\s+ENDED\s+[A-Z]+\s+\d{1,2}\s*,\s*(\d{4})",
    re.IGNORECASE,
)


def _page_text_head(page) -> str:
    text = page.extract_text() or ""
    return "\n".join(text.split("\n")[:_HEADER_LOOKAHEAD_LINES])


def locate_form_pages(pdf: pdfplumber.PDF) -> dict[str, list[int]]:
    """form_code -> 0-indexed page numbers whose header matches that form."""
    pages_by_form: dict[str, list[int]] = {code: [] for code in FORM_HEADER_PATTERNS}
    for i, page in enumerate(pdf.pages):
        head = _page_text_head(page)
        for code, pattern in FORM_HEADER_PATTERNS.items():
            if pattern.search(head):
                pages_by_form[code].append(i)
    return pages_by_form


def _cluster_1d(values: list[float], gap: float) -> list[list[float]]:
    if not values:
        return []
    values = sorted(values)
    clusters = [[values[0]]]
    for v in values[1:]:
        if v - clusters[-1][-1] > gap:
            clusters.append([v])
        else:
            clusters[-1].append(v)
    return clusters


def _find_column_anchors(words: list[dict]) -> list[float]:
    numeric_x1 = [w["x1"] for w in words if _is_data_numeric(w)]
    clusters = _cluster_1d(numeric_x1, _COL_GAP)
    if not clusters:
        return []
    modal_size = max(len(c) for c in clusters)
    kept = [c for c in clusters if len(c) >= max(2, modal_size * _MIN_COL_MEMBERS_FRAC)]
    return sorted(sum(c) / len(c) for c in kept)


def _nearest_anchor(x1: float, anchors: list[float]) -> tuple[int, float]:
    best_i, best_d = -1, float("inf")
    for i, a in enumerate(anchors):
        d = abs(x1 - a)
        if d < best_d:
            best_i, best_d = i, d
    return best_i, best_d


def _group_rows(words: list[dict]) -> list[list[dict]]:
    rows: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if rows and abs(rows[-1][0]["top"] - w["top"]) <= _ROW_TOLERANCE:
            rows[-1].append(w)
        else:
            rows.append([w])
    return rows


_DIGIT_FRAGMENT_RE = re.compile(r"^[\d,.]+$")
_FRAGMENT_MERGE_GAP = 2.0  # pt


def _merge_split_numeric_fragments(words: list[dict]) -> list[dict]:
    """Some source PDFs (confirmed: HDFC's L-41 grievance-disposal page) render
    numbers with unusually wide inter-glyph kerning, which makes pdfplumber's
    extract_words() split one number into several word fragments (e.g. "295"
    comes back as "2" then "95", 0.03pt apart) — silently corrupting every
    downstream column-position and value computation. Merge digit-only
    fragments on the same row that are almost touching back into one token
    before anything else runs on this page's words."""
    merged: list[dict] = []
    for row in _group_rows(words):
        row = sorted(row, key=lambda w: w["x0"])
        i = 0
        while i < len(row):
            cur = dict(row[i])
            j = i + 1
            while (
                j < len(row)
                and _DIGIT_FRAGMENT_RE.match(cur["text"])
                and _DIGIT_FRAGMENT_RE.match(row[j]["text"])
                and row[j]["x0"] - cur["x1"] <= _FRAGMENT_MERGE_GAP
            ):
                cur["text"] += row[j]["text"]
                cur["x1"] = row[j]["x1"]
                j += 1
            merged.append(cur)
            i = j
    return merged


def extract_page_cells(
    page, *, insurer: str, fy: str, form: str, source_sha256: str, page_number: int
) -> list[RawCell]:
    declared_scale = parse_declared_scale(page.extract_text() or "")
    words = _merge_split_numeric_fragments(page.extract_words())
    anchors = _find_column_anchors(words)
    if not anchors:
        return []  # narrative-only page (notes, no tabular data) — not an error

    label_boundary = anchors[0] - _ASSIGN_TOLERANCE - 5

    if form == "L-4":
        cells = _premium_schedule_cells(
            words,
            anchors,
            insurer=insurer,
            fy=fy,
            source_sha256=source_sha256,
            page_number=page_number,
            declared_scale=declared_scale,
        )
        if cells is not None:
            return cells

    if form == "L-1-A-RA":
        # Never falls through to the generic header scrape: on this form it
        # only ever produced unmappable columns (see _REVENUE_ACCOUNT_COLUMNS).
        return _revenue_account_cells(
            page,
            words,
            anchors,
            label_boundary,
            insurer=insurer,
            fy=fy,
            source_sha256=source_sha256,
            page_number=page_number,
            declared_scale=declared_scale,
        )

    rows = _group_rows(words)

    # Header region = every row before the first row containing a numeric
    # word assigned to an anchor (i.e. before real data starts).
    first_data_row_idx = len(rows)
    for i, row in enumerate(rows):
        if any(_is_data_numeric(w) for w in row):
            first_data_row_idx = i
            break

    col_headers = [""] * len(anchors)
    for row in rows[:first_data_row_idx]:
        for w in row:
            if w["x1"] < label_boundary:
                continue  # part of the row-label column, not a data column header
            # Broadcast wide (group) labels to every anchor they visually span;
            # narrow labels go to their single nearest anchor.
            spanned = [i for i, a in enumerate(anchors) if w["x0"] - 2 <= a <= w["x1"] + 2]
            targets = spanned or [_nearest_anchor(w["x1"], anchors)[0]]
            for i in targets:
                col_headers[i] = (col_headers[i] + " " + w["text"]).strip()

    cells: list[RawCell] = []
    for row in rows[first_data_row_idx:]:
        label_words = [w["text"] for w in row if w["x1"] < label_boundary]
        row_label = " ".join(label_words).strip()
        if not row_label:
            continue
        row_top = row[0]["top"]
        for w in row:
            if w["x1"] >= label_boundary and _is_data_numeric(w):
                i, dist = _nearest_anchor(w["x1"], anchors)
                if dist <= _ASSIGN_TOLERANCE:
                    cells.append(
                        RawCell(
                            insurer=insurer,
                            fy=fy,
                            form=form,
                            row_label=row_label,
                            col_label=col_headers[i] or f"col_{i}",
                            raw_value=w["text"],
                            source_sha256=source_sha256,
                            page_number=page_number,
                            row_top=row_top,
                            declared_scale=declared_scale,
                        )
                    )
    return cells


# Real L-1-A-RA columns sit >= ~21pt apart. HDFC's FY2023-24 bundle sets its
# "-" placeholders ~4pt left of the digits' right edge, which splits every
# column into two x1 clusters (31 "columns" instead of 18).
_MIN_COLUMN_SPACING = 8.0


def _merge_close_anchors(anchors: list[float], min_spacing: float = _MIN_COLUMN_SPACING) -> list[float]:
    merged: list[list[float]] = []
    for a in anchors:
        if merged and a - merged[-1][-1] < min_spacing:
            merged[-1].append(a)
        else:
            merged.append([a])
    return [max(group) for group in merged]  # digits' right edge is the true column edge


_ENUMERATED_ROW_RE = re.compile(r"^\(\s*[a-z]\s*\)", re.IGNORECASE)  # "(a) Premium", "(c) Reinsurance accepted"


def _row_values(row: list[dict], anchors: list[float], label_boundary: float) -> tuple[str, dict[int, list[str]]]:
    """A visual row's label text, and its numeric tokens grouped by column
    index — shared by the fixed-layout extractors (L-1-A-RA, L-4)."""
    row = sorted(row, key=lambda w: w["x0"])
    row_label = " ".join(w["text"] for w in row if w["x1"] < label_boundary).strip()
    by_col: dict[int, list[str]] = {}
    for idx, w in enumerate(row):
        if w["x1"] < label_boundary:
            continue
        # Bracketed negatives are sometimes split off as a lone "(" just
        # left of the digits ("( 10,57,589)") — keep the sign.
        text = w["text"]
        if idx > 0 and row[idx - 1]["text"] == "(" and row[idx - 1]["x1"] >= label_boundary:
            text = "(" + text
        if not _NUMERIC_RE.match(text) or not _is_data_numeric({**w, "text": text}):
            continue
        i, dist = _nearest_anchor(w["x1"], anchors)
        if dist <= _ASSIGN_TOLERANCE:
            by_col.setdefault(i, []).append(text)
    return row_label, by_col


def _revenue_account_cells(
    page,
    words: list[dict],
    anchors: list[float],
    label_boundary: float,
    *,
    insurer: str,
    fy: str,
    source_sha256: str,
    page_number: int,
    declared_scale: str | None,
) -> list[RawCell]:
    """Fixed-template L-1-A-RA extraction (see _REVENUE_ACCOUNT_COLUMNS)."""
    title = _PERIOD_TITLE_RE.search(_page_text_head(page))
    if title is None:
        logger.warning("%s %s L-1-A-RA page %d: no period in the page title, skipped", insurer, fy, page_number)
        return []
    fy_end_year = int(fy.split("-")[0]) + 1
    if int(title.group(2)) != fy_end_year:
        # Prior-year comparative page. Skipped rather than loaded as the
        # previous FY: that FY's own filing is scraped too, and holding the
        # same figure from two filings made the ratio views pair one
        # filing's expenses with the other filing's premiums.
        return []
    anchors = _merge_close_anchors(anchors)
    if len(anchors) != len(_REVENUE_ACCOUNT_COLUMNS):
        # Filings before FY2021-22 use IRDAI's older 16-column template.
        logger.warning(
            "%s %s L-1-A-RA page %d: %d columns, expected the %d-column template, skipped",
            insurer, fy, page_number, len(anchors), len(_REVENUE_ACCOUNT_COLUMNS),
        )
        return []
    period_text = "FOR THE QUARTER ENDED" if title.group(1).upper() == "QUARTER" else "FOR THE YEAR ENDED"

    cells: list[RawCell] = []
    section = ""
    for row in _group_rows(words):
        row_label, by_col = _row_values(row, anchors, label_boundary)
        if len(by_col) < 2:
            # Title/registration lines carry at most a stray number ("101
            # dated ..."); a real data row fills every column ("-" if empty).
            if row_label and not by_col:
                section = row_label
            continue
        if not row_label:
            continue
        if not _ENUMERATED_ROW_RE.match(row_label) and all(v == ["-"] for v in by_col.values()):
            # Section heading ("Premiums earned - net", "Income from
            # investments - - - -"), kept so repeated labels like "Sub Total"
            # can be told apart below. Not emitted: a heading shares its
            # canonical id with its own first sub-row, and its all-dash zero
            # would shadow that sub-row's real value.
            section = row_label
            continue
        if row_label.lower().startswith("sub total") and section:
            # "Sub Total" closes both the premiums and the investment-income
            # sections; unqualified, both mapped to premiums_earned_net.
            row_label = f"{section} - {row_label}"
        for i, parts in by_col.items():
            cells.append(
                RawCell(
                    insurer=insurer,
                    fy=fy,
                    form="L-1-A-RA",
                    row_label=row_label,
                    col_label=f"{_REVENUE_ACCOUNT_COLUMNS[i]} ({period_text}, current filing year)",
                    raw_value="".join(parts),
                    source_sha256=source_sha256,
                    page_number=page_number,
                    row_top=row[0]["top"],
                    declared_scale=declared_scale,
                )
            )
    return cells


# L-4 is also an IRDAI-prescribed layout: four columns, always in the order
# quarter / year-to-date for the filing year, then the same two for the prior
# year (confirmed on HDFC Life FY2019-20 + FY2025-26 and Axis Max Life
# FY2021-22..FY2025-26). Each column's header states its period and date,
# but wrapped over two lines ("For the Year ended" / "March 31,2026"), which
# the generic scrape stitched into the wrong columns for Axis — e.g. its
# FY2022-23 annual total (₹25,342 cr) was loaded as FY2021-22 Q4, and
# FY2025-26 came out at ₹4,085 cr instead of ₹38,877 cr.
_PREMIUM_SCHEDULE_COLUMN_COUNT = 4
_PREMIUM_SCHEDULE_MIN_SPACING = 20.0  # columns sit ~90pt apart; Axis FY2021-22 splits each into two x1 clusters 10pt apart
_HEADER_PERIOD_RE = re.compile(r"quarter|year|period", re.IGNORECASE)
_HEADER_YEAR_RE = re.compile(r"(20\d{2})")
_FORM_START_RE = re.compile(r"^FORM\b", re.IGNORECASE)
# HDFC FY2024-25+ print each column's date as its own row ("March 31,2026 ..."),
# which looks like a numeric data row unless recognised.
_HEADER_DATE_TOKEN_RE = re.compile(r"^\d{1,2},\s*20\d{2}$")


def _premium_schedule_cells(
    words: list[dict],
    anchors: list[float],
    *,
    insurer: str,
    fy: str,
    source_sha256: str,
    page_number: int,
    declared_scale: str | None,
) -> list[RawCell] | None:
    """Fixed-layout L-4 extraction. Returns None (caller falls back to the
    generic scrape) when the page doesn't show the expected four columns."""
    anchors = _merge_close_anchors(anchors, _PREMIUM_SCHEDULE_MIN_SPACING)
    if len(anchors) != _PREMIUM_SCHEDULE_COLUMN_COUNT:
        logger.warning("%s %s L-4 page %d: %d columns, not the 4-column layout", insurer, fy, page_number, len(anchors))
        return None
    label_boundary = anchors[0] - _ASSIGN_TOLERANCE - 45  # column 1 is wide: its numbers run well left of their right edge

    rows = [sorted(r, key=lambda w: w["x0"]) for r in _group_rows(words)]
    start = next((i for i, r in enumerate(rows) if "L-4" in " ".join(w["text"] for w in r).upper()), None)
    if start is None:
        return None
    header_rows: list[list[dict]] = []
    body_start = len(rows)
    for i in range(start + 1, len(rows)):
        values = [v for parts in _row_values(rows[i], anchors, label_boundary)[1].values() for v in parts]
        if len(values) >= 2 and not all(_HEADER_DATE_TOKEN_RE.match(v) for v in values):
            body_start = i
            break
        header_rows.append(rows[i])
    header = " ".join(w["text"] for r in header_rows for w in r)
    periods = ["QUARTER" if m.lower() == "quarter" else "YEAR" for m in _HEADER_PERIOD_RE.findall(header)]
    years = [int(y) for y in _HEADER_YEAR_RE.findall(header)]
    fy_end_year = int(fy.split("-")[0]) + 1
    if len(periods) != 4 or len(years) != 4 or years[0] != fy_end_year:
        logger.warning(
            "%s %s L-4 page %d: couldn't read the column periods/years from %r", insurer, fy, page_number, header
        )
        return None

    cells: list[RawCell] = []
    for row in rows[body_start:]:
        if _FORM_START_RE.match(row[0]["text"]):
            break  # the next schedule (Axis prints L-5 on the same page)
        row_label, by_col = _row_values(row, anchors, label_boundary)
        if not row_label or len(by_col) < 2:
            continue
        for i, parts in by_col.items():
            if years[i] != fy_end_year:
                continue  # prior-year comparative column — see _revenue_account_cells
            period_text = "FOR THE QUARTER ENDED" if periods[i] == "QUARTER" else "FOR THE YEAR ENDED"
            cells.append(
                RawCell(
                    insurer=insurer,
                    fy=fy,
                    form="L-4",
                    row_label=row_label,
                    col_label=f"{period_text} (current filing year)",
                    raw_value="".join(parts),
                    source_sha256=source_sha256,
                    page_number=page_number,
                    row_top=row[0]["top"],
                    declared_scale=declared_scale,
                )
            )
    return cells


def extract_pdf(
    path: str, *, insurer: str, fy: str, source_sha256: str, forms: list[str] | None = None
) -> list[RawCell]:
    cells: list[RawCell] = []
    with pdfplumber.open(path) as pdf:
        pages_by_form = locate_form_pages(pdf)
        for form, page_indices in pages_by_form.items():
            if forms and form not in forms:
                continue
            for idx in page_indices:
                cells.extend(
                    extract_page_cells(
                        pdf.pages[idx],
                        insurer=insurer,
                        fy=fy,
                        form=form,
                        source_sha256=source_sha256,
                        page_number=idx + 1,
                    )
                )
    return cells
