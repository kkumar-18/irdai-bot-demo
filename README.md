# irdai-bot

A single chat agent, dual-purpose over two independent HDFC Life / Axis Max Life
datasets, both in the same PostgreSQL database:

1. **Public disclosure comparisons** — IRDAI-mandated regulatory filings (L-1, L-4,
   L-22, L-32, L-40, L-41: revenue/solvency/claims/grievance forms), scraped from
   both insurers' investor-relations pages, normalized into a common schema.
2. **Product / quote comparisons** — guaranteed-return savings-plan product data
   (eligibility rules, premium rate tables, benefit illustrations, IRR), seeded
   from `insurance-compare-agent/` into its own warehouse (`db/quotes_schema.sql`
   / `db/quotes_seed.sql`, `nodes/quotes/tools.py`).

The agent (`graphs/analysis_graph.py`) picks which domain's tools a question
needs — a system-prompt-driven choice via LangGraph's normal tool-calling loop,
not a separate classifier — and narrates strictly over deterministic tool
results in both domains, with the same structural grounding check
(`nodes/analysis/grounding.py`) validating every number it states either way.
Reachable from a CLI or a React chat UI, either way backed by the same agent.

See `/Users/krishnakumar/.claude/plans/breezy-zooming-fox.md` for the original
disclosure-comparison design. Disclosure scope: FY2019-20 through FY2025-26,
annual filings only. Product/quote scope: the curated seed dump (Axis Max Life
and HDFC Life guaranteed-savings plans, captured 2026-09-16) — see
`insurance-compare-agent/files/insurance_agent/README.md` for how it was built
and its known gaps (e.g. HDFC Life has no rate tables loaded at all).

## Setup

```bash
uv sync
cp .env.example .env   # fill in OPENAI_API_KEY and DATABASE_URL
createdb irdai_bot     # tables/views are created on the first `ingest run`
```

## Usage

```bash
# Ingest: discover -> download -> extract -> normalize -> load
uv run irdai-bot ingest run --run-id my-run [--insurer hdfc_life] [--insurer axis_max_life]

# If it pauses for human review, edit data/review/pending_<run-id>.json
# (set canonical_id / segment_id / period, confidence >= 0.85), then:
uv run irdai-bot ingest resume --run-id my-run

# Ask a disclosure comparison question (CLI)
uv run irdai-bot ask "Compare HDFC Life and Axis Max Life total premium growth, FY2023-24 vs FY2024-25"

# Ask a product/quote question — same command, same agent, different tools
uv run irdai-bot ask "I'm 38, can pay 2.4 lakh a year for 10 years. Which guaranteed plans can I buy, and how do their IRRs compare?"
```

## Chat UI (`frontend/`)

React + Vite + TypeScript, chats with the same LangGraph analysis agent via
a small FastAPI layer (`src/irdai_bot/api.py`) and renders a chart
(Recharts) alongside the narration for trend/comparison questions — no
chart at all for questions that don't have one (a chart is never
fabricated to fill space). Colors follow entity identity: HDFC Life is
always the same blue and Axis Max Life the same orange across every chart
in a session, per the dataviz design rules in
`nodes/analysis/chart.py`/`ChartRenderer.tsx`.

```bash
# Terminal 1 — backend
uv run uvicorn irdai_bot.api:app --reload --port 8000

# Terminal 2 — frontend
cd frontend
npm install   # first time only
npm run dev   # http://localhost:5173, proxies /api -> :8000 (vite.config.ts)
```

