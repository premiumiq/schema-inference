# Example: P&C Insurance (PIQ)

Reference application of the schema inference tool to PremiumIQ's P&C insurance
data ecosystem (PAS-L legacy mainframe + PAS-M modern cloud PAS).

## Data tiers

| Tier | Files | Rows | Purpose |
|------|-------|------|---------|
| CI fixture | `test_data/pasl_policy.dat`, `test_data/pasm_policy.dat` | 12 | Profiler smoke test, fast CI, no Snowflake needed |
| Sample | `test_data/pasl_policy_sample.dat`, `test_data/pasm_policy_sample.dat` | 69 / 49 | Realistic column profiles, meaningful demo without Snowflake |
| Production | Snowflake `DEV_SANDBOX_DB.PASL.PASL_POLICY` | real | Via `--snowflake` flag with credentials |

Sample files are generated from `premiumiq/insurance_data_ecosystem`'s
`generate_baseline.py` and committed here as static data. Regenerate and
recommit when generators change materially.

## Ground truth catalogs

| File | Table | Hard columns |
|------|-------|-------------|
| `ground_truth/pasl_schema_catalog.yml` | `pasl_policy` (PAS-L, 46 cols) | 6 |
| `ground_truth/pasl_value_catalog.json` | `pasl_policy` | value types, transformations |
| `ground_truth/pasm_schema_catalog.yml` | `pasm_policy` (PAS-M, 23 cols) | 5 |
| `ground_truth/pasm_value_catalog.json` | `pasm_policy` | decimal dollars, boolean strings |

## Quickstart

```bash
# Rule engine only — no API key needed
python -m schema_inference map examples/insurance/test_data/pasl_policy.dat \
  --source-name pasl

# Score against ground truth
python scripts/score_mappings.py \
  schema_inference/registry/pasl/proposal_pasl_snowflake.json \
  --source-name pasl

# Layer 0 weight tuning (no API key)
python tools/tune_rule_weights.py --source-name pasl \
  --data-file examples/insurance/test_data/pasl_policy_sample.dat

# Full agent run (needs ANTHROPIC_API_KEY)
python -m schema_inference map examples/insurance/test_data/pasl_policy_sample.dat \
  --source-name pasl --agent --eval

# Production Snowflake run (needs credentials)
python -m schema_inference map \
  --snowflake DEV_SANDBOX_DB.PASL.PASL_POLICY \
  --source-name pasl --agent --eval
```

## Relationship to insurance_data_ecosystem

The tool (this repo) and the warehouse (`premiumiq/insurance_data_ecosystem`) are
decoupled. The warehouse generates the synthetic `.dat` sample files; once generated
they are inert flat files committed here. The generators, dbt models, and Snowflake
loaders all live in the warehouse repo.
