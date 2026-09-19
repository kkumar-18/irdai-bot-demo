from __future__ import annotations

import json
import logging

import click
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from irdai_bot.config import load_config
from irdai_bot.db.connection import connect
from irdai_bot.fetch import get_fetch_job, wait_for_jobs
from irdai_bot.graphs.analysis_graph import build_analysis_graph
from irdai_bot.graphs.ingest_graph import build_ingest_graph
from irdai_bot.nodes.analysis.grounding import collect_turn_fetch_jobs
from irdai_bot.observability import with_tracing
from irdai_bot.scope import INSURERS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@click.group()
def cli():
    pass


@cli.group()
def ingest():
    """Scrape, extract, normalize, and load disclosure data."""


@ingest.command("run")
@click.option("--run-id", required=True, help="Identifier for this ingest run (also the checkpoint thread id).")
@click.option("--insurer", "insurers", multiple=True, type=click.Choice(INSURERS), help="Restrict to specific insurer(s); default is both.")
def ingest_run(run_id: str, insurers: tuple[str, ...]):
    cfg = load_config()
    con = connect(cfg.database_url)
    graph = build_ingest_graph(cfg, con)
    config = with_tracing({"configurable": {"thread_id": run_id}})
    result = graph.invoke(
        {
            "run_id": run_id,
            "insurers": list(insurers) or list(INSURERS),
            "fiscal_years": [],
            "forms": [],
            "documents": [],
            "current_doc": None,
            "downloaded": [],
            "raw_cells": [],
            "normalized_facts": [],
            "pending_review": [],
            "resolved_review": [],
            "validation_errors": [],
            "facts_loaded": 0,
        },
        config=config,
    )
    if "__interrupt__" in result:
        click.echo(f"Paused for human review. Run `irdai-bot ingest resume --run-id {run_id}` after editing the review file.")
    else:
        click.echo(f"Ingest run {run_id} complete: {result.get('facts_loaded', 0)} facts loaded.")
    con.close()


@ingest.command("resume")
@click.option("--run-id", required=True)
def ingest_resume(run_id: str):
    cfg = load_config()
    con = connect(cfg.database_url)
    graph = build_ingest_graph(cfg, con)
    config = with_tracing({"configurable": {"thread_id": run_id}})

    review_path = cfg.review_dir / f"pending_{run_id}.json"
    resolved = json.loads(review_path.read_text()) if review_path.exists() else []

    result = graph.invoke(Command(resume=resolved), config=config)
    if "__interrupt__" in result:
        click.echo(f"Still paused for review — more labels need attention in {review_path}.")
    else:
        click.echo(f"Ingest run {run_id} complete: {result.get('facts_loaded', 0)} facts loaded.")
    con.close()


@cli.command()
@click.argument("question")
@click.option("--session", default="default", help="Conversation thread id, for multi-turn continuity within one process.")
def ask(question: str, session: str):
    cfg = load_config()
    connect(cfg.database_url).close()  # apply schema once
    graph = build_analysis_graph(cfg)
    config = with_tracing({"configurable": {"thread_id": session}})
    result = graph.invoke({"messages": [HumanMessage(question)], "turn_count": 0}, config=config)
    click.echo(result["messages"][-1].content)

    # Unlike the API server, this process exits after answering, which would
    # abandon background fetch jobs — so wait for them, then answer again if
    # they found anything (the CLI equivalent of the chat UI's Reload).
    job_ids = [j["job_id"] for j in collect_turn_fetch_jobs(result["messages"])]
    if not job_ids:
        return
    click.echo("\nFetching missing data from the insurer's website — this can take a minute or two…")
    wait_for_jobs(job_ids)
    jobs = [get_fetch_job(cfg.database_url, j) for j in job_ids]
    for job in jobs:
        if job and job["status"] == "failed":
            click.echo(f"Fetch for {job['insurer']} failed: {job['error']}")
    if any(job and job["found_new_data"] for job in jobs):
        result = graph.invoke({"messages": [HumanMessage(question)], "turn_count": 0}, config=config)
        click.echo("\n" + result["messages"][-1].content)
    else:
        click.echo("No new data was found on the insurer's website.")


if __name__ == "__main__":
    cli()
