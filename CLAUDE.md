# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Schema inference pipeline for P&C insurance data: profiles a source file or Snowflake
table, maps each column to a canonical data model target, generates the dbt SQL
transformation expression, and self-tunes its own accuracy against a ground truth
catalog. `examples/insurance/` is a reference application of the tool to PremiumIQ's
PAS-L (legacy mainframe) / PAS-M (modern cloud) insurance data — not fixture noise,
it's the example domain the tool is validated against. The tool itself is domain-
agnostic; other sources plug in via `SCHEMA_INFERENCE_CATALOG_DIR`.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env        # fill in ANTHROPIC_API_KEY (and Snowflake creds if needed)
```

## Common commands

```bash
# Rule engine only — no API key
python -m schema_inference map examples/insurance/test_data/pasl_policy.dat --source-name pasl

# Full 5-agent pipeline, scored against ground truth
python -m schema_inference map examples/insurance/test_data/pasl_policy_sample.dat \
  --source-name pasl --agent --eval

# Score an existing proposal against ground truth
python scripts/score_mappings.py schema_inference/registry/pasl/proposal_pasl_snowflake.json \
  --source-name pasl

# Layer 0: rule-weight grid search (no API key)
python tools/tune_rule_weights.py --source-name pasl

# Layer 2: prompt tuning (needs API key)
python tools/tune_prompts.py --source-name pasl --rounds 5

# Full test suite (matches CI)
python -m pytest tests/ -v -m "not snowflake"

