"""Generic extraction for any IRDAI public-disclosure form, any fiscal year.

The layout-specific pipeline (extract/pdf.py -> `facts`) covers six forms for
FY2019-20 onwards. Everything else a question can need — channel mix (L-37/
L-38), related-party transactions (L-30), FY2012-13 filings — is transcribed
here into `disclosure_values`, on demand (graphs/fetch_graph.py):

1. Route pages by the form heading printed on each page, never by filename:
   pre-2021 bundles are split across "financial" and "other" files, and
   headings vary ("FORM L-30 :", "L-4-PREMIUM SCHEDULE", "FOR L-40- :").
2. Render each page with its column alignment preserved (and digit fragments
   split by wide kerning re-joined), so column membership stays readable.
3. An LLM transcribes the page's tables into (row, column, value) cells for
   the filing's own period — structured output, no arithmetic.
4. Every value is checked against the page text: its digits must actually
   be printed there, otherwise it's dropped as a misread.

Schema version is keyed on the filing's period end, not the fiscal year: the
v2021 framework took effect for periods ending on/after Sep 30, 2021, so
FY2021-22's own quarters are mixed. This pipeline reads year-end (Q4)
bundles only, whose period end (March 31) decides the version cleanly.
"""

from __future__ import annotations

import contextvars
import datetime as dt
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Literal

import pdfplumber
from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, Field

from irdai_bot.extract.pdf import _group_rows, _merge_split_numeric_fragments
from irdai_bot.normalize.canon import form_info
from irdai_bot.normalize.units import SCALE_MULTIPLIERS, coerce_value

logger = logging.getLogger(__name__)

V2021_EFFECTIVE_PERIOD_END = dt.date(2021, 9, 30)
EXTRACTION_METHOD = "llm-page-v1"

INSURER_NAMES = {"hdfc_life": "HDFC Life", "axis_max_life": "Axis Max Life (formerly Max Life / Max New York Life)"}

# A heading names the form at the start of one of the page's first lines:
# "FORM L-30 : ...", "FORM NO. L-32 ...", "FORM - L - 34 ...", "L-4-PREMIUM
# SCHEDULE", "FOR L-40- : ..." (a real typo), "PERIODIC DISCLOSURES" then "L-1-A-RA".
_HEADING_RE = re.compile(
    r"^\s*(?:FORMS?|FOR)?\s*(?:NO\.?)?\s*[-:]?\s*L\s?-\s?0?(\d{1,2})(?![0-9])", re.IGNORECASE
)
_ANY_FORM_CODE_RE = re.compile(r"\bL\s?-\s?0?(\d{1,2})(?![0-9])")
_HEADING_LOOKAHEAD_LINES = 7
# The bundle's own "List of Website Disclosure" index names every form; a
# real form page references a handful at most (L-1 cites schedules L-4..L-7).
_INDEX_PAGE_MIN_FORMS = 8
# A form that runs over several pages repeats its heading on most of them;
# a page with no heading of its own continues the previous form, up to this many.
_MAX_CONTINUATION_PAGES = 3


def filing_period_end(fy: str) -> dt.date:
    return dt.date(int(fy[:4]) + 1, 3, 31)


def schema_version(period_end: dt.date) -> str:
    return "v2021" if period_end >= V2021_EFFECTIVE_PERIOD_END else "v2010"


@dataclass(frozen=True)
class FormPage:
    form_number: int
    page_number: int  # 1-indexed
    heading: str
    text: str  # layout-preserving rendering


def render_layout(page) -> str:
    words = _merge_split_numeric_fragments(page.extract_words())
    chars_per_pt = 220 / float(page.width)
    lines = []
    for row in _group_rows(words):
        line = ""
        for w in sorted(row, key=lambda w: w["x0"]):
            column = int(w["x0"] * chars_per_pt)
            line += " " * max(1 if line else 0, column - len(line)) + w["text"]
        lines.append(line.rstrip())
    return "\n".join(lines)


