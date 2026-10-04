# Chat request sequence

What happens when someone asks the bot a question in the web UI: from the browser, through
`POST /api/chat`, the analysis graph's ReAct loop and grounding check, to the response. If
data is missing, a background fetch job also runs and the browser polls it.

Participants:

- **Frontend**: the React chat UI ([frontend/](../frontend/)), served by Vite with `/api` proxied to the API.
- **API**: FastAPI app ([api.py](../src/irdai_bot/api.py)). It builds the analysis graph once at import time.
- **Analysis graph**: [graphs/analysis_graph.py](../src/irdai_bot/graphs/analysis_graph.py). Nodes are `agent`, `tools` and `enforce_grounding`. Turn state is kept in a `MemorySaver` keyed by `session_id`.
- **Narration LLM**: `NARRATION_MODEL`, which is OpenAI or `ollama:<model>` ([llm.py](../src/irdai_bot/llm.py)).
- **Tools**: `run_sql`, `fetch_disclosures` and the quote tools ([nodes/analysis/tools.py](../src/irdai_bot/nodes/analysis/tools.py), [nodes/quotes/tools.py](../src/irdai_bot/nodes/quotes/tools.py)).
- **Postgres**: the facts warehouse, metric views, `disclosure_values`, the product tables and `fetch_jobs`.
- **Fetch job**: a background thread running the fetch graph ([fetch.py](../src/irdai_bot/fetch.py), [graphs/fetch_graph.py](../src/irdai_bot/graphs/fetch_graph.py)).
- **Langfuse**: traces every LLM call through the callback handler that `with_tracing()` attaches ([observability.py](../src/irdai_bot/observability.py)).

## Main flow

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant FE as Frontend (React)
    participant API as FastAPI /api/chat
    participant G as Analysis graph
    participant LLM as Narration LLM<br/>(OpenAI or Ollama)
    participant T as Tools
    participant DB as Postgres
    participant LF as Langfuse

    User->>FE: Types a question
    FE->>API: POST /api/chat {message, session_id}
    API->>API: with_tracing({thread_id: session_id})
    API->>G: graph.invoke({messages:[HumanMessage], turn_count:0}, config)
    G->>G: Load prior messages for thread_id (MemorySaver)

    G->>LLM: scope_guard: is this an insurance question? (structured output)
    LLM-->>LF: trace generation (callback)
    LLM-->>G: ScopeVerdict {in_scope}
    opt in_scope = false
        G-->>API: Fixed "Out of Scope" reply (no agent, no tools)
        API-->>FE: ChatResponse {answer: refusal, charts: [], fetch_jobs: []}
        FE-->>User: Render refusal
    end

    loop ReAct loop (up to MAX_TURNS = 10)
        G->>G: agent node: prepend system prompt (routing + disclosure + quotes)
        G->>LLM: invoke(messages) with tools bound
        LLM-->>LF: trace generation (callback)
        LLM-->>G: AIMessage (tool_calls or final answer)

        alt AIMessage has tool_calls
            G->>T: ToolNode executes each call
            alt run_sql
                T->>DB: Validated read-only SELECT over facts / views / disclosure_values
                DB-->>T: rows
            else quote tools (list_products, check_eligibility, compare_illustrations, rate_lookup, ...)
                T->>DB: Query product / quote tables
                DB-->>T: rows
            else fetch_disclosures
                T->>DB: plan_fetch: what is already available or recently tried
                opt Data missing and not recently tried
                    T->>DB: INSERT fetch_jobs (status = running)
                    T-)T: Submit background fetch job (see below)
                end
                T-->>G: {status, fetching, already_available, ...} (returns immediately)
            end
            T-->>G: ToolMessage(s)
        else Final answer (no tool_calls)
            G->>G: enforce_grounding: check each figure in the answer<br/>against this turn's tool results
            alt Ungrounded figures and turn_count < MAX_TURNS
                G->>G: Append [grounding-retry] SystemMessage, go back to agent
            else Ungrounded and out of turns
                G->>G: Replace answer with "can't confidently ground", narration_flagged = true
            else All figures grounded
                G->>G: END
            end
        end
    end

    G-->>API: Final state (messages, narration_flagged)
    API->>API: collect_turn_tool_results / queries / fetch_jobs
    API->>API: derive_chart_specs (heuristic, no LLM)
    API-->>FE: ChatResponse {answer, flagged, charts[], fetch_jobs[]}
    FE-->>User: Render answer, charts, flag badge
```

## Background fetch, when `fetch_jobs` is non-empty

The chat reply has already been returned. The fetch job keeps running on a thread inside the
API process while the frontend polls it.

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant FE as Frontend (React)
    participant API as FastAPI
    participant J as Fetch job thread
    participant W as Insurer website
    participant M as Mapping LLM<br/>(OpenAI or Ollama)
    participant DB as Postgres
    participant LF as Langfuse

    Note over J: Started by fetch_disclosures in the main flow
    par Heartbeat
        loop every 10s
            J->>DB: UPDATE fetch_jobs.heartbeat_at
        end
    and Fetch graph
        J->>J: plan_fetch
        J->>W: Discover and download filings (requests + BeautifulSoup)
        W-->>J: PDFs
        J->>J: load_curated: ingest graph for curated forms (L-1-A-RA, L-4)
        J->>M: Label / column mapping
        M-->>LF: trace generation
        J->>DB: Load into facts
        J->>M: transcribe_disclosures: transcribe each remaining page
        M-->>LF: trace generation
        J->>DB: Insert disclosure_values
        J->>DB: verify_availability, then record_attempts in fetch_log
        J->>DB: UPDATE fetch_jobs (succeeded / failed, now_available, still_unavailable)
    end

    FE-->>User: Show "fetching in background" state
    loop Poll until the job finishes
        FE->>API: GET /api/fetch-jobs/{job_id}
        API->>DB: get_fetch_job (stale heartbeat older than 60s counts as failed)
        DB-->>API: status, progress, now_available
        API-->>FE: FetchJobStatus
    end
    opt found_new_data
        FE->>API: Re-ask the question: POST /api/chat (same session_id)
        Note over API: Runs the main flow again, and run_sql now finds the data
    end
```

## Notes

- **Session memory** lives in process. `MemorySaver` keeps a session's messages across chat
  turns, but they are lost when the server restarts.
- **Tracing** applies to both models. The Langfuse callback rides on the ambient
  `RunnableConfig`, so OpenAI and Ollama calls are traced the same way. This includes calls
  made on the fetch job's worker threads, because of `contextvars.copy_context()`.
- **Steps that use no LLM**: SQL validation, the grounding check, chart derivation and PDF
  layout extraction are all deterministic.
