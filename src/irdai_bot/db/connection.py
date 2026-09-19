from __future__ import annotations

from pathlib import Path

import psycopg

_SCHEMA_DIR = Path(__file__).parent

# Guards the agent's run_sql connection against a runaway LLM-written query
# (e.g. an accidental self-join cross product) tying up the server.
_READ_ONLY_STATEMENT_TIMEOUT_MS = 30_000


def connect(database_url: str, *, read_only: bool = False, apply_schema: bool = True) -> psycopg.Connection:
    # autocommit: every statement commits on its own, so a document's
    # ingest_manifest status is durable the moment it's written and a crashed
    # run's resume correctly skips what already landed. Multi-statement writes
    # that must be atomic open an explicit con.transaction() (see load.py).
    # apply_schema=False skips re-running schema.sql/views.sql — for hot paths
    # (fetch-job polling every few seconds) once the process has applied them.
    if read_only:
        return psycopg.connect(
            database_url,
            autocommit=True,
            options=(
                "-c default_transaction_read_only=on "
                f"-c statement_timeout={_READ_ONLY_STATEMENT_TIMEOUT_MS}"
            ),
        )
    con = psycopg.connect(database_url, autocommit=True)
    if apply_schema:
        con.execute((_SCHEMA_DIR / "schema.sql").read_text())
        con.execute((_SCHEMA_DIR / "views.sql").read_text())
        con.execute((_SCHEMA_DIR / "quotes_schema.sql").read_text())
        # quotes_seed.sql is plain INSERTs with no ON CONFLICT guard (it's a
        # one-time curated dump, not an idempotent migration), so it's only
        # ever run once, the first time the insurer table is empty.
        if con.execute("SELECT 1 FROM insurer LIMIT 1").fetchone() is None:
            con.execute((_SCHEMA_DIR / "quotes_seed.sql").read_text())
    return con
