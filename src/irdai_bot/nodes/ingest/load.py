from __future__ import annotations

import logging

import psycopg

from irdai_bot.graphs.state import IngestState

logger = logging.getLogger(__name__)

_COLUMNS = [
    "insurer", "fy", "period", "form", "segment", "line_item", "value", "unit",
    "declared_scale", "is_restated", "source_sha256", "source_locator",
    "raw_row_label", "raw_col_label", "extracted_at", "confidence",
]

_UPSERT_SQL = f"""
    INSERT INTO facts ({', '.join(_COLUMNS)})
    VALUES ({', '.join('%s' for _ in _COLUMNS)})
    ON CONFLICT (insurer, fy, period, form, segment, line_item, source_sha256) DO UPDATE SET
        value = excluded.value,
        unit = excluded.unit,
        declared_scale = excluded.declared_scale,
        source_locator = excluded.source_locator,
        raw_row_label = excluded.raw_row_label,
        raw_col_label = excluded.raw_col_label,
        extracted_at = excluded.extracted_at,
        confidence = excluded.confidence
"""


def make_load_node(con: psycopg.Connection):
    def load_facts(state: IngestState) -> dict:
        hard_errors = {
            e["fact"]["source_sha256"] for e in state["validation_errors"] if e["severity"] == "error"
        }
        rows = [
            tuple(f[c] for c in _COLUMNS)
            for f in state["normalized_facts"]
            if f["source_sha256"] not in hard_errors
        ]
        if rows:
            shas = {r[_COLUMNS.index("source_sha256")] for r in rows}
            # One transaction: a document is never marked 'loaded' unless all
            # of its facts actually landed (a partial load would otherwise be
            # skipped forever by download_extract's already-loaded check).
            with con.transaction(), con.cursor() as cur:
                cur.executemany(_UPSERT_SQL, rows)
                if not state.get("forms"):  # see download_extract's track_manifest
                    cur.executemany(
                        "UPDATE ingest_manifest SET status = 'loaded', updated_at = now() WHERE source_sha256 = %s",
                        [(sha,) for sha in shas],
                    )
        logger.info("Loaded %d facts into the warehouse", len(rows))
        return {"facts_loaded": len(rows)}

    return load_facts
