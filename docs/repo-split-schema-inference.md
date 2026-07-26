# Repo Split: `premiumiq/schema-inference`

Extract the schema inference pipeline from `insurance_postgres`
(`insurance_data_ecosystem`) into its own standalone repository.

**Why:** the tool is already loosely coupled — `schema_inference/` has its own
`requirements-schema-inference.txt`, its own `agent_config.yml`, its own CLI
entry point (`__main__.py`), and zero runtime imports from the warehouse
codebase. Keeping it in the warehouse repo creates friction (VS Code extension,
independent CI, client-specific deployments) and couples its release cycle to
warehouse changes.

---

## What moves

| Source path (insurance_postgres) | Destination path (schema-inference) |
|----------------------------------|--------------------------------------|
| `schema_inference/` | `schema_inference/` |
| `ground_truth/` | `ground_truth/` |
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

**Stays in insurance_postgres:**
- Everything in `dbt_project/`, `generators/`, `baseline/`, `scripts/` (except
  `score_mappings.py`), `bronze_data/`, `config/`, `targets/`, `docs/` (except
  the three mapper docs above).

**Git history:** use `git filter-repo` (not `git subtree`) to carry commit
history for the moved paths. See step 3 below.

---

## Step-by-step

### 1. Create the new GitHub repository

```bash
gh repo create premiumiq/schema-inference \
  --private \
  --description "P&C schema inference pipeline — column mapping, row-shape detection, self-tuning" \
  --clone
cd schema-inference
```

### 2. Install `git-filter-repo`

```bash
pip install git-filter-repo
# or: brew install git-filter-repo
```

### 3. Extract history from insurance_postgres

Run in a **fresh clone** of `insurance_data_ecosystem` (never on your working copy):

```bash
git clone https://github.com/premiumiq/insurance_data_ecosystem.git insurance_extract
cd insurance_extract

git filter-repo --path schema_inference/ \
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

This rewrites the cloned repo so only those paths remain, with their full
commit history. Do not add `--force` on your working copy — do this on the
throwaway clone.

### 4. Push extracted history to the new repo

```bash
git remote set-url origin https://github.com/premiumiq/schema-inference.git
git push --all
git push --tags
```

### 5. Restructure paths in the new repo

```bash
# Move score_mappings out of scripts/ to a more natural location
# (optional — keep scripts/ for consistency)

# Move tests to tests/ directory
mkdir -p tests
git mv test_contest.py tests/
git mv test_dedup.py tests/
git mv test_mapper_rowshape.py tests/
git mv test_orchestrator_rowshape.py tests/
git mv test_rowshape.py tests/
git mv test_rowshape_score.py tests/

# Rename requirements file
git mv requirements-schema-inference.txt requirements.txt

git commit -m "restructure: move tests to tests/, rename requirements"
```

### 6. Update internal imports and paths

Four places reference `scripts/score_mappings.py` via `sys.path.insert`:
- `tools/tune_rule_weights.py` — `sys.path.insert(0, str(_REPO_ROOT / "scripts"))`
- `tools/tune_prompts.py` — same
- `schema_inference/agents/evaluator_agent.py` — imports `score_mappings`
- `schema_inference/agents/orchestrator.py` — may import scorer indirectly

In the new repo, `scripts/score_mappings.py` stays at `scripts/score_mappings.py`
so these paths remain valid. No import changes needed unless you promote
`score_mappings.py` to a proper package module (recommended eventually, not
blocking).

`schema_inference/canonical/policy.py` is insurance-domain-specific.
Keep it — the canonical field definitions belong with the tool, not the
warehouse. If you later want to generalize beyond P&C, extract it to a
`canonical/` plugin system, but that's MAP-6-era scope.

### 7. Add standalone project files

**`pyproject.toml`** (replaces ad-hoc setup):
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
```

**`README.md`** — brief description, install steps, quickstart commands
(map / review / score / tune-weights / tune-prompts).

### 8. Set up CI

`.github/workflows/ci.yml` — lightweight:
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
      - run: python -m pytest tests/ -v
      - run: python scripts/score_mappings.py \
               schema_inference/registry/pasl/proposal_pasl_snowflake.json \
               --source-name pasl
```

No Anthropic API key in CI (agent tests use injected fake client). Snowflake
tests skipped in CI (need credentials); mark them with `@pytest.mark.snowflake`
and exclude via `-m "not snowflake"`.

### 9. Gitignore additions for new repo

```
# metamodel state (generated, not committed)
schema_inference/metamodel/metamodel.db

# scratch scripts
show_agent_output.py
show_schemas.py

# venv
.venv/
*.egg-info/
```

### 10. Add VS Code workspace config

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

`vscode/` subfolder is the VS Code extension (MAP-7). Placeholder for now —
create an empty `vscode/.gitkeep` and an `vscode/README.md` describing the
planned extension scope.

### 11. Update insurance_postgres to remove moved files

Back in `insurance_data_ecosystem`, on a new branch:

```bash
git rm schema_inference/ -r
git rm ground_truth/ -r
git rm scripts/score_mappings.py
git rm tools/tune_rule_weights.py tools/tune_prompts.py \
       tools/curate_few_shot_bank.py tools/generate_schema_drift_variant.py
git rm test_contest.py test_dedup.py test_mapper_rowshape.py \
       test_orchestrator_rowshape.py test_rowshape.py test_rowshape_score.py
git rm requirements-schema-inference.txt
git rm docs/mapper-agent-roadmap.md docs/self-tuning-mapper-agent-plan.md \
       docs/mapping-agent-batching-plan.md docs/repo-split-schema-inference.md
```

Leave a `docs/schema-inference.md` stub pointing to the new repo URL so
existing links don't 404.

Update `.gitignore` to remove the `schema_inference/metamodel/metamodel.db`
entry (no longer needed in this repo).

Open a PR against `main` in `insurance_data_ecosystem` — this is a
destructive removal, so it should go through normal review.

### 12. Verify development works end-to-end in the new repo

```bash
cd schema-inference
python -m venv .venv && source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -e .

# Smoke test — rule engine only, no API key needed
python -m schema_inference map schema_inference/test_data/pasl_policy.dat \
  --source-name pasl

# Scoring
python scripts/score_mappings.py \
  schema_inference/registry/pasl/proposal_pasl_snowflake.json \
  --source-name pasl

# Layer 0 tuning (no API key needed)
python tools/tune_rule_weights.py --source-name pasl

# Full agent run (needs ANTHROPIC_API_KEY)
python -m schema_inference map schema_inference/test_data/pasl_policy.dat \
  --source-name pasl --agent --eval
```

---

## After the split: first tasks in the new repo

1. **Per-source rule weights** — redesign `agent_config.yml` to support
   `rule_engine.weights_by_source` keyed by source name, with a global
   fallback. Update `tune_rule_weights.py` `--apply` to write to the
   source-specific key.
2. **MAP-7 design spike** — VS Code extension architecture document in
   `vscode/DESIGN.md`.
3. **Live Layer 2 run on PAS-M** — first real end-to-end self-tuning
   demonstration with an API key.
4. **PAS-M table coverage** — extend `ground_truth/pasm_schema_catalog.yml`
   to cover `pasm_coverage`, `pasm_premium_register`, `pasm_transaction_log`,
   `pasm_risk_object` (separate catalog files, one per table, to avoid
   cross-table column-name collisions).
