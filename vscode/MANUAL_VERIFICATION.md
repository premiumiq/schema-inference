# Manual verification checklist

Full click-through checklist for the schema_inference VS Code extension —
covers the seven build-order steps (design doc) plus the demo-ready fixes
and the self-tuning panel. Nothing here is automatable from a coding
session; it needs a real VS Code window.

## 0. Setup

1. Terminal, repo root. Git Bash:
   ```bash
   source .venv/Scripts/activate
   python -m pip install -e ".[dev]"
   ```
   (If `pip`/`python` aren't found post-activation, call the venv's
   interpreter directly: `.venv/Scripts/python.exe -m pip install -e ".[dev]"`.
   If that reports "No module named pip", bootstrap it first:
   `python -m ensurepip --upgrade`.)
2. `.env` has `ANTHROPIC_API_KEY` set (needed for §4 and §8's Layer 2 —
   skip those specific steps if untestable).
3. `cd vscode && npm install && npm run compile` — confirm clean, no errors.
4. Open `vscode/` **itself** as the workspace root in VS Code (File → Open
   Folder → the `vscode` subfolder, not the repo root — needed so VS Code
   sees `vscode/package.json`'s `engines.vscode` field and offers the "Run
   Extension" F5 debug config). Or from Git Bash: `code vscode/`.
5. Press F5 → "Run Extension" → launches an Extension Development Host
   window.
6. In that **new** window, open the repo root (`schema-inference/`) as its
   workspace folder. This is what triggers activation
   (`workspaceContains:**/schema_inference/agent_config.yml`).
7. Settings (`Ctrl+,`) → search "Schema Inference" → set
   `schemaInference.pythonPath` to `.venv/Scripts/python.exe` if
   `python`/`python3` isn't your interpreter on PATH.

## 1. Hover + basic mapping

8. Open `examples/insurance/test_data/pasl_policy.dat`.
9. `Ctrl+Shift+P` → **Schema Inference: Profile & Map Current File** →
   source name `pasl` → pick **Rule-only (fast)**.
10. Confirm "N/46 columns mapped" notification.
11. Hover a header column (**line 1 only** — the hover provider doesn't
    respond on data rows) → confirm target/confidence/method/notes card.

## 2. Review panel + per-file isolation

12. **Schema Inference: Open Review Panel** → confirm it opens, tiers
    render, progress counter shows.
13. Open `examples/insurance/test_data/pasm_policy.dat`. Repeat steps 9–12
    for it (source name `pasm`).
14. With both files' panels open, switch between the two review-panel
    tabs → confirm each shows its own file's columns, not a stale reveal
    of the other (this was a real singleton bug, fixed — verify the fix
    holds).

## 3. Stale-proposal detection

Two things must both be true before this triggers, and one edit-type
constraint:

- The stale-detection cache is **in-memory per session** — you must have
  run Profile & Map on the exact file **in this Extension Development Host
  session** before editing it. Reloading the window or restarting
  debugging clears it silently (no error shown, the watcher just no-ops).
- `tracker.check` diffs **column-level fingerprints** (column list,
  inferred type per column) — not data values. Editing one cell's value
  in place (e.g. appending a digit to an existing `POL_NO`) does not
  change the column's shape and will **not** trigger staleness. That's
  correct behavior, not a bug — it mirrors the CLI's `track` command
  exactly.
- Hover only shows the stale card on **line 1** (the header row), same
  constraint as step 11.

15. With `pasl_policy.dat` already profiled this session (step 9), make an
    actual **schema-shape** edit and save: delete an entire column (header
    cell + that value from every data row), *or* add a new column, *or*
    change a column's values so its inferred type flips (e.g. make every
    `POL_NO` value non-numeric).
16. Hover the header row again → confirm **"Profile out of date"** card
    with a real change summary (added/removed/type-changed), not the
    normal mapping card.
17. If the review panel for that file is still open, confirm a yellow
    stale banner appears there too.

## 4. Agent pipeline + progress

18. Re-run **Profile & Map Current File** on `pasl_policy.dat` (this also
    clears staleness) → pick **Agent pipeline**.
19. Watch the progress notification → confirm it updates through stages
    (rule pass → MappingAgent → CriticAgent → SQLAgent → row shape →
    finalizing), not a silent hang.
20. If no `ANTHROPIC_API_KEY` is set: confirm the error shown is legible
    text, not a raw Python traceback.

## 5. dbt scaffolding + diagnostics

21. In the review panel, accept/skip through all pending columns, resolve
    any missing fields / contested mappings shown.
22. Click **Finalize review** → confirm success message with a saved path.
23. Click **Generate dbt Staging Model** → save dialog → save it.
24. Confirm the `.sql` file opens automatically, and warning squiggles
    appear on lines for genuinely unmapped optional fields (hover the
    squiggle to see the message).
25. Re-click **Generate dbt Staging Model** targeting the same path →
    confirm a modal "already exists, overwrite?" dialog appears (skip
    confirming unless you also want to test the overwrite path).

