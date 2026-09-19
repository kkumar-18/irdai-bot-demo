"""Langfuse tracing for every LangGraph invocation (chat, ingest, fetch).

get_langfuse_handler() is cached so the CallbackHandler is built once per
process; it reads LANGFUSE_SECRET_KEY/LANGFUSE_PUBLIC_KEY/LANGFUSE_BASE_URL
from the environment (see .env.example) and returns [] when the keys aren't
set, so with_tracing() degrades to a no-op instead of every call site
needing its own guard.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_langfuse_handler() -> list:
    if not os.environ.get("LANGFUSE_SECRET_KEY") or not os.environ.get("LANGFUSE_PUBLIC_KEY"):
        logger.info("Langfuse tracing disabled: LANGFUSE_SECRET_KEY/LANGFUSE_PUBLIC_KEY not set")
        return []

    from langfuse.langchain import CallbackHandler

    logger.info("Langfuse tracing enabled (%s)", os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com"))
    return [CallbackHandler()]


def with_tracing(config: dict | None = None) -> dict:
    config = dict(config) if config else {}
    handler = get_langfuse_handler()
    if handler:
        config["callbacks"] = [*config.get("callbacks", []), *handler]
    return config
