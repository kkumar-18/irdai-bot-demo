"""Builds the mapping-model chat client, switchable between OpenAI and a
local Ollama server via one config value (Config.mapping_model) — set
MAPPING_MODEL="ollama:<model>" (e.g. "ollama:qwen3.8:27b") to route mapping
calls (normalize/mapping.py, extract/disclosures.py's page transcription) to
Ollama instead; any other value is passed straight to OpenAI, unchanged from
before this switch existed. The narration agent (graphs/analysis_graph.py)
is not affected by this — it always uses OpenAI.
"""

from __future__ import annotations

import logging

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from irdai_bot.config import Config

logger = logging.getLogger(__name__)

_OLLAMA_PREFIX = "ollama:"


def make_mapping_llm(cfg: Config) -> BaseChatModel:
    if cfg.mapping_model.startswith(_OLLAMA_PREFIX):
        from langchain_ollama import ChatOllama

        model = cfg.mapping_model[len(_OLLAMA_PREFIX) :]
        logger.info("USING OLLAMA for mapping model: %s (%s)", model, cfg.ollama_base_url)
        return ChatOllama(model=model, base_url=cfg.ollama_base_url, temperature=0)

    logger.info("USING GPT for mapping model: %s", cfg.mapping_model)
    return ChatOpenAI(model=cfg.mapping_model, api_key=cfg.openai_api_key, temperature=0)
