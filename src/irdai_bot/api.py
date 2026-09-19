"""HTTP API for the frontend chat UI. Run with:

    uv run uvicorn irdai_bot.api:app --reload --port 8000

The analysis graph is built once at import time (not per-request) so its
MemorySaver checkpointer gives real multi-turn continuity across chat
messages within the same server process — see graphs/analysis_graph.py.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from irdai_bot.config import load_config
from irdai_bot.db.connection import connect
from irdai_bot.fetch import get_fetch_job
from irdai_bot.graphs.analysis_graph import build_analysis_graph
from irdai_bot.nodes.analysis.chart import ChartSpec, derive_chart_specs
from irdai_bot.nodes.analysis.grounding import (
    collect_turn_fetch_jobs,
    collect_turn_queries,
    collect_turn_tool_results,
)
from irdai_bot.observability import with_tracing

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

cfg = load_config()
connect(cfg.database_url).close()  # apply schema.sql/views.sql once; hot paths skip it
graph = build_analysis_graph(cfg)

app = FastAPI(title="irdai-bot chat API")
app.add_middleware(
    CORSMiddleware,
    # Vite's default dev port. The frontend also proxies /api -> this
    # server in dev (vite.config.ts), so this is the fallback path for
    # any non-proxied access (e.g. hitting the API directly from a browser
    # tab, or a future non-Vite-proxied deployment).
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str
    session_id: str


class FetchJobRef(BaseModel):
    job_id: str
    insurer: str
    fetching: list[str]  # "fy form" pairs being fetched


class ChatResponse(BaseModel):
    answer: str
    flagged: bool
    # One chart per distinctly-shaped run_sql result this turn (see
    # nodes/analysis/chart.py:derive_chart_specs) — empty when nothing this
    # turn was chartable, more than one entry for a "plot this as separate
    # breakdowns" question that ran one query per breakdown.
    charts: list[ChartSpec]
    # Non-empty when the answer is missing data that's now being fetched in
    # the background — the frontend shows a waiting state and polls these.
    fetch_jobs: list[FetchJobRef]


class FetchJobStatus(BaseModel):
    job_id: str
    insurer: str
    status: str  # 'running' | 'succeeded' | 'failed'
    fetching: list[str]
    found_new_data: bool
    now_available: list[str]
    still_unavailable: list[str]
    progress: dict  # {"stage", "filings_done", "filings_total"} while running
    error: str | None
    started_at: str
    finished_at: str | None


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/chat")
def chat(req: ChatRequest) -> ChatResponse:
    config = with_tracing({"configurable": {"thread_id": req.session_id}})
    result = graph.invoke(
        {"messages": [HumanMessage(req.message)], "turn_count": 0},
        config=config,
    )
    turn_results = collect_turn_tool_results(result["messages"])
    turn_queries = collect_turn_queries(result["messages"])
    return ChatResponse(
        answer=result["messages"][-1].content,
        flagged=result.get("narration_flagged", False),
        charts=derive_chart_specs(turn_results, last_query=turn_queries[-1] if turn_queries else None),
        fetch_jobs=[
            FetchJobRef(job_id=j["job_id"], insurer=j["insurer"], fetching=j["fetching"])
            for j in collect_turn_fetch_jobs(result["messages"])
        ],
    )


@app.get("/api/fetch-jobs/{job_id}")
def fetch_job_status(job_id: str) -> FetchJobStatus:
    job = get_fetch_job(cfg.database_url, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown fetch job")
    return FetchJobStatus(**job)
