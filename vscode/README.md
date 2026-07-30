# Schema Inference (VS Code extension)

Inline column-mapping annotations, an accept/reject review panel, and dbt
staging-model scaffolding for the [`schema_inference`](../README.md)
pipeline — driven by a JSON-RPC bridge to the Python tool, not a
reimplementation of it.

This is an internal PremiumIQ tool, sideloaded as a `.vsix`, not published
to the public Marketplace.

## Requirements

- A Python environment with `schema_inference` installed
  (`pip install -e ".[dev]"` from the repo root — see the root
  [README](../README.md) / `CLAUDE.md` for setup).
- The workspace you open in VS Code must be (or contain) this repo, so the
  bridge process (`python -m schema_inference.bridge`) can import
  `schema_inference`.
- If your interpreter isn't on `PATH` as `python`/`python3`, set
  `schemaInference.pythonPath` (Settings → search "Schema Inference") to
  your venv's interpreter, e.g. `.venv/Scripts/python.exe` on Windows.
- For the two Snowflake commands: `SNOWFLAKE_ACCOUNT`/`_USER`/
  `_PRIVATE_KEY_PATH`/`_WAREHOUSE`/`_ROLE`/`_DATABASE` must be set as real
  process environment variables in whatever process VS Code itself runs
  in — **not** just present in a `.env` file. Nothing in this extension or
  the Python pipeline loads `.env` (same as `ANTHROPIC_API_KEY`); the
  bridge subprocess only inherits the environment VS Code was launched
  with.

## Usage

1. Open a `.dat`/`.csv` source file (e.g.
   `examples/insurance/test_data/pasl_policy.dat`).
2. Run **Schema Inference: Profile & Map Current File** (Command Palette).
   Enter a logical source name if prompted (`pasl`, `pasm`, ...), then pick
   **Rule-only (fast)** or **Agent pipeline** (LLM-assisted, needs
   `ANTHROPIC_API_KEY` in the bridge process's environment).
3. Hover any column in the file's header row to see its proposed target
   field, confidence, method, and notes.
4. Run **Schema Inference: Open Review Panel** to accept/modify/skip each
   column, resolve missing required fields and MAP-3 contested mappings,
   and review the MAP-5 row-shape inference.
5. Click **Finalize review** to write the approved `MappingDefinition`
   JSON, then **Generate dbt Staging Model** to scaffold a `stg_*.sql`
   stub — genuinely unmapped columns are flagged as warnings in the
   generated file.
6. The **Schema Inference: Mapping Health** view (Explorer sidebar) shows
   past scoring runs (F1, hard-F1, mean loss, or whatever else got
   recorded) grouped by table, from the metamodel store.
7. Run **Schema Inference: Open Self-Tuning Panel** for insight into and
   control over the pipeline's self-tuning layers: grid-search rule
   weights (Layer 0, with a direct apply), curate the few-shot example
   bank (Layer 1), and run/review/accept LLM prompt-tuning sessions
   (Layer 2 — accepting a candidate changes agent behavior for every
   future run, so it's gated behind a confirm).
8. Instead of a `.dat`/`.csv` file, run **Schema Inference: Profile
   Snowflake Table**, enter `DATABASE.SCHEMA.TABLE`, and pick a mapping
   mode as in step 2 — there's no file to hover over or watch for a live
   table, so this goes straight to the review panel once mapping finishes.
9. To map against a live table's own schema instead of the built-in
   `policy`/`pasm_coverage` targets, run **Schema Inference: Extract
   Target Schema from Snowflake Table**, enter the *target* table (e.g. a
   warehouse silver table), review/edit the extracted field list in the
   JSON tab it opens (add aliases — none are extracted automatically, and
   the rule engine's fuzzy-match recall depends heavily on them), then
   name the source `table_name`(s) to register it against. This only
   lasts for the current bridge session — restarting the bridge loses it.

If the source file changes on disk after profiling, hover and the review
panel both surface a "profile out of date" banner instead of silently
showing stale mappings.

## Known limitations

- Python interpreter resolution is a `PATH` guess plus a manual
  file-picker fallback, not full `ms-python` Environments API integration.
- One bridge process per workspace; not tested with multiple VS Code
  windows on the same workspace simultaneously.
- Diagnostics only cover unmapped columns in a *generated* staging model,
  not arbitrary hand-written SQL.
- A Snowflake-extracted target schema has no aliases (there's no source
  to infer domain synonyms from a bare column list) and is never
  persisted — it's registered in-memory for the current bridge process
  only. Expect materially worse rule-pass recall against it than the
  hand-curated `policy`/`pasm_coverage` schemas until a human adds
  aliases in the review tab.

See `docs/map-7-vscode-extension-design.md` in the repo root for the full
design rationale and build-order history.
