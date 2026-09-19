# Prompts

Every LLM prompt template in the codebase. Each entry: what it does, the model that runs it,
and where it's defined and called.

---

### 1. Line-item mapping prompt

Maps a batch of raw, OCR-adjacent row labels from one form/insurer to the form's fixed set
of canonical `line_item` ids (e.g. "Premium L-4" → `premium_income_gross`), with a
confidence score and reasoning per label. Runs only for labels not already in `label_cache`.
Uses LangChain structured output (`LineItemMappingBatch`, a Pydantic model) — no free text
parsing.

- **Model:** mapping model (`gpt-5.6-luna` by default), `temperature=0`.
- **Defined:** [normalize/mapping.py:135](../src/irdai_bot/normalize/mapping.py#L135), inside `map_line_items()`.
- **Called from:** `nodes/ingest/normalize.py`'s `normalize` node, inside both the
  [ingest_graph](agentic_doc.md#1-ingest-graph) and the curated-forms step of the
  [fetch_graph](agentic_doc.md#2-fetch-graph).

### 2. Column mapping prompt

Maps a batch of raw, multi-line column headers to a segment id (product line), a period
(`'annual'` / `'quarterly'`), and whether the column is a prior-year comparative — from
header text that's often scrambled by the source PDF having no ruling lines. Also
structured output (`ColumnMappingBatch`), cached the same way as line items.

- **Model:** mapping model (`gpt-5.6-luna` by default), `temperature=0`.
- **Defined:** [normalize/mapping.py:203](../src/irdai_bot/normalize/mapping.py#L203), inside `map_columns()`.
- **Called from:** same call sites as the line-item mapping prompt above.

### 3. Disclosure-page transcription prompt

Transcribes every numeric cell on one page of any IRDAI form (L-1 through L-45, any fiscal
year) into structured `(row_label, column_label, measure, period, unit, scale, value)`
cells, for the filing's own period only (comparative columns skipped). This is the generic
path for everything the two fixed-layout PDF parsers don't cover. Structured output
(`PageCells`); every returned value is checked against the page's own text afterward
(`printed_on_page`, not part of the prompt) before being trusted.

- **Model:** mapping model (`gpt-5.6-luna` by default), `temperature=0`.
- **Defined:** [extract/disclosures.py:136](../src/irdai_bot/extract/disclosures.py#L136) (`_PROMPT`), formatted and sent in `extract_page()`.
- **Called from:** `transcribe_disclosures` step of the [fetch_graph](agentic_doc.md#2-fetch-graph), once per page, up to 4 pages in parallel per filing.

### 4. Chat agent system prompt

The system prompt for the user-facing comparison agent: the full `facts`/view/
`disclosure_values` schema, the valid `line_item` vocabulary, every IRDAI form number and
title, and the rules governing tool use, grounding, chart-friendly SQL shape, and when to
call `fetch_disclosures` vs. say data is unavailable. This is a instructions-only prompt (no
structured output) — the model replies with either a tool call or a final narration.

- **Model:** narration model (`gpt-5.6-terra` by default), `temperature=0`, `reasoning_effort="none"` (required for a tool-calling model on this endpoint).
- **Defined:** [graphs/analysis_graph.py:38](../src/irdai_bot/graphs/analysis_graph.py#L38) (`SYSTEM_PROMPT`), assembled from `SCHEMA_CARD`, `LINE_ITEM_VOCAB`, and `FORMS_CARD` (all in [nodes/analysis/tools.py](../src/irdai_bot/nodes/analysis/tools.py)).
- **Called from:** the `agent` node of the [analysis_graph](agentic_doc.md#3-analysis-graph), prepended to the conversation on every turn.

---

## Not a prompt

The **grounding-retry nudge** ([graphs/analysis_graph.py](../src/irdai_bot/graphs/analysis_graph.py), `_GROUNDING_RETRY_PREFIX`) and the **deterministic tool-result
hints** ([nodes/analysis/tools.py](../src/irdai_bot/nodes/analysis/tools.py), `_DISCLOSURE_PROBE_HINT` / `_NON_CURATED_FORM_HINT`) are not templates sent as a
prompt — they're fixed strings injected into the conversation as a `SystemMessage` or a tool
result, for the *next* LLM call to react to. Listed in [agentic_doc.md](agentic_doc.md) and [tool_call_doc.md](tool_call_doc.md) respectively, not here.
