from __future__ import annotations

import json
import logging
from pathlib import Path

from langgraph.types import interrupt

from irdai_bot.graphs.state import IngestState

logger = logging.getLogger(__name__)


def _review_path(review_dir: Path, run_id: str) -> Path:
    return review_dir / f"pending_{run_id}.json"


def make_human_review_node(review_dir: Path):
    def human_review_gate(state: IngestState) -> dict:
        path = _review_path(review_dir, state["run_id"])
        path.write_text(json.dumps(state["pending_review"], indent=2, default=str))
        logger.warning(
            "%d labels need review. Edit %s (set canonical_id/segment_id/period as appropriate, "
            "confidence >= 0.85) then run `irdai-bot ingest resume --run-id %s`.",
            len(state["pending_review"]),
            path,
            state["run_id"],
        )
        decisions = interrupt(
            {"pending_review": state["pending_review"], "review_file": str(path)}
        )
        return {"resolved_review": decisions, "pending_review": []}

    return human_review_gate
