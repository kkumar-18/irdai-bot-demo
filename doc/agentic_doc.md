# Agentic workflows

Every LangGraph `StateGraph` in the codebase — a graph of nodes where an LLM's own output
(a tool call, a routing decision) determines which node runs next, as opposed to a fixed
sequence of function calls. Three graphs, each built by its own `build_*_graph()` function.

---

### 1. Ingest graph

`discover_documents → [download_and_extract]* (fan-out, one per filing) → normalize_labels
→ human_review_gate → apply_review_decisions → (loops back to normalize_labels) →
validate_facts → load_facts`, with a `fail_batch` exit if nothing loadable survives
validation. The CLI's full pipeline: scrape the insurer's site, download each filing,
extract raw cells, map labels to canonical ids (prompts 1–2), pause for a human to resolve
low-confidence labels, validate plausibility, and load into `facts`. `human_review_gate` /
`apply_review_decisions` are skipped entirely when `interactive_review=False` (the fetch
graph's use of this same builder — see below) — no one is at a CLI mid-chat to resolve a
pause.

- **Built by:** `build_ingest_graph()`, [graphs/ingest_graph.py](../src/irdai_bot/graphs/ingest_graph.py).
- **Prompts used:** [1](prompt_doc.md#1-line-item-mapping-prompt), [2](prompt_doc.md#2-column-mapping-prompt) (inside `normalize_labels`).
- **Tools used:** none — this graph doesn't run inside a chat turn, so there's no LLM deciding what to call next; each node's LLM call is a fixed step in the sequence, not a tool choice.
- **Checkpointer:** Postgres (`PostgresSaver`), only when `interactive_review=True` — durable across the CLI process exiting between `ingest run` (pause) and `ingest resume`.
- **Invoked from:** `irdai-bot ingest run` / `ingest resume` ([cli.py](../src/irdai_bot/cli.py)), and indirectly by the fetch graph below.

### 2. Fetch graph

`plan_fetch → (nothing missing? skip to verify) → load_curated → transcribe_disclosures →
verify_availability → record_attempts`. The background workflow that fills a data gap the
chat agent hits: `load_curated` runs the ingest graph above (non-interactively) for the two
forms with a real fixed-layout parser (L-1-A-RA, L-4); `transcribe_disclosures` downloads
whatever's still missing and runs prompt 3 per page (up to 3 filings and 4 pages in parallel)
into `disclosure_values`; `record_attempts` logs the attempt in `fetch_log` regardless of
outcome, so a genuinely unavailable filing isn't re-scraped on every question.

- **Built by:** `build_fetch_graph()`, [graphs/fetch_graph.py](../src/irdai_bot/graphs/fetch_graph.py).
- **Prompts used:** [1](prompt_doc.md#1-line-item-mapping-prompt), [2](prompt_doc.md#2-column-mapping-prompt) (via `load_curated`'s call into the ingest graph), [3](prompt_doc.md#3-disclosure-page-transcription-prompt) (in `transcribe_disclosures`).
- **Tools used:** none, same reason as the ingest graph — no LLM routing decision inside this graph itself.
- **Checkpointer:** none (`build_ingest_graph(..., interactive_review=False)` compiles with no checkpointer either) — this graph is expected to run to completion unattended in one call.
- **Invoked from:** `fetch.py`'s `_run_job()`, on a background thread started by the [`fetch_disclosures` tool](tool_call_doc.md#2-fetch_disclosures).

### 3. Analysis graph

`agent ⇄ tools` (a ReAct loop: the agent node calls the LLM with tools bound; if it returns
a tool call, `tools` executes it and loops back to `agent`; otherwise control moves on) `→
enforce_grounding`, which either accepts the answer or — if any stated figure doesn't
trace to a `run_sql`/`fetch_disclosures` result from this turn — injects a
`[grounding-retry]` `SystemMessage` and routes back to `agent`, up to `MAX_TURNS = 10`. This
is the user-facing chat agent behind both the CLI's `ask` command and the web API's
`/api/chat`.

- **Built by:** `build_analysis_graph()`, [graphs/analysis_graph.py](../src/irdai_bot/graphs/analysis_graph.py).
- **Prompts used:** [4](prompt_doc.md#4-chat-agent-system-prompt) (the `agent` node, every turn).
- **Tools used:** [`run_sql`](tool_call_doc.md#1-run_sql), [`fetch_disclosures`](tool_call_doc.md#2-fetch_disclosures) — this is the only graph where the LLM itself chooses what to call next.
- **Checkpointer:** `MemorySaver` (in-process only) — gives real multi-turn continuity within one server process/session, not across a restart.
- **Invoked from:** `irdai-bot ask` ([cli.py](../src/irdai_bot/cli.py)) and `POST /api/chat` ([api.py](../src/irdai_bot/api.py)), which builds the graph once at import time and reuses it for every request.

---

## How they relate

```
chat question ──► analysis graph ──► run_sql (empty?) ──► fetch_disclosures tool
                                                                   │
                                                       starts, on a thread:
                                                                   ▼
                                                             fetch graph
                                                              │       │
                                                     load_curated   transcribe_disclosures
                                                              │       │
                                                        ingest graph  (own LLM calls)
                                                   (interactive_review=False)
```

The chat agent never calls the ingest graph directly — only through the fetch graph, which
runs it headless. The ingest graph's human-review path (`irdai-bot ingest run`) is the only
place a person is ever in the loop.
