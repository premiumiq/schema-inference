# MappingAgent Batching — Plan (Cost Mitigation #4)

Companion to the cost discussion in this session: items 1-3 (prompt caching,
config-driven `max_tool_calls_per_column`, Layer 2 split-scoped pipeline
runs) are implemented. This is the fourth, larger lever — batching
MappingAgent's per-column tool-use loop the way CriticAgent and SQLAgent
already batch — written up for a decision, not yet implemented.

## Current design

`mapping_agent.py` opens **one isolated conversation per low-confidence
column**: up to `max_tool_calls_per_column` (default 5) sequential
tool-use turns, then a forced final JSON answer. Columns run concurrently
via asyncio (`concurrent_columns`, default 10), but each conversation is
independent — no shared context between columns.

`critic_agent.py` and `sql_agent.py` do the opposite: **one batch call**
covering every column they touch, no tool use, one JSON response with an
entry per column.

## Why MappingAgent isn't batched today

Per the module's own docstring, this design directly replaced the original
single-shot batch call (`mapper.py`'s `_run_llm_batch`) specifically
*because* that call **had no tools** — it guessed from the column name and
a handful of profile fields alone, with no way to check
`check_value_catalog` for hard columns (the exact failure mode that produced
the `WRTG_AGT`→`agent_id` and cents-not-divided-by-100 bugs PR #125's review
caught). The per-column isolation was never the goal — investigative tool
use was. Batching is compatible with keeping tool use; isolation is a side
effect of how it was built, not a requirement.

## What batching would look like

Group columns into batches (same chunking pattern `_run_llm_batch` and
`sql_agent.py` already use — `LLM_BATCH_SIZE`/`batch_size`, ~15-20 columns).
One shared conversation per batch: the model can still call
`check_value_catalog`, `get_column_profile`, etc., for any column in the
batch across multiple turns (every tool already takes `column_name` as an
argument, so a tool call's target column is recoverable from its own input
— `AgentTrace` attribution per column survives a shared conversation
without new plumbing there). The batch ends with one JSON array covering
every column in it, instead of N separate single-column JSON answers.

## Benefits

- **Fewer total calls.** N columns in isolated conversations → N ×
  (1-6 calls) becomes ceil(N / batch_size) × (1-6 calls). For a brand-new,
  untuned source where most columns route to the agent (the worst case for
  today's design, and the most cost-sensitive moment — new client
  onboarding, before any Layer 0 tuning has run), this is the largest
  win: 46 isolated conversations could become 3 batched ones.
- **Possible quality upside from shared context.** The model sees several
  columns at once and can reason about structurally similar ones together
  (e.g. several premium-related columns all needing the cents check) —
  this is genuinely double-edged, see Costs below.

## Costs / risks

- **This benefit is now smaller than it looked before items 1-3 landed.**
  Prompt caching (item 1) already eliminates most of the repeated
  system-prompt + tool-schema cost across columns — that was the single
  biggest piece of "redundant cost from N separate conversations." What's
  left uncached per column is the column-specific user message (~150
  tokens) and the model's own reasoning/tool-call output (never cacheable
  either way, batched or not). Batching's *remaining* incremental savings
  on top of caching is real but meaningfully smaller than a naive
  "N conversations → 1" estimate suggests. Measure against a caching-enabled
  baseline before committing to this, not against the pre-caching numbers
  from this session.
- **Cross-column interference is a real accuracy risk, not just a
  theoretical one.** A shared conversation means one column's reasoning can
  anchor or bias a structurally similar but semantically different column
  in the same batch (e.g. two `*_CD` suffix columns where one is a real
  code and one is a red herring per `WRTG_AGT`'s exact failure pattern).
  Isolated per-column conversations can't leak this way by construction;
  batched ones can.
- **Loses per-column reproducibility/testability.** Today, re-running one
  column in isolation always gets the same investigative path regardless of
  what else was being mapped in that run. Batched, a column's outcome
  depends on which other columns share its batch and in what order — this
  directly complicates Layer 2's regression check (`_run_and_score`'s
  prev-correct/now-correct comparison assumes a column's correctness is a
  function of the prompt alone, not of batch composition).
- **Loses real per-column concurrency.** Today, up to 10 columns run
  truly in parallel (asyncio). A shared conversation is inherently
  sequential within itself — trading wall-clock parallelism for fewer
  total calls. This is less painful than it would have been before the
  throttle/pacer work (concurrency was already not buying real
  request-rate speed under a tight RPM cap), but it's a real wall-clock
  tradeoff for orgs with a higher rate limit who *were* benefiting from
  concurrency.
- **Engineering cost is non-trivial.** Needs: batch-aware prompt
  construction (describe N columns instead of 1), a JSON schema for an
  array of answers instead of one object, tool-call-to-column attribution
  wiring (recoverable, per above, but not free), and batch-size tuning as a
  new knob with its own cost/risk tradeoff (bigger batch = fewer calls but
  more cross-column interference risk and more per-call tokens; smaller
  batch = less risk, less savings).

## Recommendation

**Not now.** Items 1-3 already address the acute cost problem (the $0.51
that prompted this) without any accuracy or testability tradeoff. Batching
is the right next lever specifically for **new-source onboarding** (many
columns simultaneously low-confidence, before Layer 0 tuning narrows that
down) — revisit when there's a second real source to onboard and a
caching-enabled cost baseline to compare against. Don't build this against
PAS-L alone; the fixture is already tuned to near-100% rule-engine
accuracy (Layer 0), so very few columns ever reach MappingAgent on it today
— there's no real signal here to validate a batched redesign against.

If/when revisited: start with a conservative batch size (5-8, well under
the existing 15-20 used by the no-tool-use batch callers, since tool-use
batches carry more risk per column than the simple JSON-array callers do),
and add a per-column isolation fallback — if a batch's answer for one
column looks malformed or low-confidence, re-run that single column in
isolation before accepting the batch result. That keeps the worst case
no worse than today's design while capturing the common-case savings.
