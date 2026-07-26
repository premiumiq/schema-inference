# Mapper Agent Roadmap — Backlog

Backlog of extensions to the schema-inference mapper agent pipeline
(`schema_inference/agents/`), captured after the 5-agent pipeline (PR #125)
landed and the CTE-depth performance bug was fixed. Items are grouped into
phases by dependency, not by priority within a phase — each phase header
states what unlocks it.

Numbering convention: `MAP-#`, distinct from the generator work packages
(`WP-#`) used for baseline/simulation data work.

> **Repository note:** this pipeline is being extracted into its own
> standalone repo (`premiumiq/schema-inference`). See the repo-split checklist
> in `docs/repo-split-schema-inference.md`. All MAP items below continue
> development there after the split.

---

## Phase A — Foundation ✅ Complete

### MAP-1: Persist mappings into a metamodel — **DONE**

SQLite-backed `MetamodelStore` at `schema_inference/metamodel/store.py`.
Four tables: `mapping_history`, `loss_runs`, `prompt_versions`,
`few_shot_examples`. Integrated into orchestrator (write on every run),
reviewer (write human actions), evaluator (write loss runs). `open_store()`
returns `None` on failure — never blocks the pipeline. `backfill.py` imports
existing registry JSON one-time. DB file at
`schema_inference/metamodel/metamodel.db` (gitignored).

**Outstanding:** SQLite → dbt models / Snowflake tables migration (deferred;
SQLite is correct for CI/local; revisit once real volume accumulates across
multiple client sources).

---

### MAP-2: Generalize the loss function — **DONE**

`scripts/score_mappings.py` now accepts any `{source_name}_schema_catalog.yml`
+ `{source_name}_value_catalog.json` pair (not hardcoded to PAS-L).
Loss function: `loss = hard_mult × (target_term × TARGET_WEIGHT +
calibration_penalty × CALIB_WEIGHT + sql_term × SQL_WEIGHT)`.
`AggregateMetrics` exposes `mean_loss`, `f1`, `hard_f1`, `sql_correctness_rate`.
Transformation-correctness check (`_check_transformation`) is static substring
matching against the value catalog's `transformation` field — no live SQL engine
needed. Per-source value catalogs exist for PAS-L
(`ground_truth/pasl_value_catalog.json`) and PAS-M
(`ground_truth/pasm_value_catalog.json`).

---

## Phase B — Contested mappings ✅ Complete

### MAP-3: Resolve near-tied mappings instead of arbitrary dedup — **DONE**

Merged PR #184 (2026-07-26).

`_deduplicate()` in `mapper.py` now detects near-ties
(`abs(conf_a − conf_b) < TIE_EPSILON = 0.05`):
- **Secondary-target escape:** if the canonical field has a `secondary_target`,
  the runner-up is promoted to it rather than demoted — both columns legitimately
  map.
- **Genuine contest:** escalated to CriticAgent with a *comparison* prompt
  ("decide between A and B for this target") rather than per-column review.
  Verified on real data: given REGN_CD vs INS_ST both claiming `region_code`,
  critic correctly picks REGN_CD citing data profile.
- **Unresolvable contests:** surfaced in `MappingProposal.contested_mappings`
  list, separate from `unmapped_columns`, for a dedicated reviewer phase
  (`_phase_contested_mappings` in `reviewer.py`).

`CanonicalField.secondary_target` added to `canonical/policy.py`. Full pipeline
on real PAS-L Snowflake data: 44/46 accuracy (F1 0.93), 100% hard-column
precision, no regression.

PAS-L rarely triggers contests (already maps cleanly); PAS-M's ambiguous
columns (`line_of_business` dual-target, etc.) are the natural next test case.

---

## Phase C — Self-tuning agent

### MAP-4: Self-tuning agent (loss-driven prompt/rule adjustment) — **LAYERS 0–2 DONE; LAYER 3 DEFERRED**

See dedicated design doc — [`self-tuning-mapper-agent-plan.md`](self-tuning-mapper-agent-plan.md).

**Build status:**

- **Layer 0** (numeric rule-weight tuning) — **done.** `tools/tune_rule_weights.py`.
  Grid-searches the `(name_sim, type_compat, pattern_bonus)` weight simplex,
  writes winning weights to `agent_config.yml`. PAS-L result:
  weights 0.65/0.25/0.10 → 0.15/0.20/0.65, F1 87.5% → 100%.
  **Per-source weights — done.** `agent_config.yml`'s `rule_engine.weights_by_source`
  (keyed by source name, falling back to the global `rule_engine.weights` for any
  unlisted source) means tuning one source's weights no longer clobbers another's.
  `mapper._rule_weights(source_name)` / `_rule_map_column(..., source_name=)` /
  `map_table(...)` thread it through; `tune_rule_weights.py --apply` writes to the
  source-specific key only (verified: a `pasm --apply` run leaves `pasl`'s mapping
  output byte-identical). First real PAS-M Layer 0 run: weights 0.65/0.25/0.10 →
  0.50/0.35/0.15, mean_loss 0.2771 → 0.2756 — marginal; F1/hard-F1 unchanged
  (85.7%/66.7%), confirming the gap below is a rule-engine ceiling, not a
  weight-tuning problem.

- **Layer 1** (few-shot example bank) — **done.** `tools/curate_few_shot_bank.py`,
  `schema_inference/metamodel/few_shot.py`. Scans `mapping_history` for
  hard TPs and critic-override-accepted rows; persists to `few_shot_examples`
  table (idempotent). Retrieval: 0.5 × flag-agreement + 0.5 × rapidfuzz name
  similarity, top-K injected into MappingAgent user prompt. Bank is empty in
  practice until real pipeline runs accumulate verdicts and reviewer actions.

- **Layer 2** (textual prompt tuning) — **done.** `tools/tune_prompts.py`.
  Diagnose → propose → validate loop: PromptDiagnosisAgent groups failure
  patterns, PromptTunerAgent proposes one targeted edit per round, re-runs
  pipeline on holdout split, accepts if loss improves with no regression.
  Diff guardrail (0.50–0.995 diff ratio), train/holdout split, human approval
  gate before prompt reaches production, early-stop after 3 consecutive failed
  rounds.
  **Scope**: tuning runs are scoped to the split/ambiguous column subset only
  (not full 46-col pipeline) via `_scoped_table()`.
  **First live PAS-M run** (`ANTHROPIC_API_KEY` set, mapping agent, 5 rounds
  requested / 3 run before early-stop): baseline holdout mean_loss 0.1738,
  F1 0.8889. All 3 candidate prompts rejected — 2 of 3 independently regressed
  specifically on `underwriting_tier` (mean_loss 0.3316), the 3rd tied the
  baseline exactly. No candidate accepted (correct behavior — the tuning loop
  never self-promotes; nothing cleared the improvement bar). Confirms prompt-
  level tuning alone doesn't close PAS-M's real gap (`line_of_business`
  dual-target, `insured_ein` trap, `underwriting_tier`/`commercial_rating_group`
  confusion) — that needs Layer 1 volume or Layer 3, not Layer 2.

- **Layer 3** (learned critic trigger) — **deferred.** Gate: two sources'
  ground truth now exists (PAS-L and PAS-M, `ground_truth/`). Confirmed real
  headroom on PAS-M: PAS-L-tuned weights transfer at only 76.5% F1; full
  re-tuning improves to 88.2% F1 / 66.7% hard-F1 — rule engine genuinely
  cannot resolve `line_of_business` dual-target or `insured_ein` customer_id
  trap. Layer 3 still needs accumulated `mapping_history` volume across both
  sources (not just the catalogs) before fitting a classifier is worthwhile.
  Revisit once 3+ client sources have gone through Layers 0–2.

---

### MAP-4.1: Multi-schema canonical targets — **DONE** (`pasm_coverage` onboarded as template)

The tool could previously only ever map to one hardcoded canonical schema
(`canonical/policy.py`'s `CANONICAL_FIELDS`, imported at module scope in 7
files). Onboarding a second real table required making the target schema
selectable per table instead of a single global.

- **`canonical/registry.py`** — `schema_for_table(table_name) -> schema_key`,
  with `TABLE_SCHEMA: dict[str, str]` mapping known table names to a schema
  key and every unlisted `table_name` falling back to `'policy'`. This
  fallback is what makes the refactor provably a no-op for every
  pre-existing table (verified: rule-only mapping output for both
  `pasl_policy` and `pasm_policy` is byte-identical to the pre-refactor
  commit with weights held constant).
- `mapper.py`, `orchestrator.py`: `canonical_fields`/`canonical_by_name`/
  `canonical_names` now flow as explicit params from `map_table()`/
  `run_mapping()` (which resolve `schema_key` once via `table.name`) down
  through `_rule_map_column`, `_run_llm_batch`, `_deduplicate`,
  missing-required-fields detection.
- `critic_agent.py`, `sql_agent.py`: `resolve_contests()`/`run_sql_agent()`
  take an optional `canonical_by_name`, defaulting to the `policy` schema.
- `tools.py`: the six agent tool functions (`lookup_canonical`,
  `generate_sql`, etc.) are invoked by the LLM via a fixed JSON tool schema
  with no way to pass "which target schema" as an argument. Extended the
  existing `contextvars`-based run-registry pattern (`register_profiles`)
  with `_SOURCE_NAME_VAR`/`_CANONICAL_SCHEMA_VAR` so tool calls resolve the
  right schema and the right per-source catalog for whichever run is active.

**Bug found and fixed along the way:** `tools.py`'s `check_value_catalog`/
`get_hard_columns` and `orchestrator.py`'s missing-field suppression were
hardcoded to `pasl_value_catalog.json`/`pasl_schema_catalog.yml` regardless
of which source was actually running — every PAS-M agent run had silently
been getting PAS-L's catalog data through those tools. Now resolved
per-source from `_SOURCE_NAME_VAR` / the `source_name` param.

**Template proof:** `pasm_coverage` onboarded end to end — fixtures pulled
from `insurance_data_ecosystem`'s generated `bronze_data/pasm_coverage.dat`
(12-row CI fixture + 49-row sample, matching the `pasl`/`pasm_policy`
tiering), `canonical/pasm_coverage.py` mirroring
`dbt_project/insurance_multi_pas/models/staging/pas_m/stg_pasm_coverage.sql`'s
column output (no silver-layer model exists yet upstream for this table, so
the staging model's `CAST`'d output is the authoritative target shape),
ground truth catalog at
`examples/insurance/ground_truth/pasm_coverage_schema_catalog.yml`. Rule-only
mapping scores F1 100% (11/11 columns correct) — expected, this table's
source/target column names are already near-identical, unlike
`pasl_policy`'s mainframe abbreviations.

**Remaining PAS-M tables — postponed until after MAP-7 design + initial
implementation.** Roadmap's original table list (`pasm_coverage`/
`pasm_premium_register`/`pasm_transaction_log`/`pasm_risk_object`) turned out
stale: bronze tables `pasm_premium_register` and `pasm_transaction_log` are
staged under *renamed* models, and `pasm_risk_object` has no staging model
at all. Corrected picture, and the steps to onboard each (same recipe
`pasm_coverage` used):

