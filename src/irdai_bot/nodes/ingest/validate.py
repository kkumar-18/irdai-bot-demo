from __future__ import annotations

import logging

from irdai_bot.graphs.state import IngestState
from irdai_bot.normalize.validators import validate_fact

logger = logging.getLogger(__name__)


def validate_facts(state: IngestState) -> dict:
    errors: list[dict] = []
    for fact in state["normalized_facts"]:
        for issue in validate_fact(fact):
            errors.append({"severity": issue.severity, "message": issue.message, "fact": fact})
            if issue.severity == "warning":
                logger.warning(issue.message)
    hard_errors = [e for e in errors if e["severity"] == "error"]
    if hard_errors:
        logger.error("%d hard validation errors — see validation_errors", len(hard_errors))
    return {"validation_errors": errors}
