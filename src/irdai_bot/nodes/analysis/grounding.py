"""Structural anti-hallucination check: every number the agent states in its
final narration must actually appear among the values its run_sql calls
returned this turn. This is enforced by extracting and comparing numeric
tokens, not by asking the model to self-police via a prompt instruction.
"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

_NUMBER_RE = re.compile(r"-?\d[\d,]*\.?\d*%?")
_TOLERANCE = 0.01  # relative tolerance for rounding in prose ("₹79,387 crore" from 79386.6...)


def collect_turn_tool_results(messages: list[AnyMessage]) -> list[list[dict]]:
    """The run_sql results from the CURRENT turn only — scans backward from
    the end of the conversation until the last HumanMessage.

    Why this exists rather than reading an accumulated state field: with a
    persistent multi-turn chat session (MemorySaver reused across HTTP
    requests, not a fresh process per question like the CLI), a plain
    operator.add-reducer field would accumulate every prior turn's query
    results forever, and this same turn's grounding check would then treat
    an earlier, unrelated turn's numbers as valid grounding evidence. This
    derives fresh from the authoritative message history every time
    instead, so it's correct regardless of how many turns preceded it."""
    results: list[list[dict]] = []
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            break
        if not isinstance(m, ToolMessage):
            continue
        try:
            parsed = json.loads(m.content) if isinstance(m.content, str) else m.content
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, list):
            results.append(parsed)
    return results


def collect_turn_tool_values(messages: list[AnyMessage]) -> list[Any]:
    """Every ToolMessage's parsed content from the current turn, whatever its
    shape — list-of-rows from run_sql-style tools (facts/derived views,
    v_product_catalog etc.), or a nested dict from a quote tool like
    get_product_rules/rate_lookup. Same turn-boundary logic as
    collect_turn_tool_results, but deliberately not filtered to list-shaped
    results only: the grounding check (find_ungrounded) must validate numbers
    regardless of which tool's result shape they came from. Chart derivation
    still uses collect_turn_tool_results, which stays list-shaped only —
    a nested dict result isn't a row set to chart."""
    values: list[Any] = []
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            break
        if not isinstance(m, ToolMessage):
            continue
        try:
            parsed = json.loads(m.content) if isinstance(m.content, str) else m.content
        except (json.JSONDecodeError, TypeError):
            continue
        values.append(parsed)
    return values


def collect_turn_queries(messages: list[AnyMessage]) -> list[str]:
    """The raw run_sql query strings the agent issued this turn, oldest
    first — same turn-boundary logic as collect_turn_tool_results (scans
    back to the last HumanMessage), read from each tool-calling AIMessage's
    tool_calls instead of the ToolMessage results. Used by
    nodes/analysis/chart.py to recover which insurer a query filtered on
    when the SELECT list itself doesn't include an `insurer` column (a
    single-insurer question has no reason to select a column it's already
    filtering to one value) — without this, that chart's series couldn't be
    colored by entity at all."""
    queries: list[str] = []
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            break
        if not isinstance(m, AIMessage) or not m.tool_calls:
            continue
        for call in m.tool_calls:
            if call.get("name") == "run_sql":
                query = call.get("args", {}).get("query")
                if query:
                    queries.append(query)
    queries.reverse()
    return queries


def collect_turn_fetch_jobs(messages: list[AnyMessage]) -> list[dict]:
    """Background fetch jobs the agent started (or joined) this turn, from
    fetch_disclosures results — same turn boundary as the collectors above.
    The API hands these to the frontend so it can show the waiting state and
    poll each job."""
    jobs: dict[str, dict] = {}
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            break
        if not isinstance(m, ToolMessage) or m.name != "fetch_disclosures":
            continue
        try:
            parsed = json.loads(m.content) if isinstance(m.content, str) else m.content
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict) and parsed.get("status") == "fetching_in_background":
            jobs.setdefault(parsed["job_id"], parsed)
    return list(reversed(jobs.values()))


def _normalize(token: str) -> float | None:
    token = token.strip().rstrip("%").replace(",", "")
    try:
        return float(token)
    except ValueError:
        return None


def extract_numbers(text: str) -> set[float]:
    out = set()
    for m in _NUMBER_RE.finditer(text):
        n = _normalize(m.group())
        if n is not None:
            out.add(n)
    return out


def _flatten_numbers(obj: Any) -> set[float]:
    """Recursively pulls every int/float leaf out of an arbitrarily-nested
    tool result, so a dict-of-sub-lists shape (get_product_rules) grounds
    numbers the same way a plain list of rows (run_sql) does."""
    out: set[float] = set()
    if isinstance(obj, dict):
        for v in obj.values():
            out |= _flatten_numbers(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= _flatten_numbers(v)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out.add(float(obj))
    return out


def collect_result_values(tool_results: list[Any]) -> set[float]:
    values: set[float] = set()
    for result in tool_results:
        values |= _flatten_numbers(result)
    return values


def find_ungrounded(narration: str, tool_results: list[Any]) -> list[float]:
    stated = extract_numbers(narration)
    grounded = collect_result_values(tool_results)
    # Also allow small integers (row counts, ordinals, FY-like numbers) and
    # values derived from simple rounding of a grounded value.
    ungrounded = []
    for n in stated:
        if any(abs(n - g) <= _TOLERANCE * max(abs(g), 1) for g in grounded):
            continue
        if abs(n) < 100 and n == int(n):
            continue  # likely an ordinal/count fragment, not a claimed fact
        if 1990 <= n <= 2099 and n == int(n):
            continue  # a bare year (confirmed real false-positive: "FY 2025-26" -> 2025.0)
        ungrounded.append(n)
    return ungrounded
