# MAP-7 — VS Code Extension: Design Spike

Design spike for surfacing the schema inference pipeline inside VS Code.
Status: design only — no extension code written yet. This doc is the
prerequisite named in `mapper-agent-roadmap.md`'s MAP-7 entry ("design spike
is the next task").

---

## 1. Problem statement

Today the tool is CLI-only: `profile` → `map` → `review` → `track`, each a
separate process, state handed off as JSON files on disk
(`registry/{source}/profile_{table}.json`, then a `MappingProposal` JSON,
then a `MappingDefinition` JSON). Two audiences are blocked by this:

- **Non-Python-fluent data engineers / client SMEs** who need to review and
  approve mappings but shouldn't need a Python venv or CLI fluency.
- **Engineers who already live in VS Code with the dbt project open** —
  today they run the CLI, eyeball JSON, then hand-edit a staging model SQL
  file in a separate window. No link between the mapping decision and the
  SQL it produces.

Goal: collapse that into one editor surface — run mapping, review/accept
columns, see the generated SQL, all without leaving VS Code, while keeping
the actual inference logic in Python (no port to TypeScript).

---

## 2. Scope (from roadmap, restated as concrete deliverables)

| # | Feature | Backing data |
|---|---|---|
| 1 | Inline column annotations (hover: target, confidence, method, verdict) | `ColumnMapping` |
| 2 | Accept/reject review panel (PR-review-thread UX) | `MappingProposal` → `reviewer.py` phases |
| 3 | dbt staging model scaffolding (`CAST`/`COALESCE`/`NULLIF` stubs) | `ColumnMapping.sql_expression` |
| 4 | Mapping health sidebar (F1, hard-F1, mean loss tiles) | `loss_runs` table |
| 5 | Contested mapping panel | `MappingProposal.contested_mappings` |
| 6 | Row-shape display (dedup key/strategy) | `MappingProposal.row_shape` |

Explicitly **out of scope** for v1 (revisit after initial implementation
lands, per roadmap sequencing):

- Editing rule weights / `agent_config.yml` from the UI (Layer 0/2 tuning
  stays CLI — `tools/tune_rule_weights.py`, `tools/tune_prompts.py`).
- Prompt-version accept/reject UI (Layer 2's human gate stays CLI —
  deliberate: `tune_prompts.py` never self-promotes, and adding a second,
  easier-to-misclick accept path undermines that guardrail).
- Live Snowflake browsing inside the extension (profiling against
  `--snowflake` stays CLI-triggered; the extension consumes whatever
  profile JSON already exists in `registry/`).
- MAP-6 (table-level mapping) — not built yet, nothing to surface.

---

## 3. Architecture

### 3.1 Process model — JSON-RPC over stdio, not LSP

Roadmap text names both "Language Server Protocol or a lightweight JSON-RPC
bridge" as options. Recommendation: **plain JSON-RPC 2.0 over stdio, not
LSP.**

Why not LSP: LSP's message shapes (`textDocument/hover`,
`textDocument/publishDiagnostics`, `textDocument/codeLens`) fit exactly one
of the six features (diagnostics for unmapped columns). Contested-mapping
panels, health-sidebar tiles, and accept/reject actions have no natural LSP
message — they'd all get bolted on as custom `$/`-prefixed extensions
anyway, at which point LSP's ceremony (capability negotiation, document
sync, position-encoding negotiation) buys nothing over a bridge we own
outright. A language server is the right call when the core deliverable
*is* hover/diagnostics/completion over a text buffer; here that's one
feature out of six.