| Bronze table | Staging target | Status |
|---|---|---|
| `pasm_coverage` | `stg_pasm_coverage.sql` | done (template) |
| `pasm_premium_register` | `stg_pasm_written_premium.sql` | ready — real target exists, not yet onboarded |
| `pasm_transaction_log` | `stg_pasm_policy_event.sql` | ready — real target exists, not yet onboarded |
| `pasm_risk_object` | *(none)* | no upstream target — nothing to catalog against; skip until a staging model exists |

To onboard `pasm_premium_register` or `pasm_transaction_log`:
1. Pull fixtures: `head -13`/`head -50` of `insurance_data_ecosystem`'s
   `bronze_data/{table}.dat` into `examples/insurance/test_data/{table}.dat`
   (12-row CI fixture) and `{table}_sample.dat` (49-row sample).
2. Add `canonical/{table}.py` mirroring the staging model's `SELECT` output
   (column name, type, `required` = whichever columns have `not_null` dbt
   tests in that model's `schema.yml`), and register it in
   `canonical/registry.py`'s `_SCHEMAS`/`TABLE_SCHEMA`.
3. Write `examples/insurance/ground_truth/{table}_schema_catalog.yml`
   (`canonical_target`/`confidence_floor`/`transformation`/`is_hard`/`notes`
   per source column, same shape as `pasm_coverage_schema_catalog.yml`).
4. `profile` → `map --no-llm` → `scripts/score_mappings.py --source-name
   {table}` to verify against the catalog.

Decision: not done now — the multi-schema plumbing is proven with one
table, which is what actually unblocked MAP-7 (its UI needs schema-aware
mapping to exist, not exhaustive PAS-M coverage). Revisit after the MAP-7
design spike and initial implementation land, when there's a concrete UI
need for more demo tables.

---

## Phase D — Row-level transforms ✅ Complete

### MAP-5: Row identity + dedup inference agent — **DONE**

Merged PR #247 (2026-07-26, base corrected from `main` to
`feature/dual-PAS-schema-inference`).

