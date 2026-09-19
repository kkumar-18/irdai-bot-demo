from __future__ import annotations

import dataclasses
import hashlib
import logging
import re
from pathlib import Path

import psycopg
import requests

from irdai_bot.adapters.base import DisclosureFile
from irdai_bot.config import Config
from irdai_bot.extract.router import extract
from irdai_bot.graphs.state import IngestState

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _local_filename(insurer: str, fy: str, url: str) -> str:
    base = url.rsplit("/", 1)[-1].split("?")[0]
    return f"{insurer}_{fy}_{_UNSAFE_CHARS.sub('_', base)}"


def download_filing(cfg: Config, doc: DisclosureFile) -> tuple[Path, str]:
    """Download (once — reused from data/raw after) and return (path, sha256)."""
    local_path = cfg.raw_dir / _local_filename(doc.insurer, doc.fy, doc.url)
    if not local_path.exists():
        resp = requests.get(doc.url, headers={"User-Agent": USER_AGENT}, timeout=120)
        resp.raise_for_status()
        local_path.write_bytes(resp.content)
    return local_path, hashlib.sha256(local_path.read_bytes()).hexdigest()


def make_download_and_extract_node(cfg: Config, con: psycopg.Connection):
    def download_and_extract(state: IngestState) -> dict:
        doc = state["current_doc"]
        if doc is None:
            return {}

        local_path, source_sha256 = download_filing(cfg, doc)

        forms = state.get("forms") or []
        # ingest_manifest tracks whole documents. A form-restricted run
        # (on-demand fetch) only extracts part of one, so it neither skips on
        # nor writes that status — otherwise fetching just L-1-A-RA would mark
        # the document 'loaded' and a later full run would skip its other forms.
        track_manifest = not forms

        if track_manifest:
            already_loaded = con.execute(
                "SELECT status FROM ingest_manifest WHERE source_sha256 = %s", [source_sha256]
            ).fetchone()
            if already_loaded and already_loaded[0] == "loaded":
                logger.info("Skipping already-loaded document %s (%s)", doc.url, source_sha256[:12])
                return {"downloaded": []}

            con.execute(
                """
                INSERT INTO ingest_manifest (source_sha256, insurer, fy, url, status, updated_at)
                VALUES (%s, %s, %s, %s, 'downloaded', now())
                ON CONFLICT (source_sha256) DO UPDATE SET status = 'downloaded', updated_at = now()
                """,
                [source_sha256, doc.insurer, doc.fy, doc.url],
            )

        cells = extract(
            str(local_path),
            doc.mime,
            insurer=doc.insurer,
            fy=doc.fy,
            form_hint=doc.form_hint,
            source_sha256=source_sha256,
            forms=forms,
        )
        if track_manifest:
            con.execute(
                "UPDATE ingest_manifest SET status = 'extracted', updated_at = now() WHERE source_sha256 = %s",
                [source_sha256],
            )

        return {
            "downloaded": [
                {
                    "disclosure_file": dataclasses.asdict(doc),
                    "local_path": str(local_path),
                    "source_sha256": source_sha256,
                }
            ],
            "raw_cells": [dataclasses.asdict(c) for c in cells],
        }

    return download_and_extract
