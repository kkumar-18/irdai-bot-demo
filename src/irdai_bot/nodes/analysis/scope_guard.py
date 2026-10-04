"""Pre-agent topic gate: classifies each new user message as insurance-related
or not, and answers off-topic ones with a fixed refusal before the agent (and
its tools) ever runs. The agent's own prompt (prompts/routing.md) carries the
same rules as a second line of defence.
"""

from __future__ import annotations

import logging
from pathlib import Path

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from irdai_bot.graphs.state import AnalysisState

logger = logging.getLogger(__name__)

_PROMPT = (Path(__file__).resolve().parents[2] / "prompts" / "scope_guard.md").read_text()

REFUSAL = (
    "## Out of Scope\n\n"
    "I can only answer insurance-related questions — HDFC Life and Axis Max Life "
    "public-disclosure comparisons, and their guaranteed-return product quotes. "
    "Please ask an insurance question."
)

_CONTEXT_MESSAGES = 6
_CONTEXT_CHARS = 300


class ScopeVerdict(BaseModel):
    in_scope: bool = Field(description="True only if the new user message may be answered under the rules.")


def _classifier_input(messages: list) -> str:
    question = messages[-1].content
    history = []
    for m in messages[:-1]:
        if isinstance(m, HumanMessage):
            history.append(f"User: {m.content}"[:_CONTEXT_CHARS])
        elif isinstance(m, AIMessage) and isinstance(m.content, str) and m.content and not m.tool_calls:
            history.append(f"Assistant: {m.content}"[:_CONTEXT_CHARS])
    recent = "\n".join(history[-_CONTEXT_MESSAGES:]) or "(none)"
    return f"Conversation so far:\n{recent}\n\nNEW USER MESSAGE:\n{question}"


def make_scope_guard(llm: BaseChatModel):
    classifier = llm.with_structured_output(ScopeVerdict)

    def scope_guard(state: AnalysisState) -> dict:
        try:
            verdict = classifier.invoke(
                [SystemMessage(_PROMPT), HumanMessage(_classifier_input(state["messages"]))]
            )
        except Exception:
            # Fail open: a classifier glitch (e.g. a local model emitting bad
            # JSON) shouldn't block real insurance questions, and the agent's
            # prompt still refuses off-topic ones.
            logger.exception("Scope guard failed; deferring to the agent's own scope rules")
            return {}
        if verdict.in_scope:
            return {}
        logger.info("Scope guard refused: %r", state["messages"][-1].content)
        return {"messages": [AIMessage(REFUSAL)]}

    return scope_guard


def route_after_scope_guard(state: AnalysisState) -> str:
    return "refused" if isinstance(state["messages"][-1], AIMessage) else "agent"