Why a custom bridge, not raw CLI subprocess-per-call: `map --agent` on the
5-agent pipeline takes real wall-clock time (LLM calls, throttled by
`agents/throttle.py`'s RPM cap) — a long-lived process avoids re-importing
`schema_inference` and re-establishing agent state per request, and lets the
extension show incremental progress (per-column agent trace) instead of
blocking on total completion.

```
┌─────────────────────────────┐        stdio (JSON-RPC 2.0)       ┌──────────────────────────────┐
│  VS Code extension (TS)     │ ───────────────────────────────▶  │  schema_inference bridge (Py) │
│  vscode/src/extension.ts    │ ◀───────────────────────────────  │  schema_inference/bridge.py   │
│  - webview panels           │   requests / responses /          │  - JSON-RPC server loop        │
│  - hover provider           │   notifications (progress,        │  - wraps profiler/mapper/      │
│  - diagnostics collection   │   traces)                         │    reviewer/tracker/metamodel  │
└─────────────────────────────┘                                    └──────────────────────────────┘
                                                                              │
                                                                              ▼
                                                                    registry/, metamodel.db,
                                                                    canonical/registry.py
                                                                    (same files the CLI uses —
                                                                     no separate state store)
```

The bridge process is spawned once per VS Code workspace session (activation
event: workspace contains `schema_inference/` or a `.schema-inference`
marker — see §3.4), reused for all requests, and torn down on window close.
It shares the *same* `registry/` directory, `metamodel.db`, and
`agent_config.yml` the CLI reads/writes — the extension is a second client
of existing state, not a parallel store. This matters directly for MAP-1's
metamodel design goal (single source of history) and for tracker.py's
schema-version continuity: whichever a user runs first, CLI or extension,
the other sees consistent state next time.

### 3.2 Bridge module (`schema_inference/bridge.py`, new)

New file, added to the existing package — not a new top-level package,
mirroring how `reviewer.py`/`tracker.py` already sit alongside `mapper.py`.
Responsibilities:

- Read JSON-RPC requests from stdin, one per line (newline-delimited, not
  `Content-Length`-framed — simpler to parse from the TS side with a
  readline stream, and we don't need LSP's binary-safety guarantees since
  payloads are all UTF-8 JSON).
- Dispatch to thin wrapper functions around existing pure functions
  (`profiler.profile_file`, `mapper.map_table`, `agents.orchestrator.run_mapping`,
  `reviewer.*_phase_*` helpers refactored to be callable without
  `input()` — see §3.3 caveat below —, `tracker.record_or_compare`,
  `metamodel.store.open_store`).
- Emit **notifications** (no response expected) for progress during
  long-running calls: one per `AgentTrace` as agents complete columns,
  so the review panel can render "3/12 columns done" instead of a spinner.
- Never hold pipeline logic itself — this is intentionally a thin
  translation layer, same spirit as `__main__.py`'s existing `_cmd_*`
  wrappers. Anything computed here that isn't wire-format marshaling is a
  sign it belongs in a `schema_inference/*.py` module instead, callable
  from both CLI and bridge.

Example request/response shape (illustrative, not final wire spec):

```jsonc
// → request
{"jsonrpc": "2.0", "id": 7, "method": "map.run",
 "params": {"profile_path": "schema_inference/registry/pasm/profile_pasm_policy.json",
            "table_name": "pasm_policy", "agent": true, "eval": true}}

// ← notification (repeated, one per column as agents finish)
{"jsonrpc": "2.0", "method": "map.progress",
 "params": {"column": "REGN_CD", "status": "critic_resolved", "target_field": "region_code"}}

// ← response
{"jsonrpc": "2.0", "id": 7, "result": {"proposal": { /* MappingProposal */ },
                                        "run": { /* AgentMappingRun minus proposal, avoid dup */ }}}
```

### 3.3 Reviewer refactor required (blocking, not optional)

`reviewer.py`'s `_phase_review` / `_phase_contested_mappings` /
`_phase_missing_fields` / `_prompt_action` are written for a synchronous
terminal loop (`input()` calls, `_display_column_panel` prints). The
extension's accept/reject panel needs those same decisions to arrive as
discrete RPC calls (`review.accept_column`, `review.reject_column`,
`review.resolve_contest`), one per user click, arbitrarily interleaved with
the user reviewing other columns in any order — not a blocking linear scan.

This means MAP-7's *first implementation PR* is not the extension shell —
it's extracting `reviewer.py`'s per-column decision logic (what happens to
a `ColumnMapping` + a reviewer action → `ApprovedMapping`, and the
metamodel write in `_record_review_to_metamodel`) into functions that take
an explicit action parameter instead of calling `input()`, with the
existing CLI phases becoming thin loops over that same function. This is a
refactor of existing code, not new surface — call out explicitly in the
spike so it isn't discovered mid-build. `auto_review_proposal` already
proves the decision logic doesn't *need* to be interactive (it threshold-
decides instead of prompting), so the extraction target already has a
working non-interactive precedent to generalize from.

### 3.4 Extension activation & workspace detection

Activation event: `workspaceContains:**/schema_inference/agent_config.yml`
— narrow and specific, avoids activating in unrelated Python repos that
happen to have a `schema_inference` folder name collision. Extension reads
`agent_config.yml`'s path relative to workspace root to find the repo root,
then spawns the bridge as `python -m schema_inference.bridge` using
whichever Python interpreter the Python extension (if installed) reports
for the workspace, falling back to `python3`/`python` on PATH with a
one-time "select interpreter" prompt if the import fails (mirrors how the
Python extension itself resolves interpreters — don't invent a second
mechanism).

