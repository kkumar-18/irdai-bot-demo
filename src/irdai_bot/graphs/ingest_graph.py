"""Ingest graph: discover -> [download+extract]* -> normalize -> (review)? -> validate -> load.

See /Users/krishnakumar/.claude/plans/breezy-zooming-fox.md for the design
rationale (Send fan-out per document, interrupt()-based human review gate,
PostgresSaver checkpointing). Verified against the installed langgraph==1.2.11
API surface directly (Send/Command/interrupt live in langgraph.types on this
version) before writing this.
"""

from __future__ import annotations

import logging

import psycopg
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from irdai_bot.checkpoint import get_ingest_checkpointer
from irdai_bot.config import Config
from irdai_bot.graphs.state import IngestState
from irdai_bot.llm import make_mapping_llm
from irdai_bot.nodes.ingest.apply_review import make_apply_review_node
from irdai_bot.nodes.ingest.discover import discover_documents
from irdai_bot.nodes.ingest.download_extract import make_download_and_extract_node
from irdai_bot.nodes.ingest.human_review import make_human_review_node
from irdai_bot.nodes.ingest.load import make_load_node
from irdai_bot.nodes.ingest.normalize import make_normalize_node
from irdai_bot.nodes.ingest.validate import validate_facts

logger = logging.getLogger(__name__)


def _fan_out_documents(state: IngestState) -> list[Send]:
    forms = state.get("forms") or []
    return [Send("download_and_extract", {"current_doc": d, "forms": forms}) for d in state["documents"]]


def _route_after_normalize(state: IngestState) -> str:
    return "review" if state["pending_review"] else "validate"


def _route_after_validate(state: IngestState) -> str:
    hard_error_shas = {
        e["fact"]["source_sha256"] for e in state["validation_errors"] if e["severity"] == "error"
    }
    has_loadable = any(
        f["source_sha256"] not in hard_error_shas for f in state["normalized_facts"]
    )
    return "load" if has_loadable or not state["normalized_facts"] else "fail"


def _fail_batch(state: IngestState) -> dict:
    logger.error(
        "Ingest run %s: no loadable facts — %d hard validation errors",
        state["run_id"],
        len([e for e in state["validation_errors"] if e["severity"] == "error"]),
    )
    return {}


def build_ingest_graph(cfg: Config, con: psycopg.Connection, *, interactive_review: bool = True):
    """interactive_review=False (the chat agent's on-demand fetch) drops the
    interrupt()-based review gate: nobody is at a CLI to resolve it mid-chat,
    so only labels the mapper accepted with confidence get loaded, and
    low-confidence ones simply stay out until a CLI `ingest run` reviews them.
    With no interrupt to resume, that graph also needs no durable checkpointer."""
    mapping_llm = make_mapping_llm(cfg)

    graph = StateGraph(IngestState)
    graph.add_node("discover_documents", discover_documents)
    graph.add_node("download_and_extract", make_download_and_extract_node(cfg, con))
    graph.add_node("normalize_labels", make_normalize_node(con, mapping_llm))
    if interactive_review:
        graph.add_node("human_review_gate", make_human_review_node(cfg.review_dir))
        graph.add_node("apply_review_decisions", make_apply_review_node(con))
    graph.add_node("validate_facts", validate_facts)
    graph.add_node("load_facts", make_load_node(con))
    graph.add_node("fail_batch", _fail_batch)

    graph.add_edge(START, "discover_documents")
    graph.add_conditional_edges("discover_documents", _fan_out_documents, ["download_and_extract"])
    graph.add_edge("download_and_extract", "normalize_labels")
    if interactive_review:
        graph.add_conditional_edges(
            "normalize_labels", _route_after_normalize, {"review": "human_review_gate", "validate": "validate_facts"}
        )
        graph.add_edge("human_review_gate", "apply_review_decisions")
        graph.add_edge("apply_review_decisions", "normalize_labels")
    else:
        graph.add_edge("normalize_labels", "validate_facts")
    graph.add_conditional_edges(
        "validate_facts", _route_after_validate, {"load": "load_facts", "fail": "fail_batch"}
    )
    graph.add_edge("load_facts", END)
    graph.add_edge("fail_batch", END)

    return graph.compile(checkpointer=get_ingest_checkpointer(cfg) if interactive_review else None)