`schema_inference/agents/row_shape_agent.py` — deterministic heuristics over
`TableProfile`:
- **Natural key:** distinct-ratio (0.6) + non-null rate (0.2) + `is_id_column`
  flag (0.2), argmax across business columns.
- **Recency column:** `_SEQ`/`_VER` integer-like ID > CDC operation flag >
  date column.
- **Dedup strategy:** `row_number` (key + recency), `cdc_latest` (CDC flag
  present), or `none` (table already one-row-per-entity).
- Confidence 0.0–1.0, same shape as `ColumnMapping.confidence`.

`RowShapeProposal` model added to `schema_inference/models.py`. Attached to
`MappingProposal.row_shape` (serialized as dict). Emitted from both the agent
orchestrator path and the legacy `map_table()` path — pure profile logic,
no LLM required.

`scripts/score_mappings.py` extended: `RowShapeScore` + `_score_row_shape()`
weighted loss (key 0.5, recency 0.3, strategy 0.2), scored against a
`row_shape` ground-truth section in the schema catalog.

`ground_truth/pasl_schema_catalog.yml` extended with `row_shape` section:
`natural_key: [POL_NO]`, `recency_column: POL_NO_SEQ`, `dedup_strategy: row_number`.
Detector matches exactly on PAS-L, confidence 0.9, row-shape loss 0.0.

