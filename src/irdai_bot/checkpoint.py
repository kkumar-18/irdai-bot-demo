from __future__ import annotations

import psycopg
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row

from irdai_bot.config import Config


def get_ingest_checkpointer(cfg: Config) -> PostgresSaver:
    """Durable checkpointer, required (not optional) for the ingest graph:
    interrupt()/Command(resume=...) needs state to survive the CLI process
    exiting between `ingest run` (pauses for label review) and `ingest resume`."""
    # autocommit/prepare_threshold/dict_row are what PostgresSaver requires of
    # a connection it doesn't open itself.
    conn = psycopg.connect(cfg.database_url, autocommit=True, prepare_threshold=0, row_factory=dict_row)
    saver = PostgresSaver(conn)
    saver.setup()  # idempotent: creates/migrates the checkpoint tables
    return saver


def get_analysis_checkpointer() -> MemorySaver:
    """Each `ask` invocation is a fresh turn — no cross-process durability
    needed. Swap for a PostgresSaver keyed by a --session id if multi-turn
    continuity across separate CLI invocations becomes a requirement."""
    return MemorySaver()
