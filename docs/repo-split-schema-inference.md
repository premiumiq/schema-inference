# Repo Split: `premiumiq/schema-inference`

Extract the schema inference pipeline from `insurance_data_ecosystem` into its
own standalone repository.

---

## Separation philosophy (Route C)

Two repositories, one clean boundary:

| Repo | Contains | Does not contain |
|------|----------|-----------------|
| `premiumiq/schema-inference` | Tool code, tuning scripts, scorer, CI, VS Code extension, **example data used to validate the tool** | Warehouse DDL, dbt models, data generators, Snowflake loaders |
| `premiumiq/insurance_data_ecosystem` | Bronze tables, dbt project(s), generators, loaders, simulation | Schema inference tool, tuning scripts, scorer |

**The boundary for data:** raw source rows (`.dat` files) and ground truth
catalogs belong in `schema-inference` — not because they are part of the tool's
code, but because they are the **example application** of the tool to the
insurance domain. The warehouse repo *generates* them; once generated they are
inert files the tool consumes, with no live coupling back to the warehouse.

**What the .dat files are (and aren't):** the profiler reads rows to compute
column statistics (type, cardinality, null rate, patterns). The agents and
tuning layers never see individual rows — they see column profiles. The .dat
files are therefore a portable stand-in for a Snowflake table, not training
data. The self-tuning layers train against the *labels* in the ground truth
catalogs, not against raw rows. A 200-row .dat file produces the same profile
quality as a 10,000-row Snowflake table for the features that matter (type
inference, pattern detection, cardinality ratio stabilise at ~50 rows).

**Three data tiers in the example:**

| Tier | File | Rows | Purpose |
|------|------|------|---------|
| CI fixture | `examples/insurance/test_data/pasl_policy.dat` | 12 | Profiler smoke test, fast CI, no Snowflake |
| Generated sample | `examples/insurance/test_data/pasl_policy_200.dat` | 200 | Realistic demo, meaningful profile statistics, still no Snowflake |
| Production | Snowflake `DEV_SANDBOX_DB.PASL_POLICY` | real | Via `schema_inference/snowflake_reader.py` with credentials |

Generated samples are produced once by `insurance_data_ecosystem`'s
`generate_baseline.py`, then committed to `schema-inference` as static files.
When the generators change materially, regenerate and recommit. No live
dependency.

**What stays in insurance_data_ecosystem:** everything that requires the
warehouse or generates warehouse data — `generators/`, `baseline/`,
`dbt_project/`, `scripts/load_multi_pas_sandbox.py`, `bronze_data/`, `config/`,
`targets/`. The multi-PAS work (generators, dbt models, loaders) stays here
because it is the warehouse-side application of the tool, not the tool itself.

---

## What moves

| Source path (insurance_data_ecosystem) | Destination path (schema-inference) |
|----------------------------------------|--------------------------------------|
| `schema_inference/` | `schema_inference/` |
| `ground_truth/` | `examples/insurance/ground_truth/` |
| `schema_inference/test_data/` (extracted from within schema_inference/) | `examples/insurance/test_data/` |
| `scripts/score_mappings.py` | `scripts/score_mappings.py` |
| `tools/tune_rule_weights.py` | `tools/tune_rule_weights.py` |
| `tools/tune_prompts.py` | `tools/tune_prompts.py` |
| `tools/curate_few_shot_bank.py` | `tools/curate_few_shot_bank.py` |
| `tools/generate_schema_drift_variant.py` | `tools/generate_schema_drift_variant.py` |
| `requirements-schema-inference.txt` | `requirements.txt` |
| `test_*.py` (schema-inference tests at repo root) | `tests/` |
| `docs/mapper-agent-roadmap.md` | `docs/mapper-agent-roadmap.md` |
| `docs/self-tuning-mapper-agent-plan.md` | `docs/self-tuning-mapper-agent-plan.md` |
| `docs/mapping-agent-batching-plan.md` | `docs/mapping-agent-batching-plan.md` |
| `docs/repo-split-schema-inference.md` | `docs/repo-split-schema-inference.md` |

**Target layout in `schema-inference`:**

```
schema-inference/
  schema_inference/          ← tool (profiler, mapper, agents, metamodel, reviewer)
  scripts/
    score_mappings.py        ← loss function / evaluator
  tools/                     ← tuning scripts (Layer 0/1/2)
  examples/
    insurance/               ← example application of tool to PIQ insurance data
      ground_truth/
        pasl_schema_catalog.yml
        pasl_value_catalog.json
        pasm_schema_catalog.yml
        pasm_value_catalog.json
      test_data/
        pasl_policy.dat              ← 12-row CI fixture
        pasm_policy.dat              ← 12-row CI fixture
        pasl_policy_200.dat          ← 200-row generated sample (committed)
        pasm_policy_200.dat          ← 200-row generated sample (committed)
        pasl_policy_schema_drift.dat ← schema drift variant for Layer 1 testing
      README.md              ← three-tier quickstart (fixture / sample / Snowflake)
  tests/                     ← unit + integration tests (no Snowflake)
  vscode/                    ← MAP-7 VS Code extension (placeholder at split time)
  docs/
  pyproject.toml
  requirements.txt
  .env.example
  schema-inference.code-workspace
```

**Git history:** use `git filter-repo` (not `git subtree`) to carry commit
history for the moved paths. See step 3 below.

---

## Step-by-step

### 1. Create the new GitHub repository

```bash
gh repo create premiumiq/schema-inference \
  --private \
  --description "Schema inference pipeline — column mapping, row-shape detection, self-tuning" \
  --clone
cd schema-inference
```

### 2. Install `git-filter-repo`

```bash
pip install git-filter-repo
# or: brew install git-filter-repo
```

### 3. Extract history from insurance_data_ecosystem

Run in a **fresh throwaway clone** — never on your working copy. `filter-repo`
rewrites history in place.

```bash
git clone https://github.com/premiumiq/insurance_data_ecosystem.git insurance_extract
cd insurance_extract

git filter-repo \
  --path schema_inference/ \
  --path ground_truth/ \
  --path "scripts/score_mappings.py" \
  --path tools/ \
  --path "requirements-schema-inference.txt" \
  --path "test_contest.py" \
  --path "test_dedup.py" \
  --path "test_mapper_rowshape.py" \
  --path "test_orchestrator_rowshape.py" \
  --path "test_rowshape.py" \
  --path "test_rowshape_score.py" \
  --path "docs/mapper-agent-roadmap.md" \
  --path "docs/self-tuning-mapper-agent-plan.md" \
  --path "docs/mapping-agent-batching-plan.md" \
  --path "docs/repo-split-schema-inference.md"
```

### 4. Push extracted history to the new repo

```bash
git remote set-url origin https://github.com/premiumiq/schema-inference.git
git push --all
git push --tags
```

### 5. Restructure paths in the new repo

```bash
# Move tests to tests/
mkdir -p tests
git mv test_contest.py tests/
git mv test_dedup.py tests/
git mv test_mapper_rowshape.py tests/
git mv test_orchestrator_rowshape.py tests/
git mv test_rowshape.py tests/
git mv test_rowshape_score.py tests/

# Move ground truth and test fixtures into examples/insurance/
mkdir -p examples/insurance
git mv ground_truth examples/insurance/ground_truth

# test_data is currently inside schema_inference/; move it out
git mv schema_inference/test_data examples/insurance/test_data

# Rename requirements
git mv requirements-schema-inference.txt requirements.txt

git commit -m "restructure: examples/insurance/, tests/, rename requirements"
```

### 6. Update path references for the moved examples

Four `DEFAULT_DATA_FILE` / `DEFAULT_CATALOG_PATH` references point at the old
locations. Update them to the new `examples/insurance/` paths:

**`tools/tune_rule_weights.py`:**
```python
DEFAULT_DATA_FILE = {
    "pasl": _REPO_ROOT / "examples" / "insurance" / "test_data" / "pasl_policy.dat",
    "pasm": _REPO_ROOT / "examples" / "insurance" / "test_data" / "pasm_policy.dat",
}
```

**`tools/tune_prompts.py`:** same `DEFAULT_DATA_FILE` pattern.

**`scripts/score_mappings.py`:** `_catalog_path_for(source_name)` currently
resolves `ground_truth/{source_name}_schema_catalog.yml`. Update to
`examples/insurance/ground_truth/{source_name}_schema_catalog.yml`, or make
the base path configurable via an env var (`SCHEMA_INFERENCE_CATALOG_DIR`)
with the examples path as default — cleaner for future non-insurance use.

**`tools/generate_schema_drift_variant.py`:** update output path from
`schema_inference/test_data/` to `examples/insurance/test_data/`.

No changes needed to internal `schema_inference/` imports — `score_mappings.py`
stays at `scripts/score_mappings.py` and the `sys.path.insert` lines in
`tools/` remain valid.

### 7. Add and commit 200-row generated samples

Generate from `insurance_data_ecosystem`:

```bash
# In insurance_data_ecosystem (with venv active)
python generate_baseline.py --policies 200 --seed 42

# Extract PAS-L and PAS-M samples
# (bronze_data/pasl_policy.dat and bronze_data/pasm_policy.dat are written by the generator)
head -201 bronze_data/pasl_policy.dat > /path/to/schema-inference/examples/insurance/test_data/pasl_policy_200.dat
head -13  bronze_data/pasl_policy.dat > /path/to/schema-inference/examples/insurance/test_data/pasl_policy.dat
head -201 bronze_data/pasm_policy.dat > /path/to/schema-inference/examples/insurance/test_data/pasm_policy_200.dat
head -13  bronze_data/pasm_policy.dat > /path/to/schema-inference/examples/insurance/test_data/pasm_policy.dat
```

Commit these to `schema-inference`. Regenerate and recommit only when the
generators change materially (new columns, changed value distributions).

### 8. Add standalone project files

**`pyproject.toml`**:
```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.backends.legacy:build"

[project]
name = "schema-inference"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "anthropic>=0.40.0",
    "pydantic>=2.0",
    "pyyaml>=6.0",
    "rapidfuzz>=3.0",
    "snowflake-connector-python>=3.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio"]

[project.scripts]
schema-inference = "schema_inference.__main__:main"

[tool.setuptools.packages.find]
where = ["."]
include = ["schema_inference*"]
```

**`.env.example`**:
```
ANTHROPIC_API_KEY=sk-ant-...
SNOWFLAKE_ACCOUNT=orgname-accountname
SNOWFLAKE_USER=SVC_DBT_DEV
SNOWFLAKE_PRIVATE_KEY_PATH=/path/to/key.p8
SNOWFLAKE_DATABASE=DEV_INSURANCE_DB
SNOWFLAKE_WAREHOUSE=DEV_WH
SNOWFLAKE_ROLE=DEV-INSURANCE-PRODUCER
SCHEMA_INFERENCE_DISABLE_THROTTLE=   # set to "1" to bypass rate limiter in dev
SCHEMA_INFERENCE_CATALOG_DIR=examples/insurance/ground_truth  # default; override for other domains
```

**`README.md`** — description, install, three-tier quickstart.

**`examples/insurance/README.md`** — explains the data tiers:
- 12-row fixtures: CI and smoke tests
- 200-row samples: generated from `insurance_data_ecosystem`, committed as static example data; regenerate when generators change
- Snowflake: production path via `--snowflake` flag

### 9. Set up CI

`.github/workflows/ci.yml`:
```yaml
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -e ".[dev]"
      - run: python -m pytest tests/ -v -m "not snowflake"
      - name: Smoke test — rule engine on CI fixture
        run: |
          python -m schema_inference map \
            examples/insurance/test_data/pasl_policy.dat \
            --source-name pasl
      - name: Score against ground truth
        run: |
          python scripts/score_mappings.py \
            schema_inference/registry/pasl/proposal_pasl_snowflake.json \
            --source-name pasl
```

No Anthropic API key in CI. Snowflake tests excluded via `-m "not snowflake"`.

### 10. Gitignore for new repo

```
# metamodel state
schema_inference/metamodel/metamodel.db

# scratch scripts
show_agent_output.py
show_schemas.py

# venv / build
.venv/
*.egg-info/
dist/
```

### 11. Add VS Code workspace config

`schema-inference.code-workspace`:
```json
{
  "folders": [
    { "path": ".", "name": "schema-inference (core)" },
    { "path": "vscode", "name": "schema-inference (extension)" }
  ],
  "settings": {
    "python.defaultInterpreterPath": "${workspaceFolder}/.venv/bin/python"
  }
}
```

`vscode/` subfolder is the MAP-7 VS Code extension — placeholder at split time.
Create `vscode/.gitkeep` and `vscode/DESIGN.md` describing planned scope.

### 12. Update insurance_data_ecosystem to remove moved files

On a new branch in `insurance_data_ecosystem`:

```bash
git rm -r schema_inference/
git rm -r ground_truth/
git rm scripts/score_mappings.py
git rm tools/tune_rule_weights.py tools/tune_prompts.py \
       tools/curate_few_shot_bank.py tools/generate_schema_drift_variant.py
git rm test_contest.py test_dedup.py test_mapper_rowshape.py \
       test_orchestrator_rowshape.py test_rowshape.py test_rowshape_score.py
git rm requirements-schema-inference.txt
git rm docs/mapper-agent-roadmap.md docs/self-tuning-mapper-agent-plan.md \
       docs/mapping-agent-batching-plan.md docs/repo-split-schema-inference.md
```

Leave a `docs/schema-inference.md` stub pointing to `premiumiq/schema-inference`
so existing internal links don't 404.

Remove `schema_inference/metamodel/metamodel.db` from `.gitignore` (no longer
relevant here).

Open PR against `main` — destructive removal, goes through normal review.

### 13. Verify development works end-to-end in the new repo

```bash
cd schema-inference
python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# CI fixture — rule engine, no API key
python -m schema_inference map \
  examples/insurance/test_data/pasl_policy.dat \
  --source-name pasl

# 200-row sample — more realistic profile
python -m schema_inference map \
  examples/insurance/test_data/pasl_policy_200.dat \
  --source-name pasl

# Score against ground truth
python scripts/score_mappings.py \
  schema_inference/registry/pasl/proposal_pasl_snowflake.json \
  --source-name pasl

# Layer 0 tuning (no API key)
python tools/tune_rule_weights.py --source-name pasl \
  --data-file examples/insurance/test_data/pasl_policy_200.dat

# Full agent run (needs ANTHROPIC_API_KEY)
python -m schema_inference map \
  examples/insurance/test_data/pasl_policy_200.dat \
  --source-name pasl --agent --eval

# Production — real Snowflake table (needs credentials)
python -m schema_inference map \
  --snowflake DEV_SANDBOX_DB.PASL.PASL_POLICY \
  --source-name pasl --agent --eval
```

---

## After the split: first tasks in the new repo

1. **Per-source rule weights** — redesign `agent_config.yml` to support
   `rule_engine.weights_by_source` keyed by source name, with a global
   fallback. Update `tune_rule_weights.py --apply` to write to the source-
   specific key. Blocking for PAS-M Layer 0 apply.
2. **`SCHEMA_INFERENCE_CATALOG_DIR` env var** — wire into `score_mappings.py`
   `_catalog_path_for()` so other domains can point at their own ground truth
   without modifying code.
3. **MAP-7 design spike** — VS Code extension architecture in `vscode/DESIGN.md`.
4. **Live Layer 2 run on PAS-M** — first real end-to-end self-tuning
   demonstration (needs `ANTHROPIC_API_KEY`); PAS-M has genuine headroom
   (88.2% F1 / 66.7% hard-F1 ceiling from rule engine alone).
5. **PAS-M additional table coverage** — separate catalog files per table
   (`pasm_coverage`, `pasm_premium_register`, `pasm_transaction_log`,
   `pasm_risk_object`) in `examples/insurance/ground_truth/`.
