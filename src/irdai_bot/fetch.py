"""Background fetch jobs for missing data.

The chat agent's fetch_disclosures tool calls request_fetch(), which returns
immediately: if the requested data is missing (and wasn't tried recently) it
records a job in fetch_jobs and runs the fetch workflow (graphs/fetch_graph.py)
on a background thread. The chat reply goes out right away; the frontend polls
get_fetch_job() via GET /api/fetch-jobs/{job_id} and re-asks the question once
the job has found new data.

Jobs run on threads inside the API process, so a server restart abandons a
running job. Each job's thread bumps heartbeat_at every HEARTBEAT_INTERVAL;
a 'running' job whose heartbeat is older than HEARTBEAT_TIMEOUT is dead
(confirmed live: restarting uvicorn mid-fetch left jobs 'running' forever
and every Reload kept waiting on them). get_fetch_job reports those as
failed, which makes the frontend re-ask and start a fresh job.

While a job runs, get_fetch_job reports what has already landed, so the
frontend can show partial results instead of waiting for every filing.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, wait

from psycopg.types.json import Jsonb

from irdai_bot.config import Config
from irdai_bot.db.connection import connect
from irdai_bot.graphs.fetch_graph import build_fetch_graph, plan_fetch
from irdai_bot.observability import with_tracing
from irdai_bot.scope import normalize_form

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = dt.timedelta(seconds=10)
HEARTBEAT_TIMEOUT = dt.timedelta(seconds=60)

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fetch-job")
_futures: dict[str, Future] = {}


def request_fetch(cfg: Config, *, insurer: str, fiscal_years: list[str], forms: list[str]) -> dict:
    with connect(cfg.database_url, apply_schema=False) as con:
        plan = plan_fetch(con, insurer, fiscal_years, forms)
        if not plan["to_fetch"]:
            return {
                "status": "no_fetch_needed",
                "insurer": insurer,
                "already_available": plan["available"],
                "unavailable_already_checked_on_website": plan["recently_tried"],
                "form_did_not_exist_that_year": plan["not_applicable"],
            }

        start = False
        with con.transaction():
            # Serializes check-then-insert per insurer, so two questions (or two
            # parallel tool calls) asking for the same gap share one job.
            con.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [f"fetch_jobs:{insurer}"])
            row = con.execute(
                """
                SELECT job_id, fetching FROM fetch_jobs
                WHERE insurer = %s AND status = 'running' AND COALESCE(heartbeat_at, started_at) > %s
                  AND fetching @> %s
                ORDER BY started_at DESC LIMIT 1
                """,
                [insurer, dt.datetime.now(dt.timezone.utc) - HEARTBEAT_TIMEOUT, plan["to_fetch"]],
            ).fetchone()
            if row:
                job_id, fetching = str(row[0]), row[1]
            else:
                job_id, fetching, start = str(uuid.uuid4()), plan["to_fetch"], True
                con.execute(
                    """
                    INSERT INTO fetch_jobs (job_id, insurer, fiscal_years, forms, fetching, status, started_at, heartbeat_at)
                    VALUES (%s, %s, %s, %s, %s, 'running', now(), now())
                    """,
                    [job_id, insurer, fiscal_years, forms, fetching],
                )

    if start:
        _futures[job_id] = _executor.submit(_run_job, cfg, job_id, insurer, fiscal_years, forms)
    return {
        "status": "fetching_in_background",
        "job_id": job_id,
        "insurer": insurer,
        "fetching": fetching,
        "already_available": plan["available"],
        "unavailable_already_checked_on_website": plan["recently_tried"],
        "form_did_not_exist_that_year": plan["not_applicable"],
    }


def _heartbeat(database_url: str, job_id: str, stop: threading.Event) -> None:
    with connect(database_url, apply_schema=False) as con:
        while not stop.wait(HEARTBEAT_INTERVAL.total_seconds()):
            con.execute("UPDATE fetch_jobs SET heartbeat_at = now() WHERE job_id = %s", [job_id])


def _run_job(cfg: Config, job_id: str, insurer: str, fiscal_years: list[str], forms: list[str]) -> None:
    stop = threading.Event()
    threading.Thread(target=_heartbeat, args=(cfg.database_url, job_id, stop), daemon=True).start()
    try:
        with connect(cfg.database_url, apply_schema=False) as con:
            final = build_fetch_graph(cfg, con).invoke(
                {"job_id": job_id, "insurer": insurer, "fiscal_years": fiscal_years, "forms": forms},
                config=with_tracing(),
            )
            result = {
                k: final.get(k)
                for k in (
                    "found_new_data", "now_available", "still_unavailable", "not_applicable",
                    "filings_found", "facts_loaded", "values_transcribed", "labels_left_for_review",
                )
            }
            con.execute(
                "UPDATE fetch_jobs SET status = 'succeeded', result = %s, finished_at = now() WHERE job_id = %s",
                [Jsonb(result), job_id],
            )
            logger.info("Fetch job %s finished: %s", job_id, result)
    except Exception as e:  # network failures, site markup changes (DiscoveryError), etc.
        logger.exception("Fetch job %s failed", job_id)
        with connect(cfg.database_url, apply_schema=False) as con:
            con.execute(
                "UPDATE fetch_jobs SET status = 'failed', error = %s, finished_at = now() WHERE job_id = %s",
                [f"{type(e).__name__}: {e}", job_id],
            )
    finally:
        stop.set()


def get_fetch_job(database_url: str, job_id: str) -> dict | None:
    try:
        uuid.UUID(job_id)
    except ValueError:
        return None
    with connect(database_url, apply_schema=False) as con:
        row = con.execute(
            "SELECT job_id, insurer, fiscal_years, forms, fetching, status, result, error, started_at, "
            "finished_at, heartbeat_at, progress FROM fetch_jobs WHERE job_id = %s",
            [job_id],
        ).fetchone()
        if row is None:
            return None
        (job_id, insurer, fiscal_years, forms, fetching, status, result, error,
         started_at, finished_at, heartbeat_at, progress) = row
        if status == "running" and (heartbeat_at or started_at) < dt.datetime.now(dt.timezone.utc) - HEARTBEAT_TIMEOUT:
            status, error = "failed", "The fetch stopped before finishing (the server was restarted). Asking again starts a new one."
        if status == "running":
            # Partial results: whatever of this job's pairs has already landed.
            plan = plan_fetch(con, insurer, fiscal_years, [normalize_form(f) for f in forms])
            now_available = [p for p in plan["available"] if p in set(fetching)]
            result = {"found_new_data": bool(now_available), "now_available": now_available, "still_unavailable": []}
    result = result or {}
    return {
        "job_id": str(job_id),
        "insurer": insurer,
        "status": status,
        "fetching": fetching,
        "found_new_data": result.get("found_new_data", False),
        "now_available": result.get("now_available", []),
        "still_unavailable": result.get("still_unavailable", []),
        "progress": progress or {},
        "error": error,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat() if finished_at else None,
    }


def wait_for_jobs(job_ids: list[str]) -> None:
    """Block until the given jobs started by THIS process finish (CLI use —
    the process would otherwise exit and abandon them)."""
    wait([_futures[j] for j in job_ids if j in _futures])
