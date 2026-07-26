# schema-inference

Schema inference pipeline for P&C (and general) insurance data ecosystems.
Profiles a source file or Snowflake table, maps each column to a canonical
data model target, generates the SQL transformation expression, and self-tunes
its own accuracy against a ground truth catalog — replacing weeks of manual
reverse-engineering with a single CLI command.

## Components

| Component | Path | Description |
|-----------|------|-------------|
| Profiler | `schema_inference/profiler.py` | Column statistics from raw files or Snowflake |
| Rule engine | `schema_inference/mapper.py` | Deterministic mapper, weight-tuned per source |
| Agent pipeline | `schema_inference/agents/` | MappingAgent → CriticAgent → SQLAgent → EvaluatorAgent → Orchestrator |
| Metamodel | `schema_inference/metamodel/` | SQLite-backed mapping history, loss runs, prompt versions |
| Loss function | `scripts/score_mappings.py` | Continuous per-column loss + ground truth scoring |
| Self-tuning | `tools/` | Layer 0 (rule weights), Layer 1 (few-shot bank), Layer 2 (prompt tuning) |
| Reviewer | `schema_inference/reviewer.py` | Interactive + auto-review; human feedback → metamodel |
| IDE Extension | `vscode/` | VS Code + dbt integration (MAP-7, in design) |

## Install

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env        # fill in ANTHROPIC_API_KEY (and Snowflake creds if needed)
```

## Quickstart

`map` always takes a profile JSON (produced by `profile`), not a raw source file —
it's a two-step `profile` → `map` pipeline.

```bash
# 1. Profile the source file (writes registry/{source}/profile_{table}.json)
python -m schema_inference profile examples/insurance/test_data/pasl_policy.dat \
  --source-name pasl

# 2a. Rule engine only — no API key
python -m schema_inference map schema_inference/registry/pasl/profile_pasl_policy.json \
  --table-name pasl_policy --no-llm

# 2b. Full agent run
python -m schema_inference profile examples/insurance/test_data/pasl_policy_sample.dat \
  --source-name pasl
python -m schema_inference map schema_inference/registry/pasl/profile_pasl_policy_sample.json \
  --table-name pasl_policy_sample --agent --eval

# Score against ground truth
python scripts/score_mappings.py \
  schema_inference/registry/pasl/proposal_pasl_snowflake.json \
  --source-name pasl

# Layer 0 weight tuning (no API key)
python tools/tune_rule_weights.py --source-name pasl

# Layer 2 prompt tuning (needs API key)
python tools/tune_prompts.py --source-name pasl --rounds 5
```

See `examples/insurance/README.md` for the full three-tier data quickstart
(CI fixture / sample / Snowflake production).

## Other domains

Set `SCHEMA_INFERENCE_CATALOG_DIR` to point at your own ground truth catalogs:
```bash
SCHEMA_INFERENCE_CATALOG_DIR=/path/to/your/ground_truth \
  python -m schema_inference profile your_source.dat --source-name your_source
SCHEMA_INFERENCE_CATALOG_DIR=/path/to/your/ground_truth \
  python -m schema_inference map schema_inference/registry/your_source/profile_your_source.json \
  --table-name your_source
```

## Tests

```bash
python -m pytest tests/ -v -m "not snowflake"
```
