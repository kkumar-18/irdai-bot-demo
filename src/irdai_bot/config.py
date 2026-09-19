from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Config:
    openai_api_key: str
    mapping_model: str
    ollama_base_url: str
    narration_model: str
    data_dir: Path
    database_url: str

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def review_dir(self) -> Path:
        return self.data_dir / "review"


def load_config() -> Config:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Copy .env.example to .env and fill it in."
        )
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and fill it in."
        )
    # Resolved against the project root, never the current working
    # directory: a relative IRDAI_BOT_DATA_DIR (the .env ships "./data")
    # otherwise silently points at nothing when the server is launched from
    # anywhere else — confirmed live, starting uvicorn from frontend/ made
    # every question answer "the database file cannot be opened".
    data_dir = Path(os.environ.get("IRDAI_BOT_DATA_DIR", "data"))
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir
    cfg = Config(
        openai_api_key=api_key,
        # "ollama:<model>" (e.g. "ollama:qwen3.8:27b") routes the mapping
        # model to a local Ollama server instead of OpenAI — see
        # irdai_bot/llm.py:make_mapping_llm. Narration (graphs/analysis_graph.py)
        # always stays on OpenAI; this switch is mapping-only.
        mapping_model=os.environ.get("MAPPING_MODEL", "gpt-5.6-luna"),
        ollama_base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
        narration_model=os.environ.get("NARRATION_MODEL", "gpt-5.6-terra"),
        data_dir=data_dir,
        database_url=database_url,
    )
    for d in (cfg.raw_dir, cfg.review_dir):
        d.mkdir(parents=True, exist_ok=True)
    return cfg
