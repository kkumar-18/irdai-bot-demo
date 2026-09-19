from __future__ import annotations

import operator
from typing import Annotated, Any

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from irdai_bot.adapters.base import DisclosureFile


class DownloadedDoc(TypedDict):
    disclosure_file: dict  # DisclosureFile, as a dict (Send payloads must be plain-ish)
    local_path: str
    source_sha256: str


class IngestState(TypedDict):
    run_id: str
    insurers: list[str]
    # Optional narrowing for on-demand fetches (irdai_bot/fetch.py); empty
    # means every FY in scope.FISCAL_YEARS / every form in scope.FORMS.
    fiscal_years: list[str]
    forms: list[str]
    documents: list[DisclosureFile]
    current_doc: DisclosureFile | None  # populated only inside Send branches
    downloaded: Annotated[list[DownloadedDoc], operator.add]
    raw_cells: Annotated[list[dict], operator.add]  # RawCell, as dicts
    normalized_facts: Annotated[list[dict], operator.add]
    pending_review: Annotated[list[dict], operator.add]
    resolved_review: list[dict]
    validation_errors: Annotated[list[dict], operator.add]
    facts_loaded: int


class AnalysisState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    # Deliberately NOT tracked as accumulated state (no operator.add field
    # here) — see nodes/analysis/grounding.py:collect_turn_tool_results,
    # which derives the current turn's query results directly from
    # `messages` instead. An accumulating field would leak prior turns'
    # results into this turn's grounding check across a multi-turn session.
    turn_count: int
    narration_flagged: bool
