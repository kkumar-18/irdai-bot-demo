# Tools

Every tool the chat agent (the [analysis graph](agentic_doc.md#3-analysis-graph)) can call
mid-conversation — as opposed to a prompt application code calls directly on a fixed
schedule. Both tools are LangChain `@tool`-decorated closures, built once per request/CLI
invocation via a `make_*_tool()` factory (so each closes over a real config/connection
string rather than a global).

**How a call is actually executed:** the agent's LLM is given both tools via
`llm.bind_tools(tools)` ([graphs/analysis_graph.py:200](../src/irdai_bot/graphs/analysis_graph.py#L200)); when it returns a tool call, LangGraph's
prebuilt **`ToolNode`** ([graphs/analysis_graph.py:204](../src/irdai_bot/graphs/analysis_graph.py#L204)) — not custom code — matches the call by name, invokes the
corresponding Python function with the LLM-supplied arguments, and turns the return value
into a `ToolMessage` appended to the conversation. Neither tool ever raises out to
`ToolNode`'s own error handling: both catch their own exceptions and return an
`{"error": ...}` result instead, so a bad call always comes back as something the agent can
read and self-correct from, never a crashed turn.

---

### 1. `run_sql`

Runs one read-only `SELECT` against `facts`, the five derived ratio views, or
`disclosure_values`. Structurally restricted, not just by prompt instruction: parsed with
`sqlglot`, rejected unless it's a single `SELECT` referencing only the allowlisted relations
(CTEs excluded from that check), given a default `LIMIT 500`, and run over a Postgres
connection with `default_transaction_read_only=on` and a 30-second statement timeout as a
structural backstop below the allowlist. Returns the empty-result **hints** described below
instead of an empty list, when applicable.

- **Factory:** `make_run_sql_tool(database_url)`, [nodes/analysis/tools.py:184](../src/irdai_bot/nodes/analysis/tools.py#L184).
- **Validator:** `validate_query()`, [nodes/analysis/tools.py:100](../src/irdai_bot/nodes/analysis/tools.py#L100).
- **Arguments:** `query: str`.
- **No LLM call inside it** — this tool is pure SQL execution; the LLM only supplies the query text.

### 2. `fetch_disclosures`

Starts a background fetch for missing data and returns immediately — never blocks the
current turn. Accepts any insurer, any IRDAI form (`L-1`..`L-45`, any spelling —
normalized via `scope.normalize_form`), any fiscal year the insurer's site publishes.
Internally calls `fetch.request_fetch()`, which checks `fetch_log`/running jobs (deduping
concurrent identical requests via a Postgres advisory lock) and, if genuinely missing,
starts the [fetch graph](agentic_doc.md#2-fetch-graph) on a thread. The dict it returns
(`status`, `fetching`, `already_available`, `unavailable_already_checked_on_website`,
`form_did_not_exist_that_year`) is itself the mechanism that tells the agent what to say —
see its full docstring at the source link below.

- **Factory:** `make_fetch_disclosures_tool(cfg)`, [nodes/analysis/tools.py:236](../src/irdai_bot/nodes/analysis/tools.py#L236).
- **Starts:** `fetch.request_fetch()`, [fetch.py](../src/irdai_bot/fetch.py) — the actual background-thread launch and job bookkeeping.
- **Arguments:** `insurer: Literal["hdfc_life", "axis_max_life"]`, `fiscal_years: list[str]`, `forms: list[str]`.
- **No LLM call inside it either** — starting a job is synchronous and cheap; the LLM calls made *by* the job it starts belong to the fetch graph, documented separately.

---

## Deterministic hints (not prompts, not tools — but shape what the LLM does next)

Two fixed strings, injected as the **result** of a `run_sql` call rather than sent as a
prompt, added because relying on the system prompt alone to make the agent retry correctly
proved unreliable in practice:

- **`_DISCLOSURE_PROBE_HINT`** — returned instead of an empty list when a filtered
  `disclosure_values` query (one with a `row_label`/`column_label`/`measure` condition)
  finds nothing. Tells the agent to run an unfiltered `SELECT DISTINCT` probe first, since
  label wording varies by insurer and year and a filtered guess proves nothing.
- **`_NON_CURATED_FORM_HINT`** — returned when a `facts`/view query mentioning `L-22`,
  `L-32`, `L-40`, or `L-41` finds nothing, since those forms are never loaded into `facts`
  (only `L-1-A-RA` and `L-4` are). Redirects the agent to `disclosure_values` instead of
  letting it retry `facts` or assume `fetch_disclosures` will ever populate it there.

Both defined in [nodes/analysis/tools.py](../src/irdai_bot/nodes/analysis/tools.py), injected inside `run_sql`'s own body — not a separate call.
