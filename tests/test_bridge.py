"""MAP-7 build-order step 2: raw stdio-free harness for schema_inference/bridge.py.

Exercises dispatch() directly (no subprocess/stdio) since that's the unit
under test — the newline-delimited JSON framing in serve() is a thin loop
around dispatch() and isn't worth spawning a real process to cover.
"""

import json
from pathlib import Path

from schema_inference import bridge

FIXTURE = Path("examples/insurance/test_data/pasl_policy.dat")


def call(method, **params):
    response = bridge.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    assert response is not None
    assert response["id"] == 1
    return response


def test_ping():
    resp = call("ping")
    assert resp["result"]["pong"] is True


def test_unknown_method_returns_method_not_found():
    resp = call("no.such.method")
    assert resp["error"]["code"] == bridge.METHOD_NOT_FOUND


def test_notification_without_id_returns_none():
    assert bridge.dispatch({"jsonrpc": "2.0", "method": "ping", "params": {}}) is None


def test_full_profile_map_review_finalize_flow(tmp_path):
    profile_path = tmp_path / "profile.json"
    r = call(
        "profile.run",
        file_path=str(FIXTURE),
        source_name="pasl",
        output=str(profile_path),
    )
    assert r["result"]["row_count"] > 0
    table_name = r["result"]["table_name"]
    assert profile_path.exists()

    loaded = call("profile.load", path=str(profile_path))
    assert loaded["result"]["source_name"] == "pasl"

    proposal_path = tmp_path / "proposal.json"
    m = call(
        "map.run",
        profile_path=str(profile_path),
        table_name=table_name,
        no_llm=True,
        output=str(proposal_path),
    )
    proposal = m["result"]["proposal"]
    assert len(proposal["mappings"]) > 0
    assert proposal_path.exists()

    started = call("review.start", proposal_path=str(proposal_path))
    session_id = started["result"]["session_id"]
    pending = started["result"]["status"]["pending_columns"]

    for col in pending:
        resp = call("review.accept_column", session_id=session_id, source_column=col)
        assert "error" not in resp

    for field in proposal["missing_standard_fields"]:
        resp = call(
            "review.resolve_missing_field",
            session_id=session_id, field_name=field, resolution="NULL",
        )
        assert "error" not in resp

    for contest in proposal["contested_mappings"]:
        winner = contest["competing_columns"][0]
        resp = call(
            "review.resolve_contest",
            session_id=session_id, target_field=contest["target_field"], winner=winner,
        )
        assert "error" not in resp

    definition_path = tmp_path / "definition.json"
    fin = call("review.finalize", session_id=session_id, output_path=str(definition_path))
    assert definition_path.exists()
    assert fin["result"]["definition"]["source_name"] == "pasl"

    # Session is consumed on finalize.
    resp = call("review.accept_column", session_id=session_id, source_column=pending[0] if pending else "X")
    assert resp["error"]["code"] == bridge.INVALID_PARAMS


def test_review_finalize_rejects_pending_columns(tmp_path):
    profile_path = tmp_path / "profile.json"
    call("profile.run", file_path=str(FIXTURE), source_name="pasl", output=str(profile_path))
    proposal_path = tmp_path / "proposal.json"
    call(
        "map.run",
        profile_path=str(profile_path),
        table_name="pasl_policy",
        no_llm=True,
        output=str(proposal_path),
    )
    started = call("review.start", proposal_path=str(proposal_path))
    session_id = started["result"]["session_id"]

    resp = call("review.finalize", session_id=session_id)
    assert resp["error"]["code"] == bridge.APP_ERROR
    assert "pending" in resp["error"]["message"]


def test_metamodel_query_loss_runs_never_raises():
    resp = call("metamodel.query_loss_runs", source_name="pasl", limit=5)
    assert "loss_runs" in resp["result"]
    assert isinstance(resp["result"]["metamodel_available"], bool)


def test_tracker_check_first_then_no_change(tmp_path, monkeypatch):
    monkeypatch.setattr("schema_inference.tracker.REGISTRY_DIR", tmp_path / "registry")

    first = call("tracker.check", file_path=str(FIXTURE), source_name="bridge_test_source")
    assert first["result"]["breaking"] is False
    assert first["result"]["version"]["version"] == 1
    assert first["result"]["report"] is None

    second = call("tracker.check", file_path=str(FIXTURE), source_name="bridge_test_source")
    assert second["result"]["breaking"] is False
    assert second["result"]["report"] is None
    assert second["result"]["version"]["version"] == 1


