from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

CANON_DIR = Path(__file__).parent / "canon"


@lru_cache(maxsize=None)
def _load_yaml(name: str) -> dict:
    with open(CANON_DIR / f"{name}.yaml") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=None)
def load_form_canon(form: str) -> dict:
    data = _load_yaml(form)
    if data.get("uses_shared_segments"):
        data = {**data, "segments": _load_yaml("segments")["segments"]}
    return data


def line_item_ids(form: str) -> list[str]:
    return [li["id"] for li in load_form_canon(form)["line_items"]]


def segment_ids(form: str) -> list[str]:
    return [s["id"] for s in load_form_canon(form).get("segments", [])]


def form_info(number: int) -> dict:
    """{key, title, versions} for an IRDAI form number, from forms.yaml."""
    info = _load_yaml("forms")["forms"][number]
    return {"versions": ["v2010", "v2021"], **info}


def all_form_numbers() -> list[int]:
    return sorted(_load_yaml("forms")["forms"])
