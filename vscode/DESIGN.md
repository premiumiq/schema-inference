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

All seven build-order steps from the design doc are implemented. A
follow-up "demo-ready" pass (see `docs/map-7-vscode-extension-design.md`'s
build-order section for detail) fixed a real bug (the review panel was a
hard singleton — reviewing a second file while one panel was open
silently revealed the wrong file's stale session), added stale-proposal
detection (`tracker.check` + a file watcher), wired the LLM-assisted agent
pipeline into the extension with live per-stage progress, added unmapped-
column diagnostics on generated staging models, grouped the health
sidebar by table, and packaged the extension as an installable `.vsix`
(`esbuild`, `.vscodeignore`, icon/README/CHANGELOG — confirmed installable
via `vsce package` + `code --install-extension`).

**Self-tuning panel** (`src/tuningPanel.ts`) — done: surfaces
`schema_inference`'s three self-tuning layers (Layer 0 rule-weight grid
search, Layer 1 few-shot bank curation, Layer 2 LLM prompt tuning) that
were previously CLI-only. Required prerequisite refactors on the Python
side (`tools/tune_rule_weights.py`'s `run_layer0_tuning()`,
`tools/tune_prompts.py`'s `on_round` progress hook) mirroring the
reviewer.py-refactor pattern, plus 8 new `tuning.*` bridge methods. Found
and fixed two real bugs along the way: `curate_few_shot_bank.py`'s
`GROUND_TRUTH_DIR` pointed at a directory that has never existed in this
repo, silently zeroing out the `hard_tp` curation origin in every real run
of that tool; and `mapper._rule_weights`'s `@lru_cache` would have served
stale weights for the rest of a bridge session after an in-panel Layer 0
apply. Layer 2's Accept action (the one with real teeth — it changes agent
behavior for every future run) is gated behind a modal confirm, same as
`review.finalize`/`sql.generate_staging_model`'s overwrite guard.

The `tuning.progress` notification plumbing (identical `dispatch()`/
`_notify_sink`/`serve()` mechanism `map.progress` already proved live) is
unit-tested with a stubbed session, but a real end-to-end Layer 2 session
was started live and stopped after ~12 minutes without even finishing the
*baseline* scoring step — a real session runs the full agent orchestrator
multiple times per round (baseline holdout, per-round train, per-round
holdout-validate, plus a 3x determinism check on any improving candidate),
all serialized through the same 5 RPM process-wide throttle as everything
else. This is real, useful information, not a problem to fix: even
`max_rounds=1` is a genuinely multi-minute-to-tens-of-minutes operation in
practice, more than the original estimate assumed. Worth surfacing to
whoever clicks "Run tuning session" for the first time.

**Snowflake as a source, and as a live mapping target** — done, two
independent additions surfaced by the same manual-verification pass:
`schema_inference/snowflake_reader.py`'s `profile_snowflake_table()` had
zero callers anywhere in the repo (Snowflake profiling never actually
worked, CLI or extension, despite `.env.example`/`CLAUDE.md` describing
it) — now wired via `profile.run_snowflake` and a
"Profile Snowflake Table" command that skips the file-hover/stale-watch
path entirely (no file exists for a live table) and goes straight to the
review panel. Separately, `canonical/registry.py` gained
`register_dynamic_schema()` so a real Snowflake table's own schema (e.g.
a warehouse silver table) can become a live mapping target for the
current bridge session — introspected via a new
`describe_target_table()`/`extract_canonical_fields()` pair in
`snowflake_reader.py`, previewed in an editable JSON tab
("Extract Target Schema from Snowflake Table"), then registered via
`canonical.register_dynamic_schema`. Deliberately in-memory/session-only,
not a draft `.py` file to commit — considered and explicitly declined for
this feature, unlike the project's more common "generate a draft, human
commits" pattern (dbt scaffolding, few-shot bank, prompt tuning). No
aliases get extracted (nothing to infer domain synonyms from a bare
column list), so rule-pass recall against an extracted schema is
materially worse than the hand-curated ones until a human adds some in
the review tab — real tradeoff, not silently patched over.

Neither Snowflake path could be verified end to end from a coding session
— no reachable Snowflake instance/credentials there. Needs a real
click-through on a machine with real access (see
`vscode/MANUAL_VERIFICATION.md` §10).

Remaining before any of this is more than a spike: a full live Extension
Host pass covering everything (hover, review panel, health sidebar, dbt
scaffolding, self-tuning panel including a real Layer 2 session end to
end, and now both Snowflake paths), and the open questions in the design
doc sec 7 that haven't come up in practice yet (packaging/distribution
beyond the internal `.vsix`, multiple bridge processes).

## Planned scope

- Inline column mapping annotations in source files (hover: target field, confidence, method, verdict)
- Accept/reject review panel per column (diff-style, same UX as a PR review thread)
- dbt staging model scaffolding: pre-filled `CAST`/`COALESCE`/`NULLIF` stubs per mapped column
- Unmapped columns flagged as diagnostics (red squiggles in `.sql` files)
- Contested mapping panel (MAP-3 `contested_mappings` for human resolution in-editor)
- Row-shape display (MAP-5 inferred dedup key/strategy alongside column mappings)
- Mapping health sidebar: F1, hard-F1, mean loss tiles from metamodel `loss_runs`
- Self-tuning panel: Layer 0/1/2 insight + triggers (rule-weight grid
  search, few-shot bank curation, LLM prompt tuning with a human accept gate)

## Architecture

Extension (`vscode/`) consumes the `schema_inference` Python package as a subprocess
via a lightweight JSON-RPC bridge. See `docs/mapper-agent-roadmap.md` MAP-7 for full scope.
