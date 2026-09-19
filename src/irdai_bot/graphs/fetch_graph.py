"""Fetch graph: the background workflow that fills data gaps the chat agent
runs into — any IRDAI form (L-1..L-45), any fiscal year the insurer's
website publishes. Started (never awaited) by the agent's fetch_disclosures
tool; see irdai_bot/fetch.py for how jobs are queued and polled.

    plan_fetch -> load_curated -> transcribe_disclosures -> verify_availability -> record_attempts
         \\_____________________ (nothing missing) _____________________/

- load_curated: requested (fy, form) pairs a verified fixed-layout extractor
  exists for (FETCH_CURATED_FORMS x scope.FISCAL_YEARS) go through the
  ingest graph into `facts` — discover filings on the insurer's
  public-disclosure page (adapters/, requests + BeautifulSoup) -> download ->
  extract -> LLM label mapping -> validate -> load. interactive_review=False:
  nobody is at a CLI to resolve low-confidence labels mid-job.
- transcribe_disclosures: every pair still missing after that (other forms,
  earlier years, or curated pages whose layout didn't parse) is discovered
  and downloaded the same way, then transcribed cell-by-cell into
  `disclosure_values` (extract/disclosures.py).

Pairs are "fy L-n" labels, with forms normalized to their number (L-1-A-RA
and L-1 are the same form).
"""

from __future__ import annotations

import contextvars
import datetime as dt
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
from langgraph.graph import END, START, StateGraph
from psycopg.types.json import Jsonb
from typing_extensions import TypedDict

from irdai_bot.adapters.base import DiscoveryError
from irdai_bot.config import Config
from irdai_bot.db.connection import connect
from irdai_bot.extract.disclosures import extract_filing, filing_period_end, schema_version
from irdai_bot.graphs.ingest_graph import build_ingest_graph
from irdai_bot.llm import make_mapping_llm
from irdai_bot.nodes.ingest.discover import ADAPTERS
from irdai_bot.nodes.ingest.download_extract import download_filing
from irdai_bot.normalize.canon import form_info
from irdai_bot.observability import with_tracing
from irdai_bot.scope import CURATED_FORMS_BY_NUMBER, FISCAL_YEARS, form_number, normalize_form

logger = logging.getLogger(__name__)

# A (insurer, fy, form) tried this recently isn't scraped again: if it's
# still missing, re-running the same extraction won't change that, and a
# repeat question shouldn't cost another full download/extract/LLM pass.
REFETCH_COOLDOWN = dt.timedelta(hours=24)

# Of the curated forms, only those with a verified fixed-layout extractor
# (extract/pdf.py:_revenue_account_cells, _premium_schedule_cells) go through
# the ingest graph. Confirmed live: fetching L-22 that way spent ~60s per
# insurer and loaded 0 facts (91 labels left for review) before falling
# through to transcription anyway.
FETCH_CURATED_FORMS = {1, 4}
# Filings transcribed at once. Each already runs up to 4 LLM page calls in
# parallel; the per-filing PDF scan (~9s / 100 pages) is the serial part.
_PARALLEL_FILINGS = 3

_DISCLOSURE_VALUES_COLUMNS = (
    "insurer", "fy", "schema_version", "form", "form_key", "form_heading", "table_title", "row_label",
    "column_label", "measure", "period", "period_end", "unit", "scale", "raw_value", "value",
    "source_sha256", "source_url", "page_number", "extraction_method", "extracted_at",
)
_DISCLOSURE_PAGES_COLUMNS = (
    "source_sha256", "page_number", "insurer", "fy", "form", "heading", "page_text", "status",
    "values_extracted", "values_dropped_unverified", "error", "extracted_at",
)


class FetchState(TypedDict, total=False):
    job_id: str  # fetch_jobs row to report progress on (absent when run outside fetch.py)
    insurer: str
    fiscal_years: list[str]
    forms: list[str]
    to_fetch: list[str]  # "fy L-n" pairs missing and not tried recently
    not_applicable: list[str]  # pairs for forms that didn't exist under that year's framework
    filings_found: int
    facts_loaded: int
    values_transcribed: int
    labels_left_for_review: int
    now_available: list[str]
    still_unavailable: list[str]
    found_new_data: bool


def label(fy: str, form: str) -> str:
    return f"{fy} {normalize_form(form)}"


def _split(pair: str) -> tuple[str, str]:
    fy, form = pair.split(" ", 1)
    return fy, form


