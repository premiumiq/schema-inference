# VS Code Extension — Design (MAP-7)

Design spike complete — full architecture, bridge protocol, refactor
requirements, failure modes, and build order at
[`docs/map-7-vscode-extension-design.md`](../docs/map-7-vscode-extension-design.md).

Build order progress (see design doc §6 for detail):
1. `schema_inference/reviewer.py` refactor — done.
2. `schema_inference/bridge.py` (JSON-RPC server) — done.
3. Extension shell (this directory) — done: activation, bridge process
   lifecycle (`src/bridgeClient.ts`), hover provider + inline annotations
   (`src/extension.ts`). `npm install && npm run compile` to build; open
   this folder in VS Code and press F5 for an Extension Development Host
   to try it against a real workspace. Manually verified end to end
   against `pasl_policy.dat` (activation, bridge spawn, profile+map,
   header-row hover cards).
4. Review panel (webview, `src/reviewPanel.ts`) — done: accept/modify/skip
   per column, missing-field and contested-mapping resolution, finalize.
   Manually verified end to end in the same Extension Development Host run
   — full review completed and `MappingDefinition` JSON written to disk.
5. Contested-mapping polish (per-column confidence, provisional-winner
   default) + row-shape section (MAP-5 natural key / dedup strategy) in
   the review panel — done.
6. Mapping health sidebar (`src/healthSidebar.ts`) — done: native
   `TreeDataProvider` under the Explorer view container, backed by
   `metamodel.query_loss_runs`. Refresh via the view-title button or
   "Schema Inference: Refresh Mapping Health".
7. dbt staging model scaffolding — done: `schema_inference/sql_scaffold.py`
   generates a `stg_{source}_{table}.sql` stub from a finalized
   `MappingDefinition`; `sql.generate_staging_model` bridge call and a
   "Generate dbt Staging Model" button in the review panel (enabled after
   Finalize) never overwrite an existing file without an explicit modal
   confirm.

All seven build-order steps from the design doc are now implemented.
Remaining before this is more than a spike: a full live Extension Host
pass covering everything (hover, review panel, health sidebar, dbt
scaffolding including the overwrite-confirm dialog), and the open
questions in the design doc sec 7 that haven't come up in practice yet
(packaging/distribution, multiple bridge processes).

## Planned scope

- Inline column mapping annotations in source files (hover: target field, confidence, method, verdict)
- Accept/reject review panel per column (diff-style, same UX as a PR review thread)
- dbt staging model scaffolding: pre-filled `CAST`/`COALESCE`/`NULLIF` stubs per mapped column
- Unmapped columns flagged as diagnostics (red squiggles in `.sql` files)
- Contested mapping panel (MAP-3 `contested_mappings` for human resolution in-editor)
- Row-shape display (MAP-5 inferred dedup key/strategy alongside column mappings)
- Mapping health sidebar: F1, hard-F1, mean loss tiles from metamodel `loss_runs`

## Architecture

Extension (`vscode/`) consumes the `schema_inference` Python package as a subprocess
via a lightweight JSON-RPC bridge. See `docs/mapper-agent-roadmap.md` MAP-7 for full scope.
