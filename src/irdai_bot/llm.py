"""Builds chat clients, switchable per-role between OpenAI and a local Ollama
server via config values (Config.mapping_model, Config.narration_model) — set
MAPPING_MODEL="ollama:<model>" / NARRATION_MODEL="ollama:<model>" (e.g.
"ollama:qwen3.8:27b") to route that role's calls to Ollama instead; any other
value is passed straight to OpenAI, unchanged from before this switch
existed. mapping_model covers normalize/mapping.py and extract/disclosures.py's
page transcription; narration_model covers the analysis agent
(graphs/analysis_graph.py). Langfuse tracing (observability.py) needs no
extra wiring for either branch — it's attached via the ambient RunnableConfig
callbacks, which LangChain propagates to any chat model regardless of class.
"""

from __future__ import annotations

import logging

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from irdai_bot.config import Config

logger = logging.getLogger(__name__)

_OLLAMA_PREFIX = "ollama:"


def _make_llm(model_spec: str, cfg: Config, role: str, **openai_kwargs) -> BaseChatModel:
    if model_spec.startswith(_OLLAMA_PREFIX):
        from langchain_ollama import ChatOllama

        model = model_spec[len(_OLLAMA_PREFIX) :]
        logger.info("USING OLLAMA for %s model: %s (%s)", role, model, cfg.ollama_base_url)
        return ChatOllama(model=model, base_url=cfg.ollama_base_url, temperature=0)

    logger.info("USING GPT for %s model: %s", role, model_spec)
    return ChatOpenAI(model=model_spec, api_key=cfg.openai_api_key, temperature=0, **openai_kwargs)


def make_mapping_llm(cfg: Config) -> BaseChatModel:
    return _make_llm(cfg.mapping_model, cfg, "mapping")


def make_narration_llm(cfg: Config) -> BaseChatModel:
    # reasoning_effort="none" is OpenAI-only (gpt-5.6-terra otherwise defaults
    # to a nonzero reasoning effort that the chat/completions endpoint refuses
    # to combine with tool calls — confirmed via a real 400: "Function tools
    # with reasoning_effort are not supported ... set reasoning_effort to
    # 'none'"); _make_llm only applies it on the ChatOpenAI branch.
    return _make_llm(cfg.narration_model, cfg, "narration", reasoning_effort="none")