def locate_form_pages(pdf: pdfplumber.PDF, wanted: set[int]) -> list[FormPage]:
    found: list[FormPage] = []
    current: int | None = None
    continuation = 0
    for i, page in enumerate(pdf.pages):
        text = page.extract_text() or ""
        if len(set(_ANY_FORM_CODE_RE.findall(text))) >= _INDEX_PAGE_MIN_FORMS:
            current = None
            continue
        heading = None
        for line in text.split("\n")[:_HEADING_LOOKAHEAD_LINES]:
            m = _HEADING_RE.match(line)
            if m and 1 <= int(m.group(1)) <= 45:
                heading, current, continuation = line.strip(), int(m.group(1)), 0
                break
        if heading is None:
            continuation += 1
            if current is None or continuation > _MAX_CONTINUATION_PAGES or not text.strip():
                current = None
                continue
        if current in wanted:
            found.append(
                FormPage(current, i + 1, heading or f"(continuation of L-{current})", render_layout(page))
            )
    return found


class ExtractedCell(BaseModel):
    table_title: str = Field(description="Sub-table heading when the page holds more than one table, else empty")
    row_label: str = Field(description="Row label as printed, prefixed with its section heading when ambiguous alone")
    column_label: str = Field(description="Full column header as printed, header lines joined with ' | '")
    measure: str = Field(description="What the number measures, in a few words, e.g. 'premium', 'number of policies'")
    period: Literal["quarter", "year_to_date", "as_at", "other"]
    period_end: str = Field(description="Column's period end as YYYY-MM-DD")
    unit: Literal["INR", "count", "percent", "ratio", "other"]
    scale: Literal["units", "thousand", "lakh", "crore", "million"]
    value_text: str = Field(description="The number exactly as printed: commas, brackets for negatives, '-' for nil")


class PageCells(BaseModel):
    cells: list[ExtractedCell]


_PROMPT = """You are transcribing one page of IRDAI public-disclosure form {code} — {title} — \
filed by the life insurer {insurer} for the financial year ended {period_end:%B %d, %Y}.

The page text below was rendered from the PDF with its column alignment preserved. \
The rupee symbol often renders as a backtick (`). A number is occasionally split by \
stray spacing (e.g. "1 88,043" in a single column) — use the alignment to read it as one value.

Transcribe every numeric cell (including "-" for nil) in columns for the filing's OWN \
period ending {period_end:%Y-%m-%d}: the quarter column, the year-to-date / "up to" / \
"for the year" column, and "as at" balances on that date. Skip comparative columns \
for earlier periods. For each cell:
- table_title: the sub-table heading if the page has more than one table, else "".
- row_label: as printed; prefix the section heading when the label alone is ambiguous \
(e.g. "Premiums earned - net: Sub Total", "Group business: Total").
- column_label: the full column header, all its header lines joined with " | " \
(e.g. "For the year ended March 31, 2013 | Premium (` in Crores)").
- measure: what the number measures (e.g. "premium", "number of policies", "number of lives covered", "amount").
- period: quarter | year_to_date | as_at | other; period_end: YYYY-MM-DD.
- unit: INR for money; count for numbers of policies, lives, schemes, complaints, \
offices, people; percent; ratio; other.
- scale: the declared scale for that cell's column/table: ` '000 / thousands -> thousand, \
lakh(s) -> lakh, crore(s)/Crs -> crore, million(s) -> million, plain rupees or counts -> units.
- value_text: exactly as printed.
Never compute, total, round or infer a value that isn't printed. If the page has no \
numeric table (only names or narrative text), return no cells.

PAGE TEXT:
{text}"""


_NIL_TOKENS = {"-", "–", "—", "nil", "na", "n.a", "n.a."}


def printed_on_page(value_text: str, page_text: str) -> bool:
    """True if value_text's digits literally appear on the page (spacing and
    digit-group commas ignored, so a kerning-split "1 88,043" still matches)."""
    token = value_text.strip()
    if token.lower() in _NIL_TOKENS:
        return True
    digits = re.sub(r"[^\d.]", "", token).strip(".")
    return bool(digits) and digits in re.sub(r"[\s,]", "", page_text)


_MULTIPLIERS = {**SCALE_MULTIPLIERS, "units": 1, "million": 1_000_000}


