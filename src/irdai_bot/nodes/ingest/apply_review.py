"""Writes human-resolved label mappings into label_cache, then hands control
back to normalize so it re-runs (now cache-hitting) over the same raw_cells.
"""

from __future__ import annotations

import psycopg

from irdai_bot.graphs.state import IngestState
from irdai_bot.normalize.mapping import _write_cache  # deliberately reused, not re-implemented


def make_apply_review_node(con: psycopg.Connection):
    def apply_review_decisions(state: IngestState) -> dict:
        for decision in state["resolved_review"] or []:
            _write_cache(
                con,
                decision["insurer"],
                decision["form"],
                decision["kind"],
                decision,
            )
        return {"resolved_review": []}

    return apply_review_decisions