## 6. Health sidebar grouping

26. Explorer sidebar → **Schema Inference: Mapping Health** view.
27. Profile+map both `pasl_policy.dat` and `pasm_policy.dat` if not
    already done this session (different source names each).
28. Confirm entries group by table name (separate expandable groups, not
    one flat list) — click the refresh icon in the view title if stale.

## 7. Crash recovery

29. Task Manager (or `tasklist` in a terminal) → find the `python.exe`
    process running `schema_inference.bridge` → kill it.
30. Try any Schema Inference command → confirm a "Restart Bridge" /
    "Select Python Interpreter" error notification appears, not a silent
    failure.

## 8. Self-tuning panel

31. **Schema Inference: Open Self-Tuning Panel**.
32. **Layer 0**: confirm current weights table shows. Type source `pasl`,
    click **Load**. Click **Run (dry run)** → confirm baseline/best
    mean_loss/f1/hard_f1 shown, weights table unchanged. Click **Run &
    Apply** → confirm weights table updates if an improvement was found
    (cross-check `schema_inference/agent_config.yml` on disk, under
    `rule_engine.weights_by_source.pasl`).
33. **Layer 1**: confirm active/retired counts show. Click **Curate now**
    → confirm counts update (0 inserted is fine if no qualifying history
    yet). If any active examples are listed, type a reason next to one
    and click **Retire** → confirm it disappears from the active list and
    the retired count increments.
34. **Layer 2**: read the duration hint first — **this is genuinely slow**
    (a single round re-runs the full agent pipeline multiple times through
    a shared 5 req/min throttle; expect several minutes minimum, possibly
    much longer). Pick agent `mapping`, click **Load** → confirm
    active-prompt status and any existing candidate list show.
    **Optional, time-permitting**: click **Run tuning session** with max
    rounds `1` → confirm the button disables and the progress area updates
    once a round completes (a long wait here is expected, not a hang).
    Once any candidate exists: click **View diff** → confirm VS Code's
    native diff editor opens, comparing active vs. candidate prompt. Click
    **Accept** on an unaccepted candidate → confirm a **modal confirm**
    appears before anything happens; confirm it → candidate now shows an
    "accepted" badge and the active-prompt status updates.

## 9. Installed-extension check (optional)

Only needed to verify the real packaged `.vsix`, not just F5.

35. In a normal terminal: `cd vscode && npm run package && npx vsce package`.
36. `code --install-extension schema-inference-vscode-0.0.1.vsix --force`
    in a **normal** VS Code window (not an Extension Dev Host).
37. Repeat §1–2 (hover, review panel) there to confirm it works as a real
    installed extension, not just via F5.

## 10. Snowflake as a source and as a mapping target

Neither of these can be verified from a coding session — no reachable
Snowflake instance/credentials in that environment. Needs your own
machine with real `SNOWFLAKE_*` environment variables set as real process
env vars (not just present in `.env` — see README's Requirements).

### 10a. Profile Snowflake Table (source)

38. `Ctrl+Shift+P` → **Schema Inference: Profile Snowflake Table** → enter
    a logical source name → enter a real `DATABASE.SCHEMA.TABLE` → pick
    Rule-only or Agent pipeline.
39. Confirm profiling succeeds and the "N/M columns mapped" message
    appears.
40. Confirm the review panel opens **directly** (no intermediate hover
    step — there's no file to hover for a live table). Walk through
    accept/modify/skip as usual.
41. Bad credentials/unreachable account/nonexistent table: confirm the
    error message is the Snowflake connector's own (reasonably legible),
    not an unhandled crash.

### 10b. Extract Target Schema from Snowflake Table

42. **Schema Inference: Extract Target Schema from Snowflake Table** →
    enter a real *target* table (e.g. a warehouse silver table like
    `SLV_POLICY`).
43. Confirm an untitled JSON tab opens with a `schema_key` and a `fields`
    array — spot-check a few entries: NOT NULL columns should show
    `"required": true`; numeric columns should show a sensible
    `target_type` (`integer`/`bigint`/`decimal`); no `aliases` populated
    (expected — see the README's known-limitations note).
44. Edit one field in the tab (e.g. add an alias, or fix a `target_type`)
    before continuing — confirms the edit actually gets picked up in the
    next step, not just the original extraction.
45. Answer the follow-up prompt with a source `table_name` you've already
    profiled (e.g. `pasl_policy` from §1). Confirm the success message
    names the `schema_key` and `table_name`.
46. Re-run **Profile & Map Current File** on that same source file →
    confirm the resulting mappings target the *extracted* schema's field
    names, not the default `policy` schema's — this is the actual proof
    the live registration took effect, not just that the command didn't
    error.
47. Restart the bridge (**Schema Inference: Restart Bridge Process**) and
    repeat step 46 → confirm mappings fall back to the default `policy`
    schema again, demonstrating the registration really is session-only,
    not persisted anywhere.
