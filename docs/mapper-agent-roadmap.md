# Mapper Agent Roadmap — Backlog

Backlog of extensions to the schema-inference mapper agent pipeline
(`schema_inference/agents/`), captured after the 5-agent pipeline (PR #125)
landed and the CTE-depth performance bug was fixed. Items are grouped into
phases by dependency, not by priority within a phase — each phase header
states what unlocks it.

Numbering convention: `MAP-#`, distinct from the generator work packages
(`WP-#`) used for baseline/simulation data work.

---

## Phase A — Foundation (unlocks everything below)

### MAP-1: Persist mappings into a metamodel

**Goal:** Replace the current one-shot JSON files (`schema_inference/registry/*/proposal_*.json`,
`schema_inference/mappings/*.json`) with a durable, queryable store of every
mapping decision ever made, across runs, sources, and clients.

**Why:** Loss-function tracking, ground-truth tuning, and few-shot curation
(MAP-3, MAP-4) all need to ask "what did we map this column to last time,
what was the verdict, and did a human ever override it?" — which requires
history, not point-in-time files.

**Scope:**
- New schema (`metamodel`, could live in this Postgres warehouse as a set of
  dbt seeds/tables, or a separate lightweight SQLite store for local/CI runs):
  - `mapping_history` — one row per (run_id, source_name, table_name, source_column):
    target_field, confidence, method, sql_expression, verdict (if scored),
    reviewer_action (if human-reviewed), recorded_at.
  - `loss_runs` — one row per scoring run: run_id, source_name, aggregate
    metrics (precision/recall/F1/hard_f1/ext_attr_accuracy), config snapshot
    (which prompt/threshold version was active).
  - `prompt_versions` / `config_versions` — see MAP-4.
- Backfill: import existing `registry/` and `mappings/` JSON into the new store
  once, then retire the flat-file path (or keep flat files as the write-through
  log, with the metamodel as the queryable index — TBD).

**Depends on:** nothing — can start immediately.

**Risk:** scope creep into "build a general data catalog." Keep the schema
narrow — it only needs to answer questions MAP-3/MAP-4 ask.

---

### MAP-2: Generalize the loss function

**Goal:** Turn `scripts/score_mappings.py` from a single-source demo scorer
into the general-purpose loss function the rest of this roadmap depends on.

**Why:** Every later item (contested-mapping resolution, self-tuning,
client-specific accuracy reporting) needs a loss number that is (a) computed
the same way for any source/client, not just PAS-L, and (b) decomposable —
per-column, per-source, and aggregate, with a real penalty curve rather than
a flat binary correct/incorrect.

**Scope:**
- Generalize `_load_catalog` / `score()` to accept any `*_schema_catalog.yml`
  + `*_value_catalog.json` pair, keyed by source name — not hardcoded to
  `pasl_schema_catalog.yml`.
- Add a continuous loss term per column, not just the TP/TN/FP/FN/WRONG_TARGET
  verdict:
  - **Calibration penalty** — Brier-score-style `(confidence − correct)²` so an
    overconfident wrong answer costs more than a low-confidence wrong answer.
  - **Hard-column weight** — multiply loss by a configurable factor (`>1`) for
    `is_hard` columns, since those are the cases that erode client trust fastest.
  - **Transformation-correctness term** — today the catalog only scores
    *target field* correctness; it doesn't check whether the generated SQL
    produces the right runtime value (the `ANNU_PREM_AMT` /100 cents bug and
    the `WRTG_AGT` string-vs-numeric bug from PR #125's review were target-correct-but-SQL-wrong
    failure modes that the current scorer can't see). Needs a
    `sql_correct: bool` ground-truth field per hard column, checked by
    executing the SQL against a known sample row and comparing output.
  - **Missing-required-field penalty** — fold `missing_standard_fields`
    detection into the same weighted sum (it's currently a separate report).
- Emit one **aggregate loss number** per run (not just F1) so later tuning
  loops have a single scalar to minimize, plus the full per-column breakdown
  for diagnosis.

**Depends on:** nothing structurally, but should land before MAP-4 (self-tuning
needs this loss function to exist first) and works much better once MAP-1
exists (loss trends over time, not just one run).

---

## Phase B — Contested mappings

### MAP-3: Resolve near-tied mappings instead of arbitrary dedup

**Goal:** When two or more source columns are equally plausible mappings to
the same target field, don't silently pick one by confidence tiebreak — detect
the tie and resolve it deliberately.

**Why:** `_deduplicate()` in `mapper.py` already demotes the loser when two
columns compete for one target, but on a near-tie (confidences within, say,
0.03 of each other) the "winner" is whichever the rule engine or LLM happened
to score a hair higher — not a reasoned choice. PAS-L's `POL_NO` mapping to
both `policy_id` (primary) and `policy_number` (secondary) shows the catalog
already has a notion of legitimate dual mappings; the dedup logic doesn't use it.

**Scope:**
- Detect near-ties: any two `ColumnMapping`s targeting the same field with
  `abs(conf_a − conf_b) < tie_epsilon` (configurable, e.g. 0.05).
- For each tie, check the canonical field for a `secondary_target` —
  if both source columns legitimately map to *different* canonical fields
  (one primary, one secondary), promote both instead of demoting one.
- If it's a genuine single-target contest (two columns, one slot, no secondary
  escape), escalate to the CriticAgent with a **comparison prompt** — "decide
  between A and B for this target, not a confirm/override on one column in
  isolation" — instead of the current per-column-independent critic review,
  which never sees that two columns are competing.
- Unresolvable ties (critic still can't decide) surface in the proposal as a
  new `contested_mappings` list, separate from `unmapped_columns`, so the
  human reviewer (`reviewer.py`) gets a dedicated review phase for these
  instead of them being silently demoted.

**Depends on:** MAP-1 loosely (contested-mapping outcomes are useful tuning
signal once persisted), but can be built standalone against the current
`mapper.py` / `critic_agent.py`.

---

## Phase C — Self-tuning agent

### MAP-4: Self-tuning agent (loss-driven prompt/rule adjustment)

**Goal:** An agent that, given a ground-truth-labeled sample for a new
source or client, automatically adjusts the rule-engine weights, confidence
thresholds, few-shot examples, and (carefully, with guardrails) the agent
system prompts to drive the loss function (MAP-2) down — producing a
measurable, reproducible accuracy curve per client instead of a fixed,
hand-tuned pipeline.

**Why:** This is the highest-leverage item in the backlog. Every other
mapper improvement is a one-time engineering fix; this is a mechanism that
gets better per client without an engineer manually re-tuning the rule
engine or rewriting prompts for every new source schema PremiumIQ ingests.

**Depends on:** MAP-1 (metamodel — needs to persist what was tried and what
the loss was) and MAP-2 (loss function — needs the thing to minimize).

**Scope:** see the dedicated design doc —
[`self-tuning-mapper-agent-plan.md`](self-tuning-mapper-agent-plan.md).
This backlog entry exists to track build status; the design doc holds the
actual plan, layered-tuning-stack design, and rollout phases.

**Build status:**
- Layer 0 (numeric rule-weight tuning) — done. `tools/tune_rule_weights.py`.
- Layer 1 (few-shot example bank) — done. `tools/curate_few_shot_bank.py`,
  `schema_inference/metamodel/few_shot.py`.
- Layer 2 (textual prompt tuning) — done. `tools/tune_prompts.py`. Mechanics
  verified with an injected fake LLM client (no `ANTHROPIC_API_KEY` in this
  environment) — live diagnose/propose/validate quality is unverified until
  run against a real key.
- Layer 3 (learned critic trigger) — **deferred, not started.** The plan
  gates this on 3+ sources of real cross-source volume; we have one (PAS-L,
  46 columns). Revisit once a second/third client source has accumulated
  `mapping_history` — fitting a classifier on PAS-L alone would overfit
  trivially. Decided 2026-06-25.

---

## Phase D — Row-level transforms (independent track)

### MAP-5: Row identity + dedup inference agent

**Goal:** We've solved column-level mapping (source column → target field).
The next-order problem is row-level: given a mapped table, what's the row
identity (natural key) and what's the recency/version signal needed to
project and deduplicate source rows into one canonical row per entity at
the target?

**Why:** Every source in this project already needs this — PAS-L dedups via
`ROW_NUMBER() OVER (PARTITION BY pol_no ORDER BY pol_no_seq DESC)`, PAS-M
dedups via the `cdc_latest_record` macro on `_cdc_timestamp` — but those
dedup strategies were hand-written by an engineer who already knew the
source's shape. A new source arrives without that hand-holding.

**Scope:**
- A `RowShapeAgent` that inspects a `TableProfile` (after column mapping) and
  proposes:
  - candidate natural key column(s) (high distinct-count, low null-rate,
    `is_id_column` or composite of low-cardinality codes + an id column),
  - a recency/version column (monotonic-looking integer/sequence column, or
    a date/timestamp column, or a CDC operation flag),
  - the dedup SQL pattern (`ROW_NUMBER() OVER (PARTITION BY <key> ORDER BY
    <recency> DESC) = 1`, or `WHERE _cdc_operation != 'D'` plus latest-wins,
    or no dedup needed if the table is already one-row-per-entity).
- Confidence score per proposal, same shape as `ColumnMapping.confidence`,
  so it slots into the same review/scoring infrastructure (MAP-2's loss
  function should be extendable to score row-shape proposals the same way
  it scores column proposals).
- Ground truth: extend `pasl_schema_catalog.yml`-style catalogs with a
  `row_shape` section (`natural_key`, `recency_column`, `dedup_pattern`) so
  this agent's proposals can be scored exactly like column mappings.

**Depends on:** MAP-2's loss-function infrastructure should be generalized
enough to score a second class of proposal (row-shape, not just column
mapping) without a parallel scoring codebase.

---

## Phase E — Table-level mapping (exploratory only — not committed)

### MAP-6: Many-to-one / one-to-many table mapping

**Goal:** Handle the case where one canonical entity is split across
multiple source tables (PAS-M's policy + coverage + premium + risk tables,
which today are hand-joined in `pas_modern/policy.py` and the staging
layer) or where multiple source systems' tables must merge into one target
— without an engineer hand-writing the join logic per source.

**Why flagged exploratory, not committed:** this is a genuinely harder
problem than column mapping or row-shape inference. It requires inferring
join keys *across tables*, reasoning about cardinality (1:1 vs 1:many vs
many:many), and detecting when a "many-to-one" collapse needs an aggregation
strategy (sum? latest? first-non-null?) rather than a join. The blast radius
of getting this wrong (silently fanning out rows via a bad join) is much
higher than a column mapping mistake. Worth a design spike before committing
real build time — not worth scoping in detail until MAP-1 through MAP-5 are
in production and we know how much of this clients actually need versus
hand-building per-source joins as we do today.

**Depends on:** MAP-5 (table-level mapping is a strict superset of row-shape
inference — you need row identity within each table before you can reason
about how tables relate).

---

## Suggested build order

1. **MAP-1 + MAP-2** in parallel (foundation; no dependency between them).
2. **MAP-4** (self-tuning) — biggest audience impact per the working
   hypothesis; start as soon as MAP-1/MAP-2 land. See the dedicated plan doc.
3. **MAP-3** (contested mappings) — can run in parallel with MAP-4; smaller
   scope, immediate accuracy win, and its outcomes become tuning signal for
   MAP-4 once MAP-1 is in place.
4. **MAP-5** (row-level) — independent track, can start any time after MAP-2
   exists in a generalized form. Good candidate for a second engineer to pick
   up in parallel with MAP-4.
5. **MAP-6** (table-level) — design spike only, after MAP-5 ships.