def _to_row(cell: ExtractedCell, page: FormPage, *, insurer: str, fy: str, source_sha256: str, source_url: str) -> dict | None:
    value = coerce_value(cell.value_text, None)
    if value is None:
        return None
    if cell.unit not in ("percent", "ratio"):
        value *= _MULTIPLIERS[cell.scale]
    try:
        period_end = dt.date.fromisoformat(cell.period_end)
    except ValueError:
        period_end = None
    period_end_of_filing = filing_period_end(fy)
    info = form_info(page.form_number)
    return {
        "insurer": insurer,
        "fy": fy,
        "schema_version": schema_version(period_end_of_filing),
        "form": f"L-{page.form_number}",
        "form_key": info["key"],
        "form_heading": page.heading[:300],
        "table_title": cell.table_title.strip(),
        "row_label": cell.row_label.strip(),
        "column_label": cell.column_label.strip(),
        "measure": cell.measure.strip(),
        "period": cell.period,
        "period_end": period_end,
        "unit": cell.unit,
        "scale": cell.scale,
        "raw_value": cell.value_text.strip(),
        "value": value,
        "source_sha256": source_sha256,
        "source_url": source_url,
        "page_number": page.page_number,
        "extraction_method": EXTRACTION_METHOD,
        "extracted_at": dt.datetime.now(dt.timezone.utc),
    }


def extract_page(
    llm: BaseChatModel, page: FormPage, *, insurer: str, fy: str, source_sha256: str, source_url: str
) -> tuple[list[dict], int]:
    """Returns (rows to load, number of LLM cells dropped as not printed on the page)."""
    info = form_info(page.form_number)
    prompt = _PROMPT.format(
        code=f"L-{page.form_number}",
        title=info["title"],
        insurer=INSURER_NAMES.get(insurer, insurer),
        period_end=filing_period_end(fy),
        text=page.text,
    )
    result: PageCells = llm.with_structured_output(PageCells).invoke(prompt)
    rows, dropped = [], 0
    for cell in result.cells:
        if not printed_on_page(cell.value_text, page.text):
            dropped += 1
            continue
        row = _to_row(cell, page, insurer=insurer, fy=fy, source_sha256=source_sha256, source_url=source_url)
        if row is not None:
            rows.append(row)
    return rows, dropped


def extract_filing(
    llm: BaseChatModel,
    path: str,
    *,
    insurer: str,
    fy: str,
    source_sha256: str,
    source_url: str,
    form_numbers: set[int],
    skip_pages: set[int] = frozenset(),
) -> tuple[list[dict], list[dict]]:
    """Transcribe the requested forms from one downloaded filing.
    Returns (disclosure_values rows, disclosure_pages rows)."""
    version = schema_version(filing_period_end(fy))
    wanted = {n for n in form_numbers if version in form_info(n)["versions"]}
    with pdfplumber.open(path) as pdf:
        pages = [p for p in locate_form_pages(pdf, wanted) if p.page_number not in skip_pages]
    if not pages:
        return [], []

    def run(page: FormPage) -> tuple[FormPage, list[dict], int, str | None]:
        try:
            rows, dropped = extract_page(
                llm, page, insurer=insurer, fy=fy, source_sha256=source_sha256, source_url=source_url
            )
            return page, rows, dropped, None
        except Exception as e:  # one unreadable page shouldn't sink the filing
            logger.exception("Extraction failed: %s %s L-%d page %d", insurer, fy, page.form_number, page.page_number)
            return page, [], 0, f"{type(e).__name__}: {e}"

    values: list[dict] = []
    page_rows: list[dict] = []
    # ThreadPoolExecutor doesn't copy contextvars into worker threads, so without
    # this the ambient RunnableConfig (and with it, the Langfuse callback handler
    # LangChain propagates automatically) silently drops out of every LLM call
    # made in here — plain .invoke(prompt) in extract_page() would run untraced.
    # A separate copy_context() per task, not one shared Context — a single
    # Context object can't be entered by more than one thread at once (raises
    # "cannot enter context: ... is already entered" once >1 page runs concurrently).
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(contextvars.copy_context().run, run, p) for p in pages]
        for page, rows, dropped, error in (f.result() for f in futures):
            values.extend(rows)
            page_rows.append(
                {
                    "source_sha256": source_sha256,
                    "page_number": page.page_number,
                    "insurer": insurer,
                    "fy": fy,
                    "form": f"L-{page.form_number}",
                    "heading": page.heading[:300],
                    "page_text": page.text,
                    "status": "failed" if error else "extracted",
                    "values_extracted": len(rows),
                    "values_dropped_unverified": dropped,
                    "error": error,
                    "extracted_at": dt.datetime.now(dt.timezone.utc),
                }
            )
            logger.info(
                "%s %s L-%d page %d: %d values (%d dropped as not printed on the page)",
                insurer, fy, page.form_number, page.page_number, len(rows), dropped,
            )
    return values, page_rows
