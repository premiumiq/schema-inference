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
  **Gap:** `agent_config.yml` has one global `rule_engine.weights` section —
  applying Layer 0 for PAS-M would clobber PAS-L's tuned weights. Per-source
  weights section needed before both sources can be tuned independently.

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
  rounds. Live diagnose/propose quality unverified without `ANTHROPIC_API_KEY`
  in CI; mechanics verified with injected fake LLM clients.
  **Scope**: tuning runs are scoped to the split/ambiguous column subset only
  (not full 46-col pipeline) via `_scoped_table()`.

- **Layer 3** (learned critic trigger) — **deferred.** Gate: two sources'
  ground truth now exists (PAS-L and PAS-M, `ground_truth/`). Confirmed real
  headroom on PAS-M: PAS-L-tuned weights transfer at only 76.5% F1; full
  re-tuning improves to 88.2% F1 / 66.7% hard-F1 — rule engine genuinely
  cannot resolve `line_of_business` dual-target or `insured_ein` customer_id
  trap. Layer 3 still needs accumulated `mapping_history` volume across both
  sources (not just the catalogs) before fitting a classifier is worthwhile.
  Revisit once 3+ client sources have gone through Layers 0–2.

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

**Depends on:** repo split (MAP-7 extension can't live in the insurance
warehouse repo). MAP-3 and MAP-5 complete (contested mappings and row-shape
are core UI surfaces).

**Not yet started.** Design spike is the logical first step once the repo
split is complete.

---

## Open design gaps

| Gap | Impact | Owner |
|-----|--------|-------|
| Per-source rule weights in `agent_config.yml` | Layer 0 tuning for PAS-M clobbers PAS-L's weights | Before next Layer 0 `--apply` run on pasm |
| PAS-M other tables cataloged | Only `pasm_policy` has ground truth; `pasm_coverage`/`pasm_premium_register`/`pasm_transaction_log`/`pasm_risk_object` uncatalogued | Blocking PAS-M end-to-end scoring |
| Layer 1 volume | Few-shot bank empty — needs real accumulated `mapping_history` with verdicts | Accumulates naturally once pipeline runs in production |
| Layer 2 live quality | tune_prompts.py mechanics verified with fake LLM client; live quality (actual prompt edit usefulness) not verified | Needs `ANTHROPIC_API_KEY` + real PAS-M run |
| MAP-5 LLM layer | Deterministic heuristics handle PAS-L; ambiguous tables (PAS-M) may need LLM fallback | After PAS-M tested |
| WP-10 schema snapshots | `schema_snapshots.enabled` placeholder — no generator code; only test fixture built | Separate WP |

---

## Suggested build order going forward

1. **Repo split** — extract `schema_inference` into `premiumiq/schema-inference`
   (see `docs/repo-split-schema-inference.md`). Unblocks MAP-7.
2. **Per-source rule weights** — design + implement before any more Layer 0
   `--apply` runs on PAS-M.
3. **MAP-7 design spike** — VS Code extension architecture once repo split lands.
4. **MAP-6 design spike** — table-level mapping; low priority until MAP-5 is
   battle-tested across multiple sources.
5. **Layer 3** — revisit once 3+ client sources have real `mapping_history` volume.
