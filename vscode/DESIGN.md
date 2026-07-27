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
   to try it against a real workspace.
4. Review panel (webview) — next.

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
