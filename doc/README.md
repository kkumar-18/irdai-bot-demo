# LLM documentation

How this project uses LLMs: every prompt, every LangGraph workflow, and every tool an LLM
can call. Split into three files so each stays a quick reference, not a wall of text.

- **[prompt_doc.md](prompt_doc.md)** — every LLM prompt template in the codebase: what it's
  for, which model runs it, and where it lives.
- **[agentic_doc.md](agentic_doc.md)** — every LangGraph workflow (a graph of nodes an LLM's
  output can route between): what it does end to end, and which prompts/tools it uses.
- **[tool_call_doc.md](tool_call_doc.md)** — every tool an LLM can call mid-conversation
  (as opposed to a prompt called directly by application code): what it does, and the
  mechanism that actually executes the call.

## Models in use

| Model | Config | Default | Used for |
|---|---|---|---|
| Mapping model | `MAPPING_MODEL` env var, [config.py](../src/irdai_bot/config.py) | `gpt-5.6-luna` | Label mapping (line items, columns) and generic disclosure-page transcription — structured, no free-form conversation. |
| Narration model | `NARRATION_MODEL` env var, [config.py](../src/irdai_bot/config.py) | `gpt-5.6-terra` | The chat agent that answers user questions — free-form, tool-calling, `reasoning_effort="none"`. |

Both are set via `.env` (see [.env.example](../.env.example)); nothing hardcodes a model name
outside `config.py`.

## Scope

This covers LLM usage only. It does not cover: PDF layout extraction ([extract/pdf.py](../src/irdai_bot/extract/pdf.py) — no LLM,
positional/regex-based), the SQL allowlist validator, the grounding check ([nodes/analysis/grounding.py](../src/irdai_bot/nodes/analysis/grounding.py) — regex-based,
no LLM), or chart derivation ([nodes/analysis/chart.py](../src/irdai_bot/nodes/analysis/chart.py) — pure heuristic, no LLM). Those are deliberately
LLM-free; see each file's own docstring for why.
