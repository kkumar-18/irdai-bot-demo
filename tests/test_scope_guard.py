from langchain_core.messages import AIMessage, HumanMessage

from irdai_bot.nodes.analysis.scope_guard import (
    REFUSAL,
    ScopeVerdict,
    _classifier_input,
    make_scope_guard,
    route_after_scope_guard,
)


class _StubLLM:
    def __init__(self, verdict=None, error=None):
        self.verdict, self.error, self.seen = verdict, error, None

    def with_structured_output(self, schema):
        return self

    def invoke(self, messages):
        self.seen = messages
        if self.error:
            raise self.error
        return self.verdict


def _state(*messages):
    return {"messages": list(messages), "turn_count": 0}


def test_off_topic_question_gets_refusal_and_ends():
    guard = make_scope_guard(_StubLLM(ScopeVerdict(in_scope=False)))
    state = _state(HumanMessage("who is the president of USA"))
    update = guard(state)
    assert update["messages"][0].content == REFUSAL
    assert route_after_scope_guard(_state(*state["messages"], *update["messages"])) == "refused"


def test_insurance_question_passes_through_to_agent():
    guard = make_scope_guard(_StubLLM(ScopeVerdict(in_scope=True)))
    state = _state(HumanMessage("Compare HDFC Life and Axis Max Life solvency ratio"))
    assert guard(state) == {}
    assert route_after_scope_guard(state) == "agent"


def test_classifier_failure_fails_open_to_agent():
    guard = make_scope_guard(_StubLLM(error=ValueError("bad json")))
    assert guard(_state(HumanMessage("hdfc premium"))) == {}


def test_classifier_sees_prior_turn_for_follow_ups():
    text = _classifier_input(
        [
            HumanMessage("HDFC Life total premium by year"),
            AIMessage("", tool_calls=[{"name": "run_sql", "args": {}, "id": "1"}]),
            AIMessage("## Public Disclosure Comparison of HDFC Life\n..."),
            HumanMessage("plot that"),
        ]
    )
    assert "User: HDFC Life total premium by year" in text
    assert "Assistant: ## Public Disclosure Comparison" in text
    assert text.endswith("NEW USER MESSAGE:\nplot that")
