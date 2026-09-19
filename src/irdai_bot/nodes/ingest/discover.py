from __future__ import annotations

import logging

from irdai_bot.adapters import axis_max_life, hdfc_life
from irdai_bot.adapters.base import DiscoveryError, DisclosureFile
from irdai_bot.graphs.state import IngestState
from irdai_bot.scope import FISCAL_YEARS

logger = logging.getLogger(__name__)

ADAPTERS = {
    "hdfc_life": hdfc_life,
    "axis_max_life": axis_max_life,
}


def discover_documents(state: IngestState) -> dict:
    documents: list[DisclosureFile] = []
    for insurer in state["insurers"]:
        adapter = ADAPTERS[insurer]
        try:
            documents.extend(adapter.list_files(tuple(state.get("fiscal_years") or FISCAL_YEARS)))
        except DiscoveryError:
            logger.exception("Discovery failed for %s — see DiscoveryError for details", insurer)
            raise
    logger.info("Discovered %d disclosure documents across %s", len(documents), state["insurers"])
    return {"documents": documents}