**Missing data is fetched in the background.** If a question needs figures
the database doesn't have yet, the agent answers right away with what it has
and calls `fetch_disclosures`, which starts a background job
(`src/irdai_bot/fetch.py`, tracked in the `fetch_jobs` table) running the
fetch workflow (`graphs/fetch_graph.py`: plan → scrape & load → verify →
record). Scraping uses requests + BeautifulSoup against the insurer's
public-disclosure page
([HDFC Life](https://www.hdfclife.com/about-us/public-disclosure),
[Axis Max Life](https://www.axismaxlife.com/newsroom/public-disclosures)),
then runs the normal download → extract → label-mapping → load pipeline for
just the missing fiscal years and forms, saving into Postgres.

In the chat UI that answer shows **"Getting data for you... Please wait"** with
a **Reload** button. Reload polls `GET /api/fetch-jobs/{job_id}` (every 3s,
up to ~45s): if the job found the data, the question is asked again and the
answer and chart update in place; if the job is still running, the button
comes back; if it finished without finding anything, the panel says the data
still isn't available and offers Reload again. A failed job is retried by
re-asking.

Notes: only labels the mapper accepts with confidence are loaded (no
human-review pause mid-job). Each (insurer, FY, form) is tried at most once
per 24 hours (`fetch_log`), so genuinely unavailable data doesn't re-scrape on
every question. Jobs run on threads in the API process — a restart abandons a
running job (reported as failed after 30 minutes; asking again starts a new
one), and PDF extraction is CPU-bound, so chat replies may slow down while a
job runs. `irdai-bot ask` waits for its jobs and then answers again.

**Any form, any year.** Nothing is out of scope just because it isn't loaded
yet. Both insurers' websites publish annual disclosures going back years
(HDFC Life from FY2005-06, Axis Max Life from FY2010-11), and the fetch
workflow reads whatever a question needs:

- *Curated forms* (L-1-A-RA, L-4, L-22, L-32, L-40, L-41 for FY2019-20+) load
  into `facts` via the layout-specific extractors and the derived ratio views.
- *Everything else* (other forms, earlier years, or curated pages whose layout
  didn't parse) is transcribed cell-by-cell into `disclosure_values`
  (`extract/disclosures.py`): pages are routed by the form heading printed on
  each page (pre-2021 bundles are split across "financial" and "other" files),
  rendered with column alignment preserved, transcribed by an LLM for the
  filing's own period, and every value is dropped unless its digits are
  actually printed on that page. Each row keeps its source URL and page.
- *Schema version* is keyed on the filing's period end — `v2010` (IRDAI's
  2010 public-disclosure circular) before Sep 30, 2021, `v2021` from then —
  and `normalize/canon/forms.yaml` is the form-number crosswalk (numbers mean
  the same form in both; verified against every bundle's headings in
  `data/raw`). L-43..L-45 only exist from FY2021-22 and are reported as such
  for earlier years, never backfilled.

Verified end to end on FY2012-13 L-37/L-38/L-30 for both insurers (e.g. HDFC
Life individual new business: ₹2,294 cr of ₹3,294 cr via bank corporate
agents). Limits: only year-end (Q4) filings are read; forms that are mostly
names or narrative (L-31 board members, L-42 valuation basis) yield few or no
numeric values; each transcribed page costs one LLM call (cached per page in
`disclosure_pages`).

Open `http://localhost:5173` and either click one of the example questions
or type your own. Each browser tab keeps its own session id
(`localStorage`), so multi-turn follow-ups within a tab share conversation
context via the backend's `MemorySaver`.

## What's actually verified vs. what isn't yet

This matters more than usual here because the source PDFs have no table
gridlines (see `extract/pdf.py`), so extraction fidelity is a real, ongoing
concern, not a formality:

- **HDFC Life's L-4 (Premium Schedule) total-premium trend, FY2019-20
  through FY2025-26, is hand-verified** against the raw PDF text and is
  internally consistent (smooth ₹29,186cr → ₹79,387cr growth, matches public
  knowledge of HDFC Life's scale). Trust this one.
- **L-4 (Premium Schedule) is extracted from its fixed 4-column layout** —
  each column's own header gives its period and date
  (`extract/pdf.py:_premium_schedule_cells`), prior-year comparative columns
  are skipped, and the L-5 schedule Axis prints on the same page is cut off.
  The generic scrape used to mis-assign Axis's two-line headers (e.g. its
  FY2022-23 annual total loaded as FY2021-22 Q4; FY2025-26 at ₹4,085 cr
  instead of ₹38,877 cr). Now: Axis Max Life FY2021-22..FY2025-26 and HDFC
  Life FY2019-20..FY2025-26, one row per insurer/FY/period/line item. Axis
  FY2025-26 cross-checks against its own Revenue Account gross premium
  (38,87,689 lakh). **Axis FY2019-20 and FY2020-21 are not extracted**: those
  bundles use an older segment-by-column L-4 layout. HDFC FY2018-19 (only
  ever present as a comparative column) is no longer loaded.
- **Two real unit-scale bugs were found and fixed this session** via the
  chat UI surfacing wrong numbers live: (1) a hardcoded scale assumption
  produced values ~100x too large where a filing declared "(₹ '000)" instead
  of "(₹ Lakh)" for the same form; (2) the regex fix for (1) then matched
  the literal digits "000" inside an unrelated year ("...November 15,
  2000"), producing values ~100x too *small*. Both are fixed
  (`normalize/units.py`) and covered by regression tests
  (`tests/test_normalize.py`) — but this class of bug is a reminder to
  spot-check new data against source PDFs before trusting a new insurer/form
  combination.
- **L-1-A-RA (Revenue Account) is extracted for FY2021-22 through
  FY2025-26, both insurers.** Its columns are mapped positionally from IRDAI's
  fixed 18-column template (Linked ×5, Participating ×6, Non-Participating ×6,
  Grand Total — `extract/pdf.py:_REVENUE_ACCOUNT_COLUMNS`) instead of from
  header text, which was the reason this form used to load nothing. Expense
  ratios hand-checked against the PDFs (e.g. HDFC FY2025-26: 7,69,091 /
  77,31,522 = 0.0995; Axis FY2025-26: 5,66,311 / 38,10,412 = 0.1486 — Axis
  prints no net-premium subtotal, so `l1_net_premium` derives it). **FY2019-20
  and FY2020-21 are not extracted**: those filings use IRDAI's older 16-column
  template (skipped with a warning), and Axis's older bundles use a different
  page header.
- **L-41 (Grievances) extraction is degraded** on pages where the source PDF
  uses unusually wide inter-glyph kerning that fragments pdfplumber's word
  tokenization (a merge pass in `extract/pdf.py` fixes most of it, not all).
- Two of Axis's older filings (FY2019-20, FY2020-21 "Financial Disclosures"
  half) produce zero extracted cells — `FORM_HEADER_PATTERNS` in
  `extract/pdf.py` was tuned against HDFC's header wording and hasn't been
  extended with Axis's own variant text yet.
- The agent originally had to guess exact `line_item`/`segment` string
  values with zero guidance (the schema card only lists column names) —
  confirmed live, it sometimes guessed a plausible-looking wrong value
  ('Total Premium' instead of the real `total_premium`) and reported "data
  not available" on a single empty query instead of double-checking. Fixed
  by surfacing the real canonical vocabulary (already defined once in
  `normalize/canon/*.yaml` for ingest-time label mapping — reused, not
  duplicated) directly in the system prompt, plus an explicit
  don't-give-up-on-one-empty-result instruction (`graphs/analysis_graph.py`).
- The `run_sql` tool's SQL allowlist (including CTEs — fixed a real bug
  where a CTE's own name was rejected as a disallowed relation), the
  grounding check (numbers in the agent's answer must trace to an actual
  query result, including across multi-turn chat sessions — fixed a real
  bug where it accumulated *every prior turn's* results forever), and the
  human-review gate are all real, tested, and were exercised against live
  data through both the CLI and the chat UI.

**Next step for correctness**, per the plan's own recommendation: build
`tests/golden_set.json` — hand-verified `(insurer, fy, form, line_item) ->
value` tuples — and resolve enough of `data/review/pending_*.json` to get
L-1-A-RA loaded (unlocks the expense-ratio, and several other derived-view,
questions).

## Architecture

- `adapters/` — per-insurer discovery (`requests` + BeautifulSoup). HDFC's
  page is plain server-rendered HTML; Axis's is Next.js, so the adapter
  parses the inline `__NEXT_DATA__` JSON payload instead of the (partially
  client-filtered) rendered DOM.
- `extract/` — MIME-routed extraction. PDF path derives table columns by
  clustering numeric x-positions (no gridlines to detect); XLSX path is a
  plain grid walk.
- `normalize/` — canonical dictionaries per form (`normalize/canon/*.yaml`),
  LLM-assisted label mapping cached in Postgres (`label_cache`), unit/scale
  coercion.
- `db/` — PostgreSQL schema (long-format `facts` table) + derived metric views
  for disclosures; `quotes_schema.sql` + `quotes_seed.sql` for the product/quote
  warehouse (ported from `insurance-compare-agent/`, applied and seeded once,
  idempotently, in `connection.py:connect()`). The ingest graph's `interrupt()`
  checkpoints (`PostgresSaver`) live in the same database; every agent tool's
  `run_sql`-style query runs in a read-only session.
- `graphs/` — two LangGraph graphs: `ingest_graph` (discover → fan-out
  download/extract → normalize → human-review gate via `interrupt()` →
  validate → load; the review gate is skipped for on-demand fetches) and
  `analysis_graph` — one ReAct agent, dual-purpose over disclosure tools
  (`run_sql` + `fetch_disclosures`, `nodes/analysis/tools.py`) and quote tools
  (`list_products`/`check_eligibility`/`compare_illustrations`/`rate_lookup`/etc.,
  `nodes/quotes/tools.py`), plus a structural grounding check. There's no
  separate intent classifier: the system prompt scopes each tool to its domain
  and LangGraph's normal tool-calling loop picks between them per question
  (even switching domains mid-session). `analysis_graph` derives each turn's
  tool results/queries fresh from message history
  (`nodes/analysis/grounding.py:collect_turn_tool_values`/
  `collect_turn_queries`) rather than accumulated state, so a multi-turn
  chat session's grounding and charts are always scoped to the current turn —
  `collect_turn_tool_values` walks any tool's result shape (flat rows or a
  nested dict) for grounding, while chart derivation stays row-shaped only.
- `nodes/analysis/chart.py` — derives a `ChartSpec` (or `None`) from a
  turn's tool results, pure/deterministic, no LLM call. Works for any
  list-of-dicts-shaped tool result, not just `run_sql` — `compare_illustrations`
  charts the same way.
- `cli.py` — `ingest run`/`ingest resume`/`ask`.
- `api.py` — FastAPI layer for the chat UI (`POST /api/chat`), builds the
  analysis graph once at startup so `MemorySaver` gives real multi-turn
  continuity across chat messages.
- `frontend/` — the chat UI (see "Chat UI" above).