def test_map_run_agent_streams_map_progress_notifications(tmp_path, monkeypatch):
    """Stubs orchestrator.run_mapping to avoid a real (slow, API-key-needing)
    agent run -- this test is only about the notification plumbing between
    on_stage and dispatch()'s notify param, not the agent pipeline itself."""
    from datetime import datetime

    from schema_inference.models import AgentMappingRun, MappingProposal

    def fake_run_mapping(table, source_name, **kwargs):
        on_stage = kwargs.get("on_stage")
        if on_stage:
            on_stage("rule_pass", {"columns": 5})
            on_stage("done", {"run_id": "fake-run"})
        return AgentMappingRun(
            run_id="fake-run", source_name=source_name, table_name=table.name,
            proposal=MappingProposal(
                source_name=source_name, table_name=table.name, mappings=[],
                unmapped_columns=[], missing_standard_fields=[], contested_mappings=[],
                excluded_metadata_columns=[], row_shape=None, run_id="fake-run",
            ),
            traces=[], rule_pass_count=5, agent_pass_count=0, critic_overrides=0,
            eval_score=None, started_at=datetime.now(), duration_seconds=0.01,
        )

    monkeypatch.setattr("schema_inference.agents.orchestrator.run_mapping", fake_run_mapping)

    profile_path = tmp_path / "profile.json"
    call("profile.run", file_path=str(FIXTURE), source_name="pasl", output=str(profile_path))

    notifications = []
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "map.run",
        "params": {"profile_path": str(profile_path), "table_name": "pasl_policy", "agent": True},
    }
    response = bridge.dispatch(request, notify=lambda method, params: notifications.append((method, params)))

    assert response["result"]["run_id"] == "fake-run"
    assert notifications == [
        ("map.progress", {"stage": "rule_pass", "columns": 5}),
        ("map.progress", {"stage": "done", "run_id": "fake-run"}),
    ]


def test_dispatch_without_notify_defaults_to_no_op_and_does_not_leak_sink():
    """A plain dispatch() call (no notify) must not carry over a notify
    sink from a previous call -- _notify_sink is reset in dispatch()'s
    finally block regardless of how the previous call was made."""
    response = bridge.dispatch({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
    assert response["result"]["pong"] is True
    assert bridge._notify_sink is bridge._noop_notify


def test_sql_generate_staging_model_never_overwrites_without_force(tmp_path):
    profile_path = tmp_path / "profile.json"
    call("profile.run", file_path=str(FIXTURE), source_name="pasl", output=str(profile_path))
    proposal_path = tmp_path / "proposal.json"
    call(
        "map.run",
        profile_path=str(profile_path),
        table_name="pasl_policy",
        no_llm=True,
        output=str(proposal_path),
    )
    started = call("review.start", proposal_path=str(proposal_path))
    session_id = started["result"]["session_id"]
    for col in started["result"]["status"]["pending_columns"]:
        call("review.accept_column", session_id=session_id, source_column=col)
    for field in started["result"]["missing_standard_fields"]:
        call("review.resolve_missing_field", session_id=session_id, field_name=field, resolution="NULL")
    for contest in started["result"]["contested_mappings"]:
        call(
            "review.resolve_contest",
            session_id=session_id, target_field=contest["target_field"],
            winner=contest["competing_columns"][0],
        )
    definition_path = tmp_path / "definition.json"
    call("review.finalize", session_id=session_id, output_path=str(definition_path))

    output_path = tmp_path / "stg_pasl_policy.sql"
    first = call(
        "sql.generate_staging_model",
        definition_path=str(definition_path), output_path=str(output_path),
    )
    assert first["result"]["written"] is True
    assert output_path.exists()
    written_contents = output_path.read_text(encoding="utf-8")
    assert "with source as" in written_contents

    # Second call without force must not clobber the file -- returns a
    # preview instead, mirroring the "never overwrite silently" rule.
    output_path.write_text("-- hand-edited by a human, do not clobber --", encoding="utf-8")
    second = call(
        "sql.generate_staging_model",
        definition_path=str(definition_path), output_path=str(output_path),
    )
    assert second["result"]["written"] is False
    assert second["result"]["exists"] is True
    assert output_path.read_text(encoding="utf-8") == "-- hand-edited by a human, do not clobber --"

    third = call(
        "sql.generate_staging_model",
        definition_path=str(definition_path), output_path=str(output_path), force=True,
    )
    assert third["result"]["written"] is True
    assert "with source as" in output_path.read_text(encoding="utf-8")


def test_serve_reads_newline_delimited_requests_and_writes_responses():
    import io

    inp = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}) + "\n")
    out = io.StringIO()
    bridge.serve(in_stream=inp, out_stream=out)
    lines = [l for l in out.getvalue().splitlines() if l.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["result"]["pong"] is True