def plan_fetch(con: psycopg.Connection, insurer: str, fiscal_years: list[str], forms: list[str]) -> dict:
    """Which requested pairs are already loaded (in `facts` or
    `disclosure_values`), which were tried within REFETCH_COOLDOWN, which
    can't exist (e.g. L-44 before the v2021 framework), and which are left to
    fetch. Shared by the graph and fetch.py's decision to start a job at all."""
    codes = sorted({normalize_form(f) for f in forms if normalize_form(f)}, key=form_number)
    available = {
        label(fy, form)
        for fy, form in con.execute(
            "SELECT DISTINCT fy, form FROM facts WHERE insurer = %s AND fy = ANY(%s) "
            "UNION SELECT DISTINCT fy, form FROM disclosure_values WHERE insurer = %s AND fy = ANY(%s)",
            [insurer, fiscal_years, insurer, fiscal_years],
        ).fetchall()
        if normalize_form(form)
    }
    recently_tried = {
        label(fy, form)
        for fy, form in con.execute(
            "SELECT fy, form FROM fetch_log WHERE insurer = %s AND fy = ANY(%s) AND attempted_at > %s",
            [insurer, fiscal_years, dt.datetime.now(dt.timezone.utc) - REFETCH_COOLDOWN],
        ).fetchall()
    }
    wanted = [label(fy, code) for fy in fiscal_years for code in codes]
    not_applicable = [
        w for w in wanted
        if schema_version(filing_period_end(_split(w)[0])) not in form_info(form_number(_split(w)[1]))["versions"]
    ]
    return {
        "available": [w for w in wanted if w in available],
        "not_applicable": not_applicable,
        "recently_tried": [w for w in wanted if w not in available and w in recently_tried and w not in not_applicable],
        "to_fetch": [
            w for w in wanted if w not in available and w not in recently_tried and w not in not_applicable
        ],
    }


