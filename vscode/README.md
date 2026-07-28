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

See `docs/map-7-vscode-extension-design.md` in the repo root for the full
design rationale and build-order history.