LLM layer for ambiguous tables deferred — deterministic heuristics resolve
PAS-L cleanly. PAS-M is the natural next case to test whether the agent layer
earns its keep.

**Conflict resolution at merge:** MAP-3 and MAP-5 both added a field to
`MappingProposal(...)` at the same insertion point. Kept both:
`contested_mappings=contested` (MAP-3) and `row_shape=row_shape_proposal.model_dump()`
(MAP-5). `models.py` auto-resolved; `mapper.py` and `orchestrator.py` resolved
manually (two-line addition at each site).

---

## Phase E — Table-level mapping (exploratory only — not committed)

### MAP-6: Many-to-one / one-to-many table mapping

**Goal:** handle the case where one canonical entity is split across multiple
source tables (PAS-M's policy + coverage + premium + risk tables, hand-joined
today) or multiple source tables must merge into one target — without an
engineer hand-writing the join logic per source.

**Why flagged exploratory, not committed:** inferring join keys *across tables*,
reasoning about cardinality (1:1 vs 1:many vs many:many), and choosing an
aggregation strategy (sum? latest? first-non-null?) is genuinely harder than
column mapping or row-shape inference. Blast radius of a bad join (silent row
fan-out) is much higher than a column mapping mistake. Worth a design spike
before committing build time — not worth scoping in detail until MAP-1 through
MAP-5 are proven in production.

**Depends on:** MAP-5 (table-level mapping is a strict superset of row-shape
inference — you need row identity within each table before reasoning about
how tables relate).

---

## Phase F — IDE Integration

### MAP-7: VS Code extension + dbt integration

**Goal:** surface the full schema inference pipeline inside the developer's
editor, eliminating the CLI-only workflow and making the tool accessible to
non-Python-fluent data engineers and client SMEs.

**Scope:**
- **Inline column annotations** — after a mapping run, annotate `.dat`/`.csv`
  source files or staging model SQL with hover cards showing: proposed target
  field, confidence, method (rule/agent/critic), verdict (if reviewed).