# Single test
python -m pytest tests/test_contest.py -v
python -m pytest tests/test_contest.py::test_specific_case -v
```

CI (`.github/workflows/ci.yml`) runs the pytest suite, then a rule-engine smoke test
on the 12-row CI fixture, then scores it against ground truth. No Anthropic key in CI.

## Architecture

Pipeline: **profile → map → review**, with **track** for schema-drift detection
between runs. `schema_inference/__main__.py` dispatches these as CLI subcommands.

### Profiling
`profiler.py` (flat file) / `snowflake_reader.py` (live table, RSA key-pair auth)
both produce the same `SchemaProfile` → `TableProfile` → `ColumnProfile` shape
(`models.py`), so everything downstream is source-agnostic. Each `ColumnProfile`
carries inferred type, null rate, sample values, and derived flags
(`is_id_column`, `is_coded_column`, `is_cents_integer`, `date_format`) that the
mapper and agents key off of.

### Mapping — two parallel implementations, same rule core
- **`mapper.py`**: rule pass (rapidfuzz name similarity + type compatibility +
  suffix pattern bonus, weighted sum — weights live in `agent_config.yml`'s
  `rule_engine.weights`, tunable by `tools/tune_rule_weights.py`) then an optional
  single-shot batched LLM call (`_run_llm_batch`) for columns below
  `llm_threshold`. This is the `map` command's default path.
- **`agents/orchestrator.py`** (`--agent` flag): runs `mapper.py`'s same rule pass,
  then replaces the batch LLM call with a 5-stage pipeline:
  1. Rule pass (`mapper._rule_map_column`, unchanged)
  2. `MappingAgent` (`agents/mapping_agent.py`) — per-column tool-use loop
     (Claude Haiku) for low-confidence columns; can call tools
     (`agents/tools.py`) to look up canonical fields / value catalogs instead
     of guessing from the name alone
  3. `CriticAgent` (`agents/critic_agent.py`) — adversarial re-review of hard
     and below-floor columns, plus contest resolution for near-ties
  4. `SQLAgent` (`agents/sql_agent.py`) — finalizes SQL for critic-overridden
     columns
  5. `EvaluatorAgent` (`agents/evaluator_agent.py`, `--eval` flag, demo/CI only)
     — wraps `scripts/score_mappings.py`

  Both paths converge on `_deduplicate()` (MAP-3: resolves multiple source
  columns targeting the same field — clear winner, near-tie promoted to a
  field's `secondary_target`, or a genuine contest recorded for the critic/
  reviewer) and `row_shape_agent.infer_row_shape()` (MAP-5: deterministic
  natural-key + dedup-strategy inference from profile stats alone, no LLM).

- **`canonical/policy.py`**: the target schema — `CANONICAL_FIELDS`, mirroring
  `slv_policy` silver-table columns in the (separate) warehouse repo. Adding
  aliases here is the primary lever for rule-pass recall; do not add new
  canonical fields without a corresponding warehouse-side column.

### Self-tuning layers (`tools/`)
- **Layer 0** (`tune_rule_weights.py`): grid-searches `mapper.py`'s three rule
  weights against ground truth, writes the winner to `agent_config.yml`.
- **Layer 1** (`curate_few_shot_bank.py` + `metamodel/few_shot.py`): curates a
  bank of past correct mappings (`hard_tp` / `critic_override_accepted`
  origins) from `mapping_history`; `MappingAgent` retrieves similar past
  examples by profile-flag + name-similarity score and injects them into its
  prompt.
- **Layer 2** (`tune_prompts.py`): iterates candidate system prompts for the
  agents against the loss function, logs every candidate to
  `prompt_versions` (append-only), and only a human `--accept` call
  (`MetamodelStore.accept_prompt_version`) makes a candidate active — the
  tuning loop itself never self-promotes a prompt.

See `docs/self-tuning-mapper-agent-plan.md`, `docs/mapper-agent-roadmap.md`,
and `docs/mapping-agent-batching-plan.md` for the design rationale behind
each layer and the current state of each initiative (referenced by MAP-N
ticket numbers in code comments).

### Metamodel store (`metamodel/store.py`)
SQLite (WAL mode), gitignored at `schema_inference/metamodel/metamodel.db`.
Tracks every mapping decision (`mapping_history`), scoring runs (`loss_runs`),
and prompt tuning history (`prompt_versions`). `open_store()` returns `None`
on any failure instead of raising — history is always optional and must
never block the mapping/review/scoring pipeline. Intended to migrate to a
warehouse-managed table eventually; method signatures are meant to stay
stable across that migration.

### Rate limiting (`agents/throttle.py`)
A single process-wide pacer (min spacing between calls, configured via
`agent_config.yml`'s `rate_limit.requests_per_minute`) shared across every
live Anthropic call — mapping/critic/sql agents and `tune_prompts.py`. This
exists because Anthropic's org-level rate limit is shared account-wide, and
reactive-only retry converges slowly once concurrency exceeds it (the
`MappingAgent`'s default 10-way column concurrency would otherwise fire ~10
near-simultaneous requests against a 5 RPM cap). `SCHEMA_INFERENCE_DISABLE_THROTTLE=1`
bypasses it — tests only, never for real runs.

### Reviewer / tracker
`reviewer.py` turns an accepted `MappingProposal` into a `MappingDefinition`
(interactive, or `--auto` with a confidence threshold for test-fixture
generation — not a substitute for real review). `tracker.py` compares a new
profile's column fingerprint against the last recorded `SchemaVersion` and
raises `BreakingSchemaChangeError` on removed columns or type changes,
otherwise reports renames/additions and which new columns need mapping.

### Config
`schema_inference/agent_config.yml` is the single tunable-parameters file:
rule-engine weights, per-agent model IDs, thresholds, concurrency, and the
rate limiter's RPM. Code always reads it through a loader with a hardcoded
fallback (`_rule_weights()`, `load_agent_config()`, etc.) so a missing or
partial file degrades gracefully rather than crashing.

## Data tiers (`examples/insurance/`)

| Tier | Files | Purpose |
|------|-------|---------|
| CI fixture | `test_data/pasl_policy.dat`, `pasm_policy.dat` (12 rows) | fast CI, no Snowflake |
| Sample | `*_sample.dat` (69/49 rows) | realistic profiles, demo without Snowflake |
| Production | Snowflake `DEV_SANDBOX_DB.PASL.PASL_POLICY` | via `--snowflake`, needs credentials |

Sample `.dat` files are generated by the separate `insurance_data_ecosystem` repo's
generator and committed here as static, inert files — regenerate/recommit only when
the generators change materially. Ground truth catalogs live in
`examples/insurance/ground_truth/{source}_schema_catalog.yml` (+ `_value_catalog.json`).

## Relationship to other repos

This repo was split out of `insurance_data_ecosystem` (see
`docs/repo-split-schema-inference.md` for the full history/rationale). The
warehouse repo (dbt models, generators, Snowflake loaders, bronze tables) is
fully decoupled — this repo only consumes already-generated `.dat` files and
Snowflake tables, with no live coupling back.

`vscode/` is a placeholder for a planned VS Code + dbt integration (MAP-7,
see `vscode/DESIGN.md`) — not yet implemented.
