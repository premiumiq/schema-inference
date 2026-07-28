# Changelog

## 0.0.1

Initial internal build. All seven build-order steps from
`docs/map-7-vscode-extension-design.md`, plus the fixes/features from the
follow-up "demo-ready" pass:

- Hover annotations on `.dat`/`.csv` header columns (target field,
  confidence, method, notes).
- Accept/reject review panel: per-column accept/modify/skip, missing
  required field resolution, MAP-3 contested-mapping resolution (with
  per-column confidence and a pre-selected provisional winner), MAP-5
  row-shape display.
- Mapping health sidebar (Explorer view), grouped by table, backed by the
  metamodel's scoring history.
- dbt staging-model scaffolding, with a modal confirm before ever
  overwriting an existing file, and warning diagnostics on genuinely
  unmapped columns in the generated SQL.
- Rule-only and LLM-assisted agent-pipeline mapping, with live
  per-stage progress during an agent run.
- Stale-proposal detection: a file-system watcher plus the existing
  `tracker.check` mechanism flags hover cards and open review panels when
  the source file changes on disk after profiling.
- Each review-panel session is independent per source file (previously a
  hard singleton would silently reveal the wrong file's stale session).