def build_fetch_graph(cfg: Config, con: psycopg.Connection):
    def report(state: FetchState, db: psycopg.Connection, **progress) -> None:
        if state.get("job_id"):
            db.execute("UPDATE fetch_jobs SET progress = %s WHERE job_id = %s", [Jsonb(progress), state["job_id"]])

    def plan(state: FetchState) -> dict:
        p = plan_fetch(con, state["insurer"], state["fiscal_years"], state["forms"])
        return {"to_fetch": p["to_fetch"], "not_applicable": p["not_applicable"]}

    def load_curated(state: FetchState) -> dict:
        pairs = [
            _split(p) for p in state["to_fetch"]
            if _split(p)[0] in FISCAL_YEARS and form_number(_split(p)[1]) in FETCH_CURATED_FORMS
        ]
        if not pairs:
            return {"facts_loaded": 0, "labels_left_for_review": 0}
        report(state, con, stage="Reading revenue account / premium schedule filings")
        years = sorted({fy for fy, _ in pairs})
        forms = sorted({CURATED_FORMS_BY_NUMBER[form_number(f)] for _, f in pairs})
        logger.info("Loading curated %s %s %s from the insurer's website", state["insurer"], years, forms)
        result = build_ingest_graph(cfg, con, interactive_review=False).invoke(
            {
                "run_id": f"fetch-{uuid.uuid4().hex[:8]}",
                "insurers": [state["insurer"]],
                "fiscal_years": years,
                "forms": forms,
                "documents": [],
                "current_doc": None,
                "downloaded": [],
                "raw_cells": [],
                "normalized_facts": [],
                "pending_review": [],
                "resolved_review": [],
                "validation_errors": [],
                "facts_loaded": 0,
            },
            config=with_tracing(),
        )
        return {
            "filings_found": len(result.get("documents", [])),
            "facts_loaded": result.get("facts_loaded", 0),
            "labels_left_for_review": len(result.get("pending_review", [])),
        }

    def transcribe_disclosures(state: FetchState) -> dict:
        still_missing = set(plan_fetch(con, state["insurer"], state["fiscal_years"], state["forms"])["to_fetch"])
        pairs = [_split(p) for p in state["to_fetch"] if p in still_missing]
        if not pairs:
            return {"values_transcribed": 0}
        by_year: dict[str, set[int]] = {}
        for fy, form in pairs:
            by_year.setdefault(fy, set()).add(form_number(form))

        adapter = ADAPTERS[state["insurer"]]
        try:
            filings = [d for d in adapter.list_files(tuple(sorted(by_year))) if d.url.lower().endswith(".pdf")]
        except DiscoveryError:
            logger.exception("Discovery failed for %s", state["insurer"])
            raise
        llm = make_mapping_llm(cfg)
        report(state, con, stage="Reading filings", filings_done=0, filings_total=len(filings))

        def transcribe(doc) -> int:
            # Own connection: psycopg transactions on one shared connection
            # would interleave across these threads.
            with connect(cfg.database_url, apply_schema=False) as db:
                path, sha = download_filing(cfg, doc)
                done_pages = {
                    n for (n,) in db.execute(
                        "SELECT page_number FROM disclosure_pages WHERE source_sha256 = %s AND status = 'extracted'",
                        [sha],
                    ).fetchall()
                }
                logger.info("Transcribing %s %s L-%s from %s", doc.insurer, doc.fy, sorted(by_year[doc.fy]), doc.url)
                values, pages = extract_filing(
                    llm,
                    str(path),
                    insurer=doc.insurer,
                    fy=doc.fy,
                    source_sha256=sha,
                    source_url=doc.url,
                    form_numbers=by_year[doc.fy],
                    skip_pages=done_pages,
                )
                _save(db, values, pages)
                return len(values)

        transcribed = done = 0
        # Same reason as extract/disclosures.py: copy the ambient RunnableConfig
        # (Langfuse callbacks included) across the thread boundary by hand, since
        # ThreadPoolExecutor doesn't propagate contextvars into worker threads.
        # A separate copy_context() per task, not one shared Context — a single
        # Context object can't be entered by more than one thread at once (raises
        # "cannot enter context: ... is already entered" under _PARALLEL_FILINGS > 1).
        with ThreadPoolExecutor(max_workers=_PARALLEL_FILINGS) as ex:
            futures = [ex.submit(contextvars.copy_context().run, transcribe, doc) for doc in filings]
            for future in futures:
                count = future.result()
                transcribed += count
                done += 1
                report(state, con, stage="Reading filings", filings_done=done, filings_total=len(filings))
        return {"filings_found": state.get("filings_found", 0) + len(filings), "values_transcribed": transcribed}

    def _save(db: psycopg.Connection, values: list[dict], pages: list[dict]) -> None:
        with db.transaction(), db.cursor() as cur:
            if values:
                cur.executemany(
                    f"INSERT INTO disclosure_values ({', '.join(_DISCLOSURE_VALUES_COLUMNS)}) "
                    f"VALUES ({', '.join(['%s'] * len(_DISCLOSURE_VALUES_COLUMNS))}) ON CONFLICT DO NOTHING",
                    [tuple(v[c] for c in _DISCLOSURE_VALUES_COLUMNS) for v in values],
                )
            if pages:
                cur.executemany(
                    f"INSERT INTO disclosure_pages ({', '.join(_DISCLOSURE_PAGES_COLUMNS)}) "
                    f"VALUES ({', '.join(['%s'] * len(_DISCLOSURE_PAGES_COLUMNS))}) "
                    "ON CONFLICT (source_sha256, page_number) DO UPDATE SET "
                    + ", ".join(f"{c} = excluded.{c}" for c in _DISCLOSURE_PAGES_COLUMNS[2:]),
                    [tuple(p[c] for c in _DISCLOSURE_PAGES_COLUMNS) for p in pages],
                )

    def verify_availability(state: FetchState) -> dict:
        after = plan_fetch(con, state["insurer"], state["fiscal_years"], state["forms"])
        now_available = after["available"]
        return {
            "now_available": now_available,
            "still_unavailable": after["recently_tried"] + after["to_fetch"],
            "not_applicable": after["not_applicable"],
            "found_new_data": any(p in now_available for p in state["to_fetch"]),
        }

    def record_attempts(state: FetchState) -> dict:
        if state["to_fetch"]:
            now = dt.datetime.now(dt.timezone.utc)
            available = set(state["now_available"])
            with con.transaction(), con.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO fetch_log (insurer, fy, form, attempted_at, facts_available)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (insurer, fy, form) DO UPDATE SET
                        attempted_at = excluded.attempted_at,
                        facts_available = excluded.facts_available
                    """,
                    [(state["insurer"], *_split(pair), now, int(pair in available)) for pair in state["to_fetch"]],
                )
        return {}

    graph = StateGraph(FetchState)
    graph.add_node("plan_fetch", plan)
    graph.add_node("load_curated", load_curated)
    graph.add_node("transcribe_disclosures", transcribe_disclosures)
    graph.add_node("verify_availability", verify_availability)
    graph.add_node("record_attempts", record_attempts)

    graph.add_edge(START, "plan_fetch")
    graph.add_conditional_edges(
        "plan_fetch",
        lambda s: "fetch" if s["to_fetch"] else "verify",
        {"fetch": "load_curated", "verify": "verify_availability"},
    )
    graph.add_edge("load_curated", "transcribe_disclosures")
    graph.add_edge("transcribe_disclosures", "verify_availability")
    graph.add_edge("verify_availability", "record_attempts")
    graph.add_edge("record_attempts", END)
    return graph.compile()
