# Evals

`tests/` checks that individual functions behave correctly on fixed inputs (a parser, a
regex, a chart heuristic). Evals check something `tests/` can't: whether the *whole system*,
run against real filings and real questions, produces numbers a person could act on. A unit
test can pass while the agent still tells a user "not available" for data that's sitting in
the database (this happened — see [Grounding and self-correction](#4-grounding-and-self-correction)).

This system has three places wrong output reaches a user without looking wrong:
1. **Extraction** silently reads the wrong number off a page (a column shifted by one, a
   scale misapplied) — the pipeline has no way to know it did this.
2. **The agent** states a number, marks a chart, or says "unavailable" — each is a claim a
   user will trust without checking the SQL behind it.
3. **The chart** silently drops a series or plots the wrong column — visually confident,
   uninspected by anyone.

Every eval below exists because one of these already happened in this codebase (see the
inline references to `git log`-worthy incidents from this project's own development) or
because the architecture makes it structurally possible. None of this is implemented yet;
this file is the list to build against.

Suggested layout once implementation starts:

```
evals/
  evals.md              this file
  golden_set.json        hand-verified (insurer, fy, form, line_item) -> value tuples
  question_suite.yaml     the fixed set of questions for §5/§6/§9
  fixtures/               PDFs used only by evals (large/real filings, not tests/fixtures/*)
  run_extraction_eval.py
  run_agent_eval.py
  run_chart_eval.py
  results/                dated JSON/CSV output, one file per run, for trend tracking
```

---

## 1. Extraction accuracy (PDF → `facts` / `disclosure_values`)

The single highest-leverage eval category: every other layer trusts extraction blindly, and
`extract/pdf.py` / `extract/disclosures.py` have no self-check. A confidently wrong number
here becomes a confidently wrong sentence in the agent's answer, three layers later, with
no error anywhere in between.

- **Golden-set value match.** Maintain `golden_set.json`: hand-verified
  `(insurer, fy, period, form, line_item, segment) -> value` tuples, read off the PDF by a
  person, not derived from the pipeline. Run extraction fresh and diff. Report exact
  mismatches separately from off-by-scale mismatches (100x/1000x — the recurring failure
  mode here, see `normalize/units.py`'s docstring for two real instances) since the latter
  indicates a systematic bug, not a one-off. Target: 100% on `L-1-A-RA` and `L-4` (the two
  forms with a real fixed-layout parser); track-but-don't-block on `disclosure_values`
  forms, since those are LLM-transcribed and will never hit 100%.
- **Column-assignment eval, not just value eval.** A value can be numerically plausible
  (passes range checks) while sitting under the wrong column — e.g. a quarter figure loaded
  as a year figure, or one insurer's non-par segment loaded as the other's par segment. The
  golden set must check `(row_label, column_label) -> value`, not just `value` in isolation,
  or this class of bug (confirmed real: Axis's FY2022-23 L-4 annual total once loaded as
  FY2021-22 Q4) passes silently.
- **`disclosure_pages.values_dropped_unverified` rate.** Every value the LLM transcriber
  proposes that doesn't literally appear on the page (`printed_on_page`) is dropped.
  A page with a high drop rate — proposed values it can't substantiate — is a proxy for that
  page being hard to read; alert on drop rate exceeding some threshold (e.g. 30%) per page
  rather than only on raw extraction failure, since a failure raises `status='failed'` but a
  high-drop success stays silently `'extracted'`.
- **Cross-form consistency.** Where two forms should agree — L-4's `total_premium` vs.
  L-1-A-RA's `premiums_earned_net` (net of reinsurance) vs. the sum of L-4's
  first-year/renewal/single columns — check they reconcile within a small tolerance across
  every loaded (insurer, fy). This is exactly how the "Axis prints no net-premium row"
  discrepancy and the FY2023-24 column-splitting bug were actually found in this project;
  turning that manual process into a standing eval catches the next one before a user does.
- **New-insurer / new-form regression.** Before this pipeline is pointed at a third insurer
  or a form without an existing fixed-layout parser, run the golden-set eval against a
  handful of its filings first. `extract/pdf.py`'s fixed-layout parsers
  (`_revenue_account_cells`, `_premium_schedule_cells`) are keyed to specific column counts
  and header phrasing verified only against HDFC Life and Axis Max Life; nothing stops a
  third insurer's differently-shaped page from being silently misread rather than rejected.
- **Layout-drift canary.** Insurers change PDF templates without notice (this project
  already handles three HDFC Life L-1-A-RA layouts and two Axis Max Life ones across the
  years in scope). Re-run the golden-set eval on a schedule (e.g. monthly, or triggered by a
  new quarter's filing) even with no code change, since the input — not the code — is what
  drifts.

## 2. Label mapping (`normalize/mapping.py`)

- **Line-item / column mapping precision and recall**, measured against a hand-labeled set
  of raw `(insurer, form, raw_label) -> canonical_id` pairs, separately from extraction
  accuracy (a correct value under a wrong canonical id is just as wrong to a user asking
  "what was the commission ratio").
- **Confidence calibration.** Among mappings the LLM scored ≥ `CONFIDENCE_THRESHOLD` (0.85)
  and therefore auto-accepted with no human review, what fraction are actually correct?
  This threshold was chosen without being measured against ground truth; an eval here is
  what would tell you whether 0.85 is too permissive (bad data enters `facts` silently) or
  too strict (unnecessary items pile up in `data/review/pending_*.json`).
- **Cache staleness.** `label_cache` is keyed on `(insurer, form, kind, raw_label)` with no
  expiry — a wrong mapping written once is wrong forever until someone finds and fixes it by
  hand. Eval: periodically re-run a sample of cached mappings through a fresh LLM call and
  flag disagreements for review, rather than trusting the cache indefinitely.
- **Segment/column order regression.** `normalize/canon/segments.yaml`'s column order was
  wrong for a real stretch of this project (Linked/Participating/Non-Participating group
  order, confirmed and fixed this session) and nothing caught it — the values loaded, just
  under the wrong segment labels, and every ratio computed against them was wrong without
  error. Eval: for a subset of hand-verified filings, check `segment_id` assignment itself,
  not just that a `segment_id` was assigned.

## 3. SQL agent tool use (`nodes/analysis/tools.py`, `graphs/analysis_graph.py`)

- **Fixed question suite, run on every prompt or schema change.** Maintain
  `question_suite.yaml`: a stable set of questions spanning single-insurer, comparison,
  trend, ratio, and channel/related-party questions, each with the expected numeric answer
  (from the golden set) and pass/fail on whether the agent's final answer matches. This
  should run in CI on every change to `SYSTEM_PROMPT`, `SCHEMA_CARD`, or `tools.py` —
  wording changes here have already changed agent behavior in ways only caught by manually
  re-asking the same question (this session changed the prompt roughly a dozen times, each
  verified by hand; that verification is exactly what this eval should automate).
- **Retrieval-path reliability under repetition, not just correctness once.** The
  disclosure-vs-facts routing bug this session (the agent querying `facts` for a form it
  never populates, giving up instead of trying `disclosure_values`) passed most runs but
  failed roughly 1-in-3 at the exact same temperature and prompt. Eval: run each question in
  the suite N times (e.g. 5–10) and report the pass **rate**, not a single pass/fail — a
  system this stochastic needs a reliability number, not a verdict.
- **Zero-result recovery.** For questions whose data is deliberately deleted from the
  database first, does the agent correctly distinguish "not yet fetched" (call
  `fetch_disclosures`, wait, retry) from "genuinely doesn't exist" (a form marked
  `versions: [v2021]` queried for a year before that framework existed)? Confirmed failure
  mode this session: the agent said data was "outside the dataset's scope" for a form that
  simply hadn't been fetched yet — that specific wording should be a hard eval failure
  whenever it appears, on top of the general "did it get the number" check.
- **`fetch_disclosures` call discipline.** Does the agent call it: (a) only after a genuine
  zero-row result, never speculatively; (b) at most once per insurer per question, per its
  own instructions; (c) with a correctly normalized form code and fiscal year (`L-1-A-RA`
  and `L-1` must resolve to the same fetch)? A prompt-compliance eval — grep the tool-call
  log for violations — since these are behaviors no downstream check catches on its own.
- **Turn-budget usage.** `MAX_TURNS = 10`. Track how many turns the agent actually uses per
  question in the suite; a creeping average signals prompt bloat or an agent that's begun
  probing-then-failing rather than answering directly, before it starts silently truncating
  answers by hitting the cap.
- **SQL allowlist adversarial suite.** `tests/test_sql_allowlist.py` covers the known
  bypass shapes (CTEs, joins to disallowed tables). Extend adversarially: subqueries in a
  `WHERE` clause referencing a disallowed table, a `UNION` with a disallowed relation,
  comments used to smuggle a second statement past the single-statement check, a disallowed
  relation referenced only inside a window function. This is a security boundary
  (`default_transaction_read_only=on` is the real backstop, but the allowlist is meant to
  fail before that), so it should be evaled adversarially, not just against bugs already
  found.

## 4. Grounding and self-correction (`nodes/analysis/grounding.py`)

- **False-negative rate: real numbers wrongly flagged ungrounded.** `find_ungrounded`'s
  tolerance and exemptions (bare years, small integers) are heuristic. Eval against a set of
  known-good narrations paired with their actual `run_sql` results: how often does a
  correct, grounded figure get flagged and trigger an unnecessary retry (burning a turn) or,
  at `MAX_TURNS`, a spurious "can't confidently ground this" refusal on a perfectly good
  answer?
- **False-positive rate: fabricated numbers that slip through.** The harder direction to
  test but the one the whole mechanism exists for. Construct adversarial narrations with a
  plausible-but-wrong figure close enough to a real one to fall inside `_TOLERANCE`
  (currently 1% relative) — does the check still catch it? This is the one eval in this list
  that should be actively hostile to the system, not just descriptive.
- **Cross-turn leakage.** `collect_turn_tool_results` deliberately scopes to the current
  turn only, so an earlier turn's numbers can never ground a new claim. Eval: a multi-turn
  session where turn 2 restates a number from turn 1 without re-querying — does grounding
  correctly reject it? (This is what makes the `[grounding-retry]` mechanism necessary in
  the first place; verify it still fires.)
- **Retry-then-give-up quality.** When grounding fails and the agent is still under
  `MAX_TURNS`, does its revised answer actually fix the flagged figures, or does it just
  reword the same wrong claim in a way that dodges the regex? Track how often a
  grounding-retry actually changes the numeric content of the answer.

## 5. Chart derivation (`nodes/analysis/chart.py`)

Zero LLM involvement here, which makes this the cheapest category to eval exhaustively —
there is no reason for anything less than very high coverage.

- **No-chart-is-correct vs. no-chart-is-a-miss.** The heuristic was deliberately hardened
  this session to return `None` rather than guess wrong (entity-pivoted columns with more
  than one metric, colliding series keys). Two failure directions, both worth tracking
  separately: charting something that's actually wrong (regression on the safety checks
  just added), and declining to chart something a person would call obviously chartable
  (heuristic too conservative, silently hides value from users). The second is easy to miss
  because "no chart" never looks like an error.
- **Melt-shape adversarial suite.** `_melt_entity_pivoted_columns` matches
  `{insurer}_{metric}` column names. Adversarial cases: a metric name that happens to start
  with an insurer token (`hdfc_life_...` used as a coincidental column name, not an actual
  per-insurer pivot), a query with three or more distinct metrics pivoted per insurer, mixed
  matched/unmatched numeric columns in the same result. Confirm the "all-or-nothing" match
  rule (partial pivots are left alone) doesn't itself produce a silently wrong chart.
- **Category vs. time axis selection.** A single-fiscal-year breakdown (channel mix,
  segment mix) should pick the category as `x_key`; a multi-year trend should pick `fy`.
  Eval the boundary directly: exactly 2 distinct fiscal years with a category column also
  present — which does it pick, and is that always right?
- **Real end-to-end capture, not just synthetic rows.** `tests/test_chart.py` uses
  hand-constructed row shapes. Add an eval that runs the actual `question_suite.yaml`
  through the live agent and captures whatever chart spec comes out — this is what would
  have caught the "one column silently dropped" bug, since that bug's synthetic-row
  equivalent was never written until after the live failure was observed.

## 6. End-to-end fetch workflow (`graphs/fetch_graph.py`, `fetch.py`)

- **Idempotency.** Fetching the same (insurer, fy, form) twice in immediate succession
  (before the 24h `fetch_log` cooldown) must not create duplicate rows. `ON CONFLICT DO
  NOTHING` / `DO UPDATE` clauses cover this at the SQL level; eval it as behavior, by
  actually invoking the graph twice and diffing row counts, not just by reading the SQL.
- **Concurrent-job dedup.** Two questions asking for the same missing data in close
  succession should join one running job (`pg_advisory_xact_lock` in `request_fetch`), not
  start two. Eval by firing two requests concurrently and asserting on `job_id` equality —
  this is a race condition, so a single-threaded test proves nothing about it.
- **Heartbeat/staleness under real interruption.** Kill the API process mid-fetch (not just
  simulate it) and confirm: the job is reported `failed` within `HEARTBEAT_TIMEOUT`, no
  partial `disclosure_values` rows are left inconsistent with `disclosure_pages`, and the
  next request for the same data starts a fresh job rather than waiting on the dead one.
  This exact failure (stale `running` jobs blocking every future Reload) was live in
  production this session before the heartbeat existed — this eval is what should have
  caught it pre-release.
- **Wall-clock budget.** Track fetch latency per filing and per page over time (LLM calls
  dominate — `_PARALLEL_FILINGS = 3`, up to 4 page-calls in parallel per filing). A
  regression here degrades UX (the frontend's `AUTO_POLL_MAX_TICKS` window) without
  touching correctness, so it needs its own eval rather than being noticed only when
  someone complains a Reload took too long.
- **`not_applicable` correctness.** Forms marked `versions: [v2021]` (L-43, L-44, L-45)
  requested for an earlier year must be reported as never-existed, not fetched-and-failed
  or fetched-and-empty — these read identically to a user unless the eval checks the
  specific field, not just "did data appear."

## 7. Golden-set regression suite (cross-cutting)

The README already names this as the acknowledged next step
(`tests/golden_set.json`, hand-verified `(insurer, fy, form, line_item) -> value`); it
belongs here as the backbone the rest of this file's evals draw from, not as a separate
one-off task:

- Extend it to cover every curated form/segment combination in scope, and grow it whenever a
  hand-verification happens in the course of debugging something else (this session
  hand-verified dozens of values against raw PDF text — each of those belongs in the golden
  set, not just in a chat transcript).
- Run §1's extraction-accuracy eval against it on every change to `extract/pdf.py`,
  `extract/disclosures.py`, or any `normalize/canon/*.yaml` file.
- Track pass rate over time per form, not just an aggregate — `L-1-A-RA` and `L-4` should
  stay at or near 100%; `disclosure_values`-only forms are expected to be lower and the
  point is watching that number, not demanding perfection from an LLM transcriber.

## 8. Adversarial / injection robustness

- **Scraped-content prompt injection.** Every raw PDF cell, row label, and column header
  extracted from a filing flows into an LLM prompt (`normalize/mapping.py`'s label-mapping
  prompt, `extract/disclosures.py`'s transcription prompt) as untrusted external content —
  it comes from a scraped, publicly-editable-by-nobody-but-still-external website. Eval:
  inject adversarial text into a fixture PDF's row/column labels (e.g. text resembling an
  instruction to the model) and confirm it's treated as data, not instruction — that
  `printed_on_page` and the confidence threshold still gate whatever comes back.
- **Malformed/adversarial PDF handling.** A truncated PDF, a PDF with no text layer (scanned
  image), an XLSX with unexpected structure — `extract/router.py`'s `UnsupportedFormat` and
  `extract_pdf`'s per-page try/except cover known cases; eval against actually malformed
  fixtures, not just well-formed ones with unusual content.
- **Adapter resilience to site changes.** `adapters/hdfc_life.py` and
  `adapters/axis_max_life.py` raise `DiscoveryError` loudly on a shape they don't recognize
  by design — eval that this still fires correctly against a deliberately mutated fixture of
  each site's HTML/JSON (a renamed field, a client-rendered page where the current adapter
  expects server-rendered), since a silent `[]` return here would look identical to "no
  filings this year."

## 9. Operational

- **Cost per question and per fetch.** Every `run_sql` retry, every `fetch_disclosures`
  call, every transcribed page is a paid LLM call. Track $/question across the suite over
  time; a prompt or model change that improves accuracy but multiplies token or call volume
  needs to be a visible tradeoff, not an invisible one.
- **Latency budget by path.** Cache-hit chat question (no fetch needed) vs. fetch-triggering
  question vs. background-fetch completion time — each has a different acceptable range and
  a different part of the system to blame when it regresses; track them separately rather
  than one blended "response time."