- **Accept/reject review panel** — diff-style sidebar per column, same UX as
  a GitHub PR review comment thread; actions write back to `reviewer.py` and
  the metamodel without leaving the editor.
- **dbt staging model scaffolding** — given a completed mapping, generate the
  staging model `.sql` pre-filled with `CAST`/`COALESCE`/`NULLIF` stubs per
  mapped column; unmapped columns flagged as diagnostics (red squiggles).
- **Mapping health sidebar** — per-source F1, hard-F1, mean loss tiles, sourced
  from the metamodel's `loss_runs` table; refreshes after each mapping run.
- **Contested mapping panel** — surfaces `MappingProposal.contested_mappings`
  (MAP-3) for human resolution directly in-editor.
- **Row-shape display** — shows the inferred dedup key/strategy (MAP-5)
  alongside the column mappings so engineers validate it before copying to a
  dbt model.

**Architecture:** VS Code extension (`schema-inference-vscode`) consumes the
`schema_inference` Python package as a subprocess via the Language Server
Protocol or a lightweight JSON-RPC bridge. The extension lives in the same
`premiumiq/schema-inference` repo (post repo-split) as a `vscode/` workspace
subfolder.

**Depends on:** repo split ✅. MAP-3 ✅ and MAP-5 ✅ complete (contested
mappings and row-shape are core UI surfaces). MAP-4.1 ✅ (multi-schema
canonical targets) — the mapping health sidebar / row-shape display need to
be schema-aware (per-table, via `canonical/registry.py`) rather than
assuming one flat target; design against `pasl_policy` + `pasm_policy` +
`pasm_coverage` as the reference tables, not just one.

**All dependencies clear. Design spike is the next step.** Not yet started.
Remaining PAS-M table coverage (`pasm_premium_register`, `pasm_transaction_log`
— see MAP-4.1) is deliberately postponed until after this spike and an
initial implementation land, so the UI design is informed by a real (if
partial) multi-table dataset without blocking on exhaustive catalog work.

---

## Open design gaps

| Gap | Impact | Owner |
|-----|--------|-------|
| ~~Per-source rule weights in `agent_config.yml`~~ | **Done** — `rule_engine.weights_by_source` (MAP-4.1) | — |
| PAS-M remaining tables cataloged | `pasm_policy` + `pasm_coverage` have ground truth; `pasm_premium_register`/`pasm_transaction_log` have real staging targets but aren't onboarded yet; `pasm_risk_object` has no target at all | **Postponed until after MAP-7 design + initial implementation** (see MAP-4.1) |
| Layer 1 volume | Few-shot bank empty — needs real accumulated `mapping_history` with verdicts | Accumulates naturally once pipeline runs in production |
| ~~Layer 2 live quality~~ | **Done** — first live PAS-M run completed (see MAP-4 Layer 2 above): no improving candidate, correctly rejected | — |
| MAP-5 LLM layer | Deterministic heuristics handle PAS-L; ambiguous tables (PAS-M) may need LLM fallback | After PAS-M tested |
| WP-10 schema snapshots | `schema_snapshots.enabled` placeholder — no generator code; only test fixture built | Separate WP |

---

## Suggested build order going forward

1. ~~**Repo split**~~ — done.
2. ~~**Per-source rule weights**~~ — done (MAP-4.1).
3. ~~**Live Layer 2 run on PAS-M**~~ — done; no improving candidate found, correctly
   rejected (see MAP-4 Layer 2 above).
4. ~~**Multi-schema canonical targets + `pasm_coverage` template**~~ — done (MAP-4.1).
5. **MAP-7 design spike** — VS Code extension architecture. All dependencies clear
   — this is the next task.
6. **Remaining PAS-M table coverage** (`pasm_premium_register`,
   `pasm_transaction_log`) — postponed until after MAP-7's design spike and
   initial implementation land (see MAP-4.1 for the exact onboarding steps).
7. **MAP-6 design spike** — table-level mapping; low priority until MAP-5 is
   battle-tested across multiple sources.
8. **Layer 3** — revisit once 3+ client sources have real `mapping_history` volume.
