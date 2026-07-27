# VS Code Extension — Design (MAP-7)

Design spike complete — full architecture, bridge protocol, refactor
requirements, failure modes, and build order at
[`docs/map-7-vscode-extension-design.md`](../docs/map-7-vscode-extension-design.md).
Extension code will live in this directory once initial implementation
starts (build order step 1 is a `schema_inference/reviewer.py` refactor,
not extension code — see design doc §3.3 and §6).

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
