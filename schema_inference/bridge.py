"""JSON-RPC 2.0 bridge for the VS Code extension (MAP-7).

Reads one JSON-RPC request per line from stdin, writes one JSON-RPC response
per line to stdout, both newline-delimited (not Content-Length-framed — this
isn't LSP, see docs/map-7-vscode-extension-design.md §3.1 for why). Every
method here is a thin wrapper around the same functions the CLI
(`__main__.py`) calls — this module must never hold pipeline logic of its
own, only request/response marshaling, mirroring how `__main__.py`'s
`_cmd_*` functions are themselves thin wrappers.

Run as: python -m schema_inference.bridge

Not wired into any packaging/activation yet — this is build-order step 2
(bridge only, no extension). Validate with a raw stdio harness /
tests/test_bridge.py, not a VS Code install.

`map.run` with `agent: true` streams `map.progress` notifications (no
`id`, per JSON-RPC 2.0) at `agents.orchestrator.run_mapping`'s stage
boundaries (rule_pass/mapping_agent/critic_agent/sql_agent/row_shape/done)
— coarse-grained by design, not per-column, so it doesn't need a callback
threaded through MappingAgent/CriticAgent/SQLAgent's own concurrency
internals. `dispatch()`'s `notify` param is the plumbing: `serve()` passes
one that writes straight to `out_stream`; `_m_map_run` never touches the
stream directly, it just calls a module-level `_notify_sink` that
`dispatch()` points at whatever `notify` was passed for the duration of
one request (safe under `serve()`'s single-threaded synchronous loop —
one request completes before the next line is read, so there's no
concurrent access to worry about).
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any, Callable

from .models import ApprovedMapping, ColumnMapping, MappingDefinition, MappingProposal, MissingFieldResolution, SchemaProfile


# ─── JSON-RPC plumbing ─────────────────────────────────────────────────────────

class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


PARSE_ERROR = -32700
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
APP_ERROR = -32000


# ─── Review sessions ────────────────────────────────────────────────────────────
#
# One MappingProposal under review = one session, keyed by a server-issued
# session_id. Holds the in-progress ApprovedMapping per column so RPC calls
# can arrive one column at a time, in any order the reviewer clicks them in
# — the thing review_proposal()'s blocking input() loop couldn't do. Built
# directly on the decision functions extracted from reviewer.py in MAP-7
# build-order step 1 (accept_mapping/modify_mapping/skip_mapping/
# resolve_missing_field/apply_contest_resolution/assign_extended_attr).

class ReviewSession:
    def __init__(self, proposal: MappingProposal):
        self.proposal = proposal
        self.approved_by_col: dict[str, ApprovedMapping] = {}
        self.missing_field_resolutions: dict[str, MissingFieldResolution] = {}
        self.extended_extra: dict[str, bool] = {}
        self.contests_resolved: set[str] = set()

        # Same auto-approve tier as review_proposal() — confidence >= 0.85
        # needs no reviewer action, seeded immediately so review.status
        # reflects it without a client round-trip. This mirrors existing
        # CLI behavior; it is not a new auto-accept path (see design doc
        # §5's "never auto-promote" invariant — the tier and threshold are
        # unchanged from what review_proposal() already does today).
        from .reviewer import accept_mapping
        for m in proposal.mappings:
            if m.confidence >= 0.85:
                am = accept_mapping(m)
                am.reviewer_action = "auto_approved"
                self.approved_by_col[m.source_column] = am

    def mapping_by_col(self, source_column: str) -> ColumnMapping:
        for m in self.proposal.mappings:
            if m.source_column == source_column:
                return m
        raise RpcError(INVALID_PARAMS, f"unknown source_column '{source_column}' for this proposal")

    def pending_columns(self) -> list[str]:
        return [
            m.source_column for m in self.proposal.mappings
            if m.source_column not in self.approved_by_col
        ]

    def status(self) -> dict:
        return {
            "total_columns": len(self.proposal.mappings),
            "decided_columns": len(self.approved_by_col),
            "pending_columns": self.pending_columns(),
            "missing_standard_fields": list(self.proposal.missing_standard_fields),
            "missing_fields_resolved": list(self.missing_field_resolutions.keys()),
            "contested_targets": [c["target_field"] for c in self.proposal.contested_mappings],
            "contests_resolved": sorted(self.contests_resolved),
        }


_sessions: dict[str, ReviewSession] = {}


# ─── Method implementations ─────────────────────────────────────────────────────

def _m_ping(params: dict) -> dict:
    from . import VERSION
    return {"pong": True, "version": VERSION}


def _m_profile_run(params: dict) -> dict:
    from .profiler import profile_file
    from .tracker import REGISTRY_DIR

    file_path = Path(params["file_path"])
    if not file_path.exists():
        raise RpcError(APP_ERROR, f"file not found: {file_path}")

    profile = profile_file(
        file_path,
        source_name=params["source_name"],
        table_name=params.get("table_name"),
        delimiter=params.get("delimiter"),
    )
    table = profile.tables[0]

    out_path = params.get("output")
    if out_path:
        out = Path(out_path)
    else:
        source_dir = REGISTRY_DIR / params["source_name"]
        source_dir.mkdir(parents=True, exist_ok=True)
        out = source_dir / f"profile_{table.name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(profile.model_dump_json(indent=2), encoding="utf-8")

    return {
        "profile_path": str(out),
        "source_name": profile.source_name,
        "table_name": table.name,
        "row_count": table.row_count,
        "column_count": len(table.columns),
        "delimiter": table.delimiter,
    }


def _m_profile_load(params: dict) -> dict:
    profile_path = Path(params["path"])
    if not profile_path.exists():
        raise RpcError(APP_ERROR, f"profile file not found: {profile_path}")
    profile = SchemaProfile.model_validate_json(profile_path.read_text(encoding="utf-8"))
    return profile.model_dump()


def _m_map_run(params: dict) -> dict:
    profile_path = Path(params["profile_path"])
    if not profile_path.exists():
        raise RpcError(APP_ERROR, f"profile file not found: {profile_path}")

    profile = SchemaProfile.model_validate_json(profile_path.read_text(encoding="utf-8"))
    table_name = params["table_name"]
    table = next((t for t in profile.tables if t.name == table_name), None)
    if table is None:
        names = [t.name for t in profile.tables]
        raise RpcError(INVALID_PARAMS, f"table '{table_name}' not in profile. Available: {names}")

    if params.get("agent"):
        from .agents.orchestrator import run_mapping

        run = run_mapping(
            table,
            source_name=profile.source_name,
            use_agent=True,
            concurrency=params.get("concurrency"),
            eval_mode=bool(params.get("eval")),
            on_stage=lambda stage, info: _notify_sink("map.progress", {"stage": stage, **info}),
        )
        proposal = run.proposal
        result: dict[str, Any] = {
            "proposal": proposal.model_dump(),
            "run_id": run.run_id,
            "rule_pass_count": run.rule_pass_count,
            "agent_pass_count": run.agent_pass_count,
            "critic_overrides": run.critic_overrides,
            "eval_score": run.eval_score,
            "duration_seconds": run.duration_seconds,
        }
    else:
        from .mapper import map_table

        proposal = map_table(
            table,
            source_name=profile.source_name,
            llm_threshold=params.get("threshold", 0.70),
            use_llm=not params.get("no_llm", False),
        )
        result = {"proposal": proposal.model_dump(), "run_id": proposal.run_id}

    out_path = params.get("output")
    if out_path:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(proposal.model_dump_json(indent=2), encoding="utf-8")
        result["proposal_path"] = str(out)

    return result


def _m_review_start(params: dict) -> dict:
    proposal_path = Path(params["proposal_path"])
    if not proposal_path.exists():
        raise RpcError(APP_ERROR, f"proposal file not found: {proposal_path}")

    proposal = MappingProposal.model_validate_json(proposal_path.read_text(encoding="utf-8"))
    session_id = str(uuid.uuid4())
    session = ReviewSession(proposal)
    _sessions[session_id] = session

    return {
        "session_id": session_id,
        "source_name": proposal.source_name,
        "table_name": proposal.table_name,
        "mappings": [m.model_dump() for m in proposal.mappings],
        "unmapped_columns": list(proposal.unmapped_columns),
        "missing_standard_fields": list(proposal.missing_standard_fields),
        "contested_mappings": list(proposal.contested_mappings),
        "excluded_metadata_columns": list(proposal.excluded_metadata_columns),
        "row_shape": proposal.row_shape,
        "status": session.status(),
    }


def _session(params: dict) -> ReviewSession:
    session_id = params.get("session_id")
    session = _sessions.get(session_id)
    if session is None:
        raise RpcError(INVALID_PARAMS, f"unknown or expired session_id '{session_id}'")
    return session


def _m_review_accept_column(params: dict) -> dict:
    from .reviewer import accept_mapping

    session = _session(params)
    m = session.mapping_by_col(params["source_column"])
    session.approved_by_col[m.source_column] = accept_mapping(m)
    return session.status()


def _m_review_modify_column(params: dict) -> dict:
    from .reviewer import UnknownTargetFieldError, modify_mapping

    session = _session(params)
    m = session.mapping_by_col(params["source_column"])
    try:
        am = modify_mapping(
            m,
            target_field=params.get("target_field"),
            sql_expression=params.get("sql_expression", m.sql_expression),
            notes=params.get("notes", ""),
        )
    except UnknownTargetFieldError as exc:
        raise RpcError(APP_ERROR, str(exc)) from exc
    session.approved_by_col[m.source_column] = am
    return session.status()


def _m_review_skip_column(params: dict) -> dict:
    from .reviewer import skip_mapping

    session = _session(params)
    m = session.mapping_by_col(params["source_column"])
    session.approved_by_col[m.source_column] = skip_mapping(m)
    return session.status()


def _m_review_resolve_missing_field(params: dict) -> dict:
    from .reviewer import resolve_missing_field

    session = _session(params)
    field_name = params["field_name"]
    if field_name not in session.proposal.missing_standard_fields:
        raise RpcError(INVALID_PARAMS, f"'{field_name}' is not a missing_standard_field on this proposal")
    resolution = resolve_missing_field(
        field_name,
        params["resolution"],
        hardcoded_value=params.get("hardcoded_value"),
        derivation_sql=params.get("derivation_sql"),
    )
    session.missing_field_resolutions[field_name] = resolution
    return session.status()


def _m_review_resolve_contest(params: dict) -> dict:
    from .reviewer import apply_contest_resolution

    session = _session(params)
    target = params["target_field"]
    contest = next(
        (c for c in session.proposal.contested_mappings if c["target_field"] == target), None
    )
    if contest is None:
        raise RpcError(INVALID_PARAMS, f"no contested mapping for target_field '{target}'")

    competing = contest["competing_columns"]
    winner = params.get("winner")
    if winner is not None and winner not in competing:
        raise RpcError(INVALID_PARAMS, f"'{winner}' is not one of the competing columns {competing}")

    # apply_contest_resolution needs every competing column already decided
    # (accepted/skipped) so it has an ApprovedMapping to overwrite in place —
    # same precondition the CLI's _phase_contested_mappings runs under
    # (it runs after the flagged/low-confidence review phases).
    missing = [c for c in competing if c not in session.approved_by_col]
    if missing:
        raise RpcError(
            APP_ERROR,
            f"columns {missing} must be accepted/modified/skipped before resolving this contest",
        )

    apply_contest_resolution(target, competing, winner, session.approved_by_col)
    session.contests_resolved.add(target)
    return session.status()


def _m_review_assign_extended_attr(params: dict) -> dict:
    from .reviewer import assign_extended_attr

    session = _session(params)
    col = params["source_column"]
    keep, warning = assign_extended_attr(
        col,
        keep_as_extended=bool(params.get("keep_as_extended", True)),
        target=params.get("target"),
    )
    session.extended_extra[col] = keep
    return {**session.status(), "kept_as_extended": keep, "warning": warning}


def _m_review_finalize(params: dict) -> dict:
    from datetime import datetime

    from .reviewer import MAPPINGS_DIR, _get_reviewer_identity, _record_review_to_metamodel

    session = _session(params)
    proposal = session.proposal

    pending = session.pending_columns()
    if pending:
        raise RpcError(APP_ERROR, f"columns still pending a decision: {pending}")
    unresolved_contests = [
        c["target_field"] for c in proposal.contested_mappings
        if c["target_field"] not in session.contests_resolved
    ]
    if unresolved_contests:
        raise RpcError(APP_ERROR, f"unresolved contests: {unresolved_contests}")

    approved = [session.approved_by_col[m.source_column] for m in proposal.mappings]

    extended = list(proposal.unmapped_columns)
    extended += [col for col, am in session.approved_by_col.items() if am.target_field is None]
    extended += [col for col, kept in session.extended_extra.items() if kept]
    extended = list(dict.fromkeys(extended))

    resolutions = [
        session.missing_field_resolutions.get(f) or MissingFieldResolution(target_field=f, resolution="NULL")
        for f in proposal.missing_standard_fields
    ]

    _record_review_to_metamodel(proposal, approved)

    reviewer_identity = params.get("reviewer_identity") or _get_reviewer_identity()
    definition = MappingDefinition(
        source_name=proposal.source_name,
        table_name=proposal.table_name,
        approved_mappings=approved,
        extended_attributes=extended,
        missing_field_resolutions=resolutions,
        reviewer_identity=reviewer_identity,
        reviewed_at=datetime.now(),
        profile_hash=params.get("profile_hash", ""),
    )

    out_path = params.get("output_path")
    if out_path:
        out = Path(out_path)
    else:
        MAPPINGS_DIR.mkdir(parents=True, exist_ok=True)
        out = MAPPINGS_DIR / f"{proposal.source_name}_{proposal.table_name}_mapping.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(definition.model_dump_json(indent=2), encoding="utf-8")

    del _sessions[params["session_id"]]
    return {"definition": definition.model_dump(), "definition_path": str(out)}


def _m_metamodel_query_loss_runs(params: dict) -> dict:
    from .metamodel.store import open_store

    store = open_store()
    if store is None:
        return {"loss_runs": [], "metamodel_available": False}
    try:
        runs = store.get_loss_runs(params["source_name"], limit=params.get("limit", 50))
    finally:
        store.close()
    return {"loss_runs": runs, "metamodel_available": True}


def _m_tuning_layer0_status(params: dict) -> dict:
    """Layer 0 (tools/tune_rule_weights.py): current active weights + recent
    Layer-0 tuning runs for this source. loss_runs also carries regular
    scoring runs (scripts/score_mappings.py) under the same source_name, so
    filter to config_snapshot.tuning_layer == 0 -- the tag _log_tuning_run
    already stamps every Layer-0 row with."""
    from .mapper import _rule_weights
    from .metamodel.store import open_store

    source_name = params["source_name"]
    weights = _rule_weights(source_name)
    active_weights = {"name_sim": weights[0], "type_compat": weights[1], "pattern_bonus": weights[2]}

    store = open_store()
    if store is None:
        return {"active_weights": active_weights, "recent_runs": [], "metamodel_available": False}
    try:
        runs = store.get_loss_runs(source_name, limit=20)
    finally:
        store.close()
    layer0_runs = [r for r in runs if r["config_snapshot"].get("tuning_layer") == 0]
    return {"active_weights": active_weights, "recent_runs": layer0_runs, "metamodel_available": True}


def _m_tuning_run_layer0(params: dict) -> dict:
    from tools.tune_rule_weights import run_layer0_tuning

    return run_layer0_tuning(
        source_name=params.get("source_name", "pasl"),
        data_file=params.get("data_file"),
        step=params.get("step", 0.05),
        apply=bool(params.get("apply")),
    )


def _m_tuning_few_shot_stats(params: dict) -> dict:
    """Layer 1 (tools/curate_few_shot_bank.py) insight: active bank contents
    plus counts by origin (hard_tp / critic_override_accepted), and how
    many examples have been retired."""
    from .metamodel.store import open_store

    source_name = params["source_name"]
    store = open_store()
    if store is None:
        return {"active": [], "active_count": 0, "retired_count": 0, "by_origin": {}, "metamodel_available": False}
    try:
        active = store.get_few_shot_examples(source_name, status="active")
        retired = store.get_few_shot_examples(source_name, status="retired")
    finally:
        store.close()

    by_origin: dict[str, int] = {}
    for row in active:
        by_origin[row["origin"]] = by_origin.get(row["origin"], 0) + 1

    return {
        "active": active,
        "active_count": len(active),
        "retired_count": len(retired),
        "by_origin": by_origin,
        "metamodel_available": True,
    }


def _m_tuning_run_layer1_curation(params: dict) -> dict:
    from tools.curate_few_shot_bank import curate

    return curate(params["source_name"])


def _m_tuning_retire_few_shot_example(params: dict) -> dict:
    from .metamodel.store import open_store

    store = open_store()
    if store is None:
        raise RpcError(APP_ERROR, "metamodel store unavailable")
    try:
        updated = store.retire_few_shot_example(params["example_id"], params.get("reason", ""))
    finally:
        store.close()
    return {"retired": updated > 0}


def _m_tuning_prompt_versions(params: dict) -> dict:
    """Layer 2 (tools/tune_prompts.py) candidate list, plus the currently
    active prompt text so the extension can diff any candidate against it."""
    from .metamodel.store import open_store

    agent_name = params["agent_name"]
    store = open_store()
    if store is None:
        return {"versions": [], "active_prompt": None, "metamodel_available": False}
    try:
        versions = store.get_prompt_versions(agent_name)
        active_prompt = store.get_active_prompt(agent_name)
    finally:
        store.close()
    return {"versions": versions, "active_prompt": active_prompt, "metamodel_available": True}


def _m_tuning_accept_prompt_version(params: dict) -> dict:
    """The human-merge action (design doc's "never auto-promote" invariant,
    same as Layer 2's CLI --accept flag) -- the extension gates this behind
    a modal confirm before ever calling it."""
    from .metamodel.store import open_store

    store = open_store()
    if store is None:
        raise RpcError(APP_ERROR, "metamodel store unavailable")
    try:
        updated = store.accept_prompt_version(params["version_id"])
    finally:
        store.close()
    if not updated:
        raise RpcError(APP_ERROR, f"no prompt_version found with id '{params['version_id']}'")
    return {"accepted": True}


def _m_tuning_run_layer2_session(params: dict) -> dict:
    """Streams tuning.progress notifications (round/version_id/loss_before/
    loss_after/improved/regressed) via the same notify plumbing map.run's
    agent branch uses -- see run_tuning_session's on_round param."""
    from tools.tune_prompts import run_tuning_session

    return run_tuning_session(
        agent_name=params.get("agent_name", "mapping"),
        source_name=params.get("source_name", "pasl"),
        data_file=params.get("data_file"),
        max_rounds=params.get("max_rounds", 5),
        on_round=lambda n, info: _notify_sink("tuning.progress", {"round": n, **info}),
    )


def _m_tracker_check(params: dict) -> dict:
    from .profiler import profile_file
    from .tracker import BreakingSchemaChangeError, record_or_compare

    file_path = Path(params["file_path"])
    if not file_path.exists():
        raise RpcError(APP_ERROR, f"file not found: {file_path}")

    profile = profile_file(
        file_path,
        source_name=params["source_name"],
        table_name=params.get("table_name"),
        delimiter=params.get("delimiter"),
    )
    table = profile.tables[0]

    try:
        sv, report = record_or_compare(
            table, source_name=params["source_name"], force=bool(params.get("force")),
        )
        return {
            "version": sv.model_dump(),
            "report": report.model_dump() if report else None,
            "breaking": False,
        }
    except BreakingSchemaChangeError as exc:
        return {"version": None, "report": exc.report.model_dump(), "breaking": True}


def _m_sql_generate_staging_model(params: dict) -> dict:
    """MAP-7 step 7: dbt staging model scaffolding. Never overwrites an
    existing file silently -- if output_path exists and force isn't set,
    returns a preview instead of writing, so the extension can show a
    confirm-before-overwrite prompt (same destructive-action caution as
    the CLI's --force-accept-breaking flag on `track`)."""
    from .models import MappingDefinition
    from .sql_scaffold import find_unmapped_fields, generate_staging_model_sql

    definition_path = Path(params["definition_path"])
    if not definition_path.exists():
        raise RpcError(APP_ERROR, f"mapping definition file not found: {definition_path}")

    definition = MappingDefinition.model_validate_json(definition_path.read_text(encoding="utf-8"))
    sql = generate_staging_model_sql(definition)

    # MAP-7 demo-ready plan phase 4: line numbers for the extension to set
    # VS Code diagnostics on, one per genuinely unmapped canonical field.
    # A simple text search rather than manual offset bookkeeping through
    # the header/CTE template -- canonical field names are unique per
    # schema, so "NULL as {name}" only ever matches that field's own line.
    lines = sql.splitlines()
    unmapped_fields = []
    for name in find_unmapped_fields(definition):
        needle = f"NULL as {name}"
        for i, line in enumerate(lines):
            if needle in line:
                unmapped_fields.append({"field_name": name, "line": i})
                break

    output_path = Path(params["output_path"])
    if output_path.exists() and not params.get("force"):
        return {
            "written": False, "exists": True, "path": str(output_path),
            "preview": sql, "unmapped_fields": unmapped_fields,
        }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(sql, encoding="utf-8")
    return {
        "written": True, "exists": False, "path": str(output_path),
        "preview": sql, "unmapped_fields": unmapped_fields,
    }


_METHODS: dict[str, Callable[[dict], dict]] = {
    "ping": _m_ping,
    "profile.run": _m_profile_run,
    "profile.load": _m_profile_load,
    "map.run": _m_map_run,
    "review.start": _m_review_start,
    "review.accept_column": _m_review_accept_column,
    "review.modify_column": _m_review_modify_column,
    "review.skip_column": _m_review_skip_column,
    "review.resolve_missing_field": _m_review_resolve_missing_field,
    "review.resolve_contest": _m_review_resolve_contest,
    "review.assign_extended_attr": _m_review_assign_extended_attr,
    "review.finalize": _m_review_finalize,
    "metamodel.query_loss_runs": _m_metamodel_query_loss_runs,
    "tracker.check": _m_tracker_check,
    "sql.generate_staging_model": _m_sql_generate_staging_model,
    "tuning.layer0_status": _m_tuning_layer0_status,
    "tuning.run_layer0": _m_tuning_run_layer0,
    "tuning.few_shot_stats": _m_tuning_few_shot_stats,
    "tuning.run_layer1_curation": _m_tuning_run_layer1_curation,
    "tuning.retire_few_shot_example": _m_tuning_retire_few_shot_example,
    "tuning.prompt_versions": _m_tuning_prompt_versions,
    "tuning.accept_prompt_version": _m_tuning_accept_prompt_version,
    "tuning.run_layer2_session": _m_tuning_run_layer2_session,
}


def _noop_notify(method: str, params: dict) -> None:
    pass


_notify_sink: Callable[[str, dict], None] = _noop_notify


def dispatch(request: dict, notify: Callable[[str, dict], None] | None = None) -> dict | None:
    """Handle one already-parsed JSON-RPC request. Returns the response dict,
    or None for a notification (no 'id') — callers should not write those.

    notify: called by a handler (currently only _m_map_run's agent branch)
    to emit a server-initiated notification before the response is ready.
    Defaults to a no-op — existing dispatch(request) call sites (tests,
    non-agent methods) are unaffected."""
    global _notify_sink
    previous_sink = _notify_sink
    _notify_sink = notify or _noop_notify
    try:
        id_ = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}

        handler = _METHODS.get(method)
        if handler is None:
            if id_ is None:
                return None
            return {"jsonrpc": "2.0", "id": id_, "error": {"code": METHOD_NOT_FOUND, "message": f"unknown method '{method}'"}}

        try:
            result = handler(params)
        except RpcError as exc:
            if id_ is None:
                return None
            return {"jsonrpc": "2.0", "id": id_, "error": {"code": exc.code, "message": exc.message}}
        except (KeyError, TypeError) as exc:
            if id_ is None:
                return None
            return {"jsonrpc": "2.0", "id": id_, "error": {"code": INVALID_PARAMS, "message": f"missing/invalid params: {exc}"}}
        except Exception as exc:  # noqa: BLE001 — surface any pipeline error to the client rather than crashing the bridge
            if id_ is None:
                return None
            return {"jsonrpc": "2.0", "id": id_, "error": {"code": APP_ERROR, "message": str(exc)}}

        if id_ is None:
            return None
        return {"jsonrpc": "2.0", "id": id_, "result": result}
    finally:
        _notify_sink = previous_sink


def serve(in_stream=None, out_stream=None) -> None:
    """Blocking read-dispatch-write loop. Exits cleanly on EOF (stdin closed
    — e.g. the VS Code extension terminating the child process)."""
    in_stream = in_stream or sys.stdin
    out_stream = out_stream or sys.stdout

    def notify(method: str, params: dict) -> None:
        out_stream.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}, default=str) + "\n")
        out_stream.flush()

    for line in in_stream:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            out_stream.write(json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": PARSE_ERROR, "message": f"invalid JSON: {exc}"},
            }) + "\n")
            out_stream.flush()
            continue

        response = dispatch(request, notify=notify)
        if response is not None:
            out_stream.write(json.dumps(response, default=str) + "\n")
            out_stream.flush()


if __name__ == "__main__":
    serve()