### 3.5 Data flow per feature

- **Inline annotations (#1):** extension reads the latest
  `MappingProposal` JSON for the open `.dat`/`.csv` file (matched by
  `source_name`/`table_name` from the file path, same convention the CLI's
  `registry/{source}/profile_{table}.json` naming already uses) and
  registers a `HoverProvider` keyed on column position (parsed from the
  file's header row / delimiter, reusing `profiler.py`'s delimiter
  detection so the extension doesn't reimplement parsing — bridge exposes
  a `profile.peek_columns` call for this, distinct from a full
  `profile.run`, so hovering doesn't trigger a full re-profile).
- **Review panel (#2):** webview, one row per `ColumnMapping`, actions
  wired to the `review.*` RPC calls from §3.3. Confidence tiers reuse
  `reviewer.py`'s existing tiering logic (`_fmt_confidence`) so the UI's
  color bands match what the CLI already prints — don't invent a second
  threshold scheme.
- **dbt scaffolding (#3):** `sql.generate_staging_model` bridge call wraps
  per-column `ColumnMapping.sql_expression` values into a `SELECT` stub
  matching the shape of existing `stg_pas*_*.sql` models (same `CAST`
  conventions) — writes to a new file, never overwrites an existing
  staging model silently (confirm-before-overwrite in the UI, same
  destructive-action caution as the CLI's `--force-accept-breaking` flag
  on `track`).
- **Health sidebar (#4):** `metamodel.query_loss_runs` bridge call reads
  `loss_runs` filtered by `source_name`, renders latest `metrics_json`
  (has `mean_loss`, `f1`, `hard_f1`, `sql_correctness_rate` per
  `AggregateMetrics`) as sidebar tiles, refreshed on `map.run` completion
  notification.
- **Contested panel (#5):** reads `MappingProposal.contested_mappings`
  directly (already a `list[dict]` with `target_field` +
  `source_columns`), no new bridge call needed beyond what `map.run`
  already returns.
- **Row-shape display (#6):** reads `MappingProposal.row_shape`
  (`RowShapeProposal.model_dump()`) directly, same as #5 — no separate
  call.

---

## 4. Multi-schema awareness (MAP-4.1 dependency, concrete)

Every panel that shows a target field must resolve through
`canonical/registry.py`'s `schema_for_table(table_name)` rather than
assuming the single `policy` schema — this was the literal blocker MAP-4.1
closed. Reference tables for the design: `pasl_policy` (schema `policy`),
`pasm_policy` (schema `policy`), `pasm_coverage` (schema `pasm_coverage`).
Concretely:

- The health sidebar aggregates `loss_runs` per `source_name` *and*
  `table_name` — a `pasm` workspace with both `pasm_policy` and
  `pasm_coverage` profiled needs two tiles, not one, since they map to
  different canonical schemas with independently-tracked F1.
- The dbt scaffolding call must pass `table_name` through to resolve
  `schema_for_table` before generating `SELECT` column lists — reusing
  `canonical_by_name`/`canonical_names` the same way `mapper.py`/
  `orchestrator.py` already thread it, not re-deriving field lists in the
  bridge layer.

---

## 5. Failure modes / non-negotiables

- **Bridge crash mid-session:** extension must detect the child process
  exiting and offer a "restart bridge" action rather than silently going
  stale — a stuck webview with no backend is worse than an explicit error.
- **Stale proposal vs. live file:** if the source `.dat`/`.csv` file
  changes on disk after a `MappingProposal` was generated (profile_hash
  mismatch), the inline annotations must show a "profile out of date —
  re-run profile" banner rather than silently showing stale confidence
  scores against columns that may have moved. `tracker.py`'s existing
  `profile_hash`/fingerprint machinery is the exact mechanism to reuse
  here — don't build a second staleness check.
- **Never auto-promote:** the extension must not add any path that accepts
  mappings or prompt versions without an explicit user action — this
  mirrors the existing invariant that `tune_prompts.py` "never
  self-promotes a prompt" and `auto_review_proposal` is explicitly
  documented as "not a substitute for real review." A "review all >0.9
  confidence" bulk-accept button is fine (mirrors `--accept-threshold`);
  a silent auto-apply on file save is not.
- **Rate limiting stays server-side:** if the review panel or a "re-run
  agent on this column" action triggers a live Anthropic call, it must
  still go through `agents/throttle.py`'s shared pacer — the bridge is a
  second process attached to the same account-wide RPM cap the CLI
  already respects, so it cannot bypass the throttle by virtue of being a
  different entry point.

---

## 6. Suggested build order (post-spike)

1. **Reviewer refactor** (§3.3) — **done.** Extracted
   `accept_mapping`/`modify_mapping`/`skip_mapping`/`resolve_missing_field`/
   `apply_contest_resolution`/`assign_extended_attr` as pure, no-I/O
   functions in `reviewer.py` (each takes an already-decided action, returns
   the resulting `ApprovedMapping`/`MissingFieldResolution`, or mutates the
   approved-list in place for contest resolution). `_prompt_action` and the
   `_phase_*` CLI loops are now thin wrappers: gather input via
   `Prompt.ask`, then call the same function an RPC handler would call
   later. `UnknownTargetFieldError` replaces the old inline
   `console.print` + `continue` retry so a non-CLI caller (bridge) gets a
   catchable error instead of a printed message. Parity proven by
   `tests/test_reviewer_actions.py` (13 new tests) plus a full
   `pytest tests/ -v -m "not snowflake"` run (21 passed, 1 skipped,
   unchanged) and a manual CLI smoke test (`map --no-llm` →
   `review --auto`) on the `pasl_policy` fixture.
   Deliberately *not* extracted: `_phase_auto_approved`'s bulk
   auto-approval (no decision to make — every row gets the same
   deterministic `reviewer_action="auto_approved"`, nothing an RPC caller
   would ever need to call standalone).
2. **`schema_inference/bridge.py`** — **done.** JSON-RPC 2.0 loop, newline-
   delimited over stdio (`dispatch()` for one parsed request, `serve()` for
   the stdin/stdout read loop — split so tests exercise `dispatch()`
   directly without a real subprocess). Methods: `ping`, `profile.run`,
   `profile.load`, `map.run` (both rule-only and `--agent` paths),
   `review.start`/`accept_column`/`modify_column`/`skip_column`/
   `resolve_missing_field`/`resolve_contest`/`assign_extended_attr`/
   `finalize`, `metamodel.query_loss_runs`, `tracker.check`. Every handler
   is a thin wrapper delegating to existing `schema_inference` functions —
   `review.*` in particular calls the exact decision functions extracted
   from `reviewer.py` in step 1, nothing reimplemented.

   The interesting design piece was the review session: one `ReviewSession`
   per in-progress `MappingProposal`, keyed by a server-issued `session_id`,
   holding an `ApprovedMapping` per column as RPC calls decide them one at a
   time in whatever order the reviewer clicks — proving out §3.3's premise
   that column review no longer needs to be a blocking linear scan. The
   ≥0.85 auto-approve tier is seeded into the session immediately (same
   threshold `review_proposal()` already uses — not a new auto-accept
   path). `review.finalize` refuses to run while any column is undecided or
   any contest unresolved, and consumes the session — mirrors the "never
   auto-promote" invariant from §5 at the protocol level, not just in the
   UI layer that doesn't exist yet.

   Deferred, noted in the module docstring: no `map.progress` notifications
   — `agents.orchestrator.run_mapping` has no progress-callback hook to
   drive them yet, so `map.run` with `agent=True` blocks until the table
   finishes rather than streaming per-column traces. Separable follow-up;
   not required to prove the request/response protocol.

   Validated by `tests/test_bridge.py` (8 tests against `dispatch()`
   directly: ping/unknown-method/notification framing, a full
   profile→map→review→finalize walk on the `pasl_policy` CI fixture,
   finalize's pending/contest guardrails, metamodel query, tracker check
   with `REGISTRY_DIR` monkeypatched to `tmp_path` so it can't write into
   the real `registry/` tree, and one `serve()` test over in-memory
   `io.StringIO` streams for the newline-framing itself). Full suite:
   28 passed, 1 skipped (`test_contest.py`'s `@pytest.mark.anthropic` test,
   no API key set) — no regressions.
3. **Extension shell** (`vscode/`) — **done.** TypeScript, compiles clean
   (`npm run compile`, strict mode). `src/bridgeClient.ts` is the JSON-RPC
   client: spawns `{pythonPath} -m schema_inference.bridge`, newline-framed
   request/response correlated by id, rejects in-flight requests and
   surfaces an error notification (`Restart Bridge` / `Select Python
   Interpreter` actions) if the process exits unexpectedly. `src/types.ts`
   hand-mirrors the `models.py` subset the extension touches — no shared
   codegen yet, noted as a reasonable later follow-up once the wire shape
   stabilizes. `src/extension.ts`: activation via the same
   `workspaceContains:**/schema_inference/agent_config.yml` event as §3.4,
   command `Schema Inference: Profile & Map Current File` (runs
   `profile.run` then rule-only `map.run` — agent pipeline deferred until
   `map.progress` notifications exist to show status during a long call),
   and a `HoverProvider` for `**/*.dat`/`**/*.csv` that reads the cached
   `MappingProposal` for the open file, splits the header row (line 0) by
   the profiled delimiter, and shows target/confidence/method/notes for
   the column under the cursor.

   Python interpreter resolution simplified from §3.4's full plan: reads
   `schemaInference.pythonPath` if set, else falls back to `python`/
   `python3` by platform — no ms-python extension-API integration yet.
   `promptForInterpreter()` (file picker → workspace setting → bridge
   restart) is the recovery path when that guess is wrong, satisfying the
   same "select interpreter" requirement without the added API surface.

   Validated two ways: `tsc` strict-mode compile with no errors, and a
   throwaway Node script driving the compiled `BridgeClient` against the
   real `schema_inference.bridge` subprocess (`ping` → `profile.run` →
   `map.run` on the `pasl_policy` CI fixture) — confirmed the TS client's
   newline-JSON framing is wire-compatible with the Python side, not just
   type-correct in isolation. Since confirmed live in a real Extension
   Development Host too (see step 4's note) — activation, bridge spawn,
   `Profile & Map Current File`, and header-row hover cards all work
   end to end against `pasl_policy.dat`.
4. **Review panel** (#2) — **done.** `vscode/src/reviewPanel.ts`: a
   `WebviewPanel` (`ReviewPanel.createOrShow`, singleton — reveals the
   existing panel rather than opening a second one) driven entirely by the
   `review.*` methods built in step 2. Columns render grouped into the same
   three tiers `reviewer.py`'s `_fmt_confidence`/`review_proposal()` already
   use (>=85% auto, 50-84% flagged, <50% low) — not a second threshold
   scheme. Each row has Accept / Skip / Modify (inline target field, SQL
   expression, notes); missing-required-field and contested-mapping
   sections resolve via the same `review.resolve_missing_field` /
   `review.resolve_contest` calls; a Finalize button calls
   `review.finalize` and surfaces the written `MappingDefinition` path or,
   if columns are still pending or a contest unresolved, the bridge's
   rejection message verbatim.

   `extension.ts`'s `profileAndMapCurrentFile` now also passes `map.run` an
   `output` path (`registry/{source}/proposal_{table}.json`, alongside the
   existing `profile_{table}.json` convention) so `review.start` has a
   stable path to reload — needed once review state has to survive past a
   single in-memory command run. New command: `Schema Inference: Open
   Review Panel`.

   Design choice worth flagging: the panel keeps its own `decisions` map to
   render already-decided rows without a status round-trip after every
   click, including replaying `apply_contest_resolution`'s winner/loser
   logic client-side for display. The bridge's `ReviewSession` is still the
   only thing `review.finalize` actually reads from — if the panel's local
   mirror ever drifted from the server, only the *display* would be wrong,
   never what gets written to disk.

   Verified: `tsc --strict` compiles clean with the new file wired in
   (`out/reviewPanel.js` present), and manually end to end in a live VS
   Code Extension Development Host against `pasl_policy.dat` -- opened the
   panel, worked through accept/modify/skip across the confidence tiers,
   resolved missing fields and contested mappings, clicked Finalize, and
   confirmed the `MappingDefinition` JSON was written to disk. Webview
   CSP/message-passing behavior (the part `tsc` and the Python-side tests
   can't reach) is now confirmed working, not just plausible from code
   review.

   Not yet exercised: the crash-recovery path (kill the bridge process
   externally, confirm the "Restart Bridge" / "Select Python Interpreter"
   notification appears) and the Debug Console for stray exceptions during
   the above run. Worth a quick pass before step 5, not a blocker.
5. **Contested + row-shape panels** (#5, #6) — **done.** No new bridge
   calls needed, as anticipated — pure UI work on top of #4's webview.
   Contested-mappings section (already present as a basic table since step
   4) now shows each competing column's confidence inline and in the
   winner dropdown, and pre-selects `provisional_winner` (always
   `competing_columns[0]`, since `mapper._deduplicate` sorts competitors
   by confidence descending before recording a contest) rather than
   defaulting to blank. New row-shape section renders the MAP-5
   `RowShapeProposal` returned by `review.start` (already wired through the
   bridge, just not displayed before): natural key, recency column, dedup
   strategy, confidence badge (same tier coloring as columns), reasoning,
   and the dedup SQL predicate if present. `vscode/src/types.ts` gained a
   proper `RowShapeProposal` interface (was a loose `Record<string,
   unknown>` before) and `provisional_winner` on the contested-mapping
   type. `tsc --strict` clean; Python suite unaffected (no `.py` changes
   this step). Not re-verified in a live Extension Host after this change
   — worth a quick look next session before relying on it, same as any
   UI-only diff.
6. **Health sidebar** (#4) — **done.** Per §7 open question 1's accepted
   recommendation, a native `TreeDataProvider`
   (`vscode/src/healthSidebar.ts`), not a webview — tiles/numbers with no
   interaction beyond refresh didn't warrant custom layout control. Root
   nodes are `loss_runs` rows (table name + timestamp, `run_id` as the
   description, config snapshot as a JSON tooltip); expanding one lists
   its `metrics` dict as leaf nodes. Deliberately *not* hardcoded to
   F1/hard-F1/mean-loss: a real check against the local `metamodel.db`
   found `tools/tune_rule_weights.py`'s Layer 0 runs record only
   `mean_loss_before`/`mean_loss_after`, while `scripts/
   score_mappings.py`'s full `AggregateMetrics` dict has 17 keys including
   `f1`/`hard_f1` — the shape isn't fixed across producers, so the sidebar
   renders whatever keys a given row actually has rather than assuming
   one schema. Registered under the built-in Explorer view container
   (`contributes.views.explorer`) rather than a new Activity Bar
   container, to avoid needing a bundled SVG icon asset for a design-spike-
   grade sidebar.

   Source name comes from a module-level `lastSourceName` in
   `extension.ts`, set whenever `profileAndMapCurrentFile` resolves one
   (from settings or the input box) — the sidebar has no other way to
   know which source to query, since a typed-in source name was never
   persisted anywhere before this.

   Verified: `tsc --strict` clean, `package.json`'s JSON parses, and a
   Node harness driving the compiled `BridgeClient` confirmed
   `metamodel.query_loss_runs` against the real (gitignored, local)
   `metamodel.db` returns exactly the shape the provider expects for
   `pasl` (1 run, Layer 0 metrics), `pasm` (2 runs), and a nonexistent
   source (`metamodel_available: true`, empty `loss_runs`, no error). Not
   yet re-verified in a live Extension Host after this change.
7. **dbt scaffolding** (#3) — last, since it's the only feature that
   writes new files into the (separate) warehouse repo's territory and
   needs the most care around overwrite confirmation.

Each step should land as its own PR against `vscode/` + the corresponding
`schema_inference/` refactor, in that order — this also gives the "initial
implementation" half of the roadmap's "design spike + initial
implementation" milestone a natural stopping point at step 3 or 4 if MAP-7
needs to pause for the postponed PAS-M table work to resume.

---

## 7. Open questions for review before implementation starts

1. **Webview vs. native tree views for the health sidebar / contested
   panel** — webview gives full control over layout (needed for diff-style
   review UX) but costs more implementation time than VS Code's built-in
   `TreeDataProvider`. Recommendation: webview for the review panel (needs
   custom interaction), native `TreeView` for the health sidebar (just
   tiles/numbers, no interaction beyond refresh).
2. **Packaging/distribution** — internal-only via `.vsix` sideload
   (`code --install-extension`), or publish to the VS Code Marketplace /
   an internal registry? Affects whether the bridge needs to vendor its
   own Python or can assume a dev's existing `.venv`. Recommendation for
   v1: sideload `.vsix` only, require the workspace's `.venv` (same one
   `pip install -e ".[dev]"` already sets up) — matches this repo's
   existing "no packaging infra yet" state and avoids solving Python
   environment bundling before the UI is even proven useful.
3. **Multi-root workspaces** — does one VS Code window ever need two
   bridge processes (e.g., this repo open alongside the warehouse repo)?
   Assume no for v1 — one bridge per workspace root containing
   `schema_inference/`.
