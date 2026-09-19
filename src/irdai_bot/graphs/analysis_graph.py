"""Analysis graph: a single ReAct agent, dual-purpose over two independent
data sources it picks between by what each question is actually about —
public disclosure comparisons (run_sql over facts + the derived metric
views, fetch_disclosures to fill gaps from the insurers' websites; see
nodes/analysis/tools.py) and product/quote comparisons (list_products,
check_eligibility, compare_illustrations, rate_lookup etc. over the
guaranteed-return product warehouse; see nodes/quotes/tools.py) — followed by
a structural grounding check on the final narration that works the same way
across both tool sets (nodes/analysis/grounding.py).

There's deliberately no separate intent-classifier node: with well-scoped
tools and a system prompt that says which domain each tool belongs to, tool
selection (langgraph's existing ReAct loop) already does the routing a
question needs — including a follow-up question that switches domains
mid-session, without any extra state to track.

Deliberately a separate graph/process from ingest: ingest is a nightly batch
job, this is interactive Q&A over the already-loaded warehouse. Fusing them
would make both worse (see plan).

The system prompt itself lives in prompts/ (routing.md — shared, domain-agnostic
rules; public_disclosure/system_prompt.md and quotes/system_prompt.md — one
per domain), not inline here, so it can be read and edited as plain prose.
Each domain's file is a str.format() template for the schema/vocabulary cards
built in that domain's tools module.
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.messages import AIMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from irdai_bot.checkpoint import get_analysis_checkpointer
from irdai_bot.config import Config
from irdai_bot.graphs.state import AnalysisState
from irdai_bot.nodes.analysis.grounding import collect_turn_tool_values, find_ungrounded
from irdai_bot.nodes.analysis.tools import (
    FORMS_CARD,
    LINE_ITEM_VOCAB,
    SCHEMA_CARD,
    VIEW_SOURCE_FORMS,
    make_fetch_disclosures_tool,
    make_run_sql_tool,
)
from irdai_bot.nodes.quotes.tools import SCHEMA_CARD as QUOTE_SCHEMA_CARD
from irdai_bot.nodes.quotes.tools import TOOL_CARD as QUOTE_TOOL_CARD
from irdai_bot.nodes.quotes.tools import make_quote_tools
from irdai_bot.scope import FISCAL_YEARS

# Room for: query, a retry, a fetch, the re-query, the answer, and a
# grounding-retry round.
MAX_TURNS = 10

_VIEW_SOURCES = "; ".join(f"{view} -> {form}" for view, form in VIEW_SOURCE_FORMS.items())

_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"

_ROUTING_PROMPT = (_PROMPTS_DIR / "routing.md").read_text().rstrip("\n")
_DISCLOSURE_PROMPT_TEMPLATE = (_PROMPTS_DIR / "public_disclosure" / "system_prompt.md").read_text().rstrip("\n")
_QUOTE_PROMPT_TEMPLATE = (_PROMPTS_DIR / "quotes" / "system_prompt.md").read_text().rstrip("\n")

SYSTEM_PROMPT = "\n\n".join(
    [
        _ROUTING_PROMPT,
        _DISCLOSURE_PROMPT_TEMPLATE.format(
            SCHEMA_CARD=SCHEMA_CARD,
            LINE_ITEM_VOCAB=LINE_ITEM_VOCAB,
            FORMS_CARD=FORMS_CARD,
            # Not FISCAL_YEARS=FISCAL_YEARS with {FISCAL_YEARS[-1]} in the
            # template: str.format()'s field-access grammar treats a
            # non-all-digit index like "-1" as a string dict key, not a
            # negative sequence index (unlike an f-string's real Python
            # expression evaluation) — confirmed live, it raised "tuple
            # indices must be integers or slices, not str".
            FISCAL_YEAR_FIRST=FISCAL_YEARS[0],
            FISCAL_YEAR_LAST=FISCAL_YEARS[-1],
            _VIEW_SOURCES=_VIEW_SOURCES,
        ),
        _QUOTE_PROMPT_TEMPLATE.format(QUOTE_SCHEMA_CARD=QUOTE_SCHEMA_CARD, QUOTE_TOOL_CARD=QUOTE_TOOL_CARD),
    ]
)


def _agent_node(llm_with_tools):
    def agent(state: AnalysisState) -> dict:
        messages = state["messages"]
        if not messages or not isinstance(messages[0], SystemMessage):
            messages = [SystemMessage(SYSTEM_PROMPT), *messages]
        response = llm_with_tools.invoke(messages)
        return {"messages": [response], "turn_count": state.get("turn_count", 0) + 1}

    return agent


def _should_continue(state: AnalysisState) -> str:
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls and state.get("turn_count", 0) < MAX_TURNS:
        return "tools"
    return "ground"


# Distinct from the real per-turn SystemMessage prepended in _agent_node:
# this one marks an internal grounding-retry nudge so _route_after_grounding
# can recognize it without it being mistaken for a new user turn boundary
# (collect_turn_tool_results stops scanning at a HumanMessage, so the retry
# nudge deliberately is NOT one — using HumanMessage here would truncate the
# next grounding check's view of this turn's own query results).
_GROUNDING_RETRY_PREFIX = "[grounding-retry] "


def _enforce_grounding(state: AnalysisState) -> dict:
    last = state["messages"][-1]
    narration = last.content if isinstance(last.content, str) else str(last.content)
    turn_values = collect_turn_tool_values(state["messages"])
    ungrounded = find_ungrounded(narration, turn_values)
    if not ungrounded:
        return {}
    if state.get("turn_count", 0) >= MAX_TURNS:
        return {
            "messages": [
                AIMessage(
                    "I can't confidently ground this answer in the queried data "
                    f"(unverified figures: {ungrounded}) — please rephrase or narrow the question."
                )
            ],
            "narration_flagged": True,
        }
    return {
        "messages": [
            SystemMessage(
                _GROUNDING_RETRY_PREFIX
                + f"Your answer stated {ungrounded}, which do not match any value returned by "
                "run_sql this turn. Revise your answer using only values you have actually queried."
            )
        ]
    }


def _route_after_grounding(state: AnalysisState) -> str:
    last = state["messages"][-1]
    if isinstance(last, SystemMessage) and last.content.startswith(_GROUNDING_RETRY_PREFIX):
        return "agent"
    return END


def build_analysis_graph(cfg: Config):
    # reasoning_effort="none": gpt-5.6-terra defaults to a nonzero reasoning
    # effort that the chat/completions endpoint refuses to combine with tool
    # calls (confirmed via a real 400: "Function tools with reasoning_effort
    # are not supported ... set reasoning_effort to 'none'").
    llm = ChatOpenAI(
        model=cfg.narration_model, api_key=cfg.openai_api_key, temperature=0, reasoning_effort="none"
    )
    tools = [
        make_run_sql_tool(cfg.database_url),
        make_fetch_disclosures_tool(cfg),
        *make_quote_tools(cfg.database_url),
    ]
    llm_with_tools = llm.bind_tools(tools)

    graph = StateGraph(AnalysisState)
    graph.add_node("agent", _agent_node(llm_with_tools))
    graph.add_node("tools", ToolNode(tools))
    graph.add_node("enforce_grounding", _enforce_grounding)

    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", _should_continue, {"tools": "tools", "ground": "enforce_grounding"})
    graph.add_edge("tools", "agent")
    graph.add_conditional_edges("enforce_grounding", _route_after_grounding, {"agent": "agent", END: END})

    return graph.compile(checkpointer=get_analysis_checkpointer())
