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


def test_profile_run_snowflake_writes_registry_profile_and_returns_summary(tmp_path, monkeypatch):
    """profile_snowflake_table() has had zero real callers anywhere in this
    repo until this bridge method -- stubbed here (no reachable Snowflake
    instance in this environment) to prove the bridge wraps it correctly,
    same registry-path convention as profile.run."""
    from datetime import datetime

    from schema_inference.models import ColumnProfile, SchemaProfile, TableProfile

    def fake_profile_snowflake_table(database, schema, table, source_name, table_name=None, **kwargs):
        tname = table_name or table.lower()
        col = ColumnProfile(
            name="POLICY_ID", inferred_type="integer", null_rate=0.0, distinct_count=10,
            sample_values=["1", "2"], value_distribution={},
        )
        return SchemaProfile(
            source_name=source_name,
            tables=[TableProfile(
                name=tname, row_count=100, columns=[col], delimiter="",
                source_file=f"{database}.{schema}.{table}",
            )],
            profiled_at=datetime.now(),
            profile_hash="fakehash123",
        )

    monkeypatch.setattr("schema_inference.snowflake_reader.profile_snowflake_table", fake_profile_snowflake_table)

    out = tmp_path / "profile.json"
    resp = call(
        "profile.run_snowflake",
        database="DEV_DB", schema="PASL", table="pasl_policy",
        source_name="pasl", output=str(out),
    )
    result = resp["result"]
    assert result["table_name"] == "pasl_policy"
    assert result["row_count"] == 100
    assert result["column_count"] == 1
    assert out.exists()
    assert "POLICY_ID" in out.read_text(encoding="utf-8")


def test_profile_run_snowflake_surfaces_connection_errors_as_app_error(monkeypatch):
    def raise_connection_error(*args, **kwargs):
        raise RuntimeError("250001: Could not connect to Snowflake backend")

    monkeypatch.setattr("schema_inference.snowflake_reader.profile_snowflake_table", raise_connection_error)

    resp = call(
        "profile.run_snowflake",
        database="DEV_DB", schema="PASL", table="pasl_policy", source_name="pasl",
    )
    assert resp["error"]["code"] == bridge.APP_ERROR
    assert "Could not connect" in resp["error"]["message"]


def test_canonical_extract_snowflake_schema_shape(monkeypatch):
    """describe_target_table (the one function that opens a real
    connection) is stubbed -- no reachable Snowflake instance in this
    environment. This proves the bridge marshals params and shapes the
    response correctly; extract_canonical_fields' own logic is unit-tested
    in test_snowflake_reader.py."""
    def fake_describe_target_table(database, schema, table):
        return [
            {"name": "POLICY_ID", "snowflake_type": "NUMBER(38,0)", "nullable": False},
            {"name": "EFFECTIVE_DATE", "snowflake_type": "DATE", "nullable": True},
        ]

    monkeypatch.setattr("schema_inference.snowflake_reader.describe_target_table", fake_describe_target_table)

    resp = call(
        "canonical.extract_snowflake_schema",
        database="DEV_DB", schema="SILVER", table="SLV_POLICY",
    )
    result = resp["result"]
    assert result["schema_key"] == "slv_policy"  # defaulted from table name
    assert {f["name"] for f in result["fields"]} == {"policy_id", "effective_date"}
    assert next(f for f in result["fields"] if f["name"] == "policy_id")["required"] is True


def test_canonical_register_dynamic_schema_makes_it_resolvable():
    """Full extract -> register round trip (register step doesn't touch
    Snowflake at all, so no stubbing needed here) -- confirms
    canonical_registry.schema_for_table() reflects the newly registered
    schema for the given table_names immediately, same in-process global
    state map.run/sql.generate_staging_model would see on their next call."""
    from schema_inference.canonical import registry as canonical_registry

    fields = [
        {"name": "policy_id", "target_type": "bigint", "required": True,
         "description": "", "aliases": [], "secondary_target": None},
    ]
    resp = call(
        "canonical.register_dynamic_schema",
        schema_key="test_slv_policy_bridge", fields=fields, table_names=["test_source_table_bridge"],
    )
    assert resp["result"] == {
        "registered": True, "schema_key": "test_slv_policy_bridge",
        "table_names": ["test_source_table_bridge"],
    }
    assert canonical_registry.schema_for_table("test_source_table_bridge") == "test_slv_policy_bridge"
    assert canonical_registry.get_names("test_slv_policy_bridge") == frozenset({"policy_id"})


def test_metamodel_query_loss_runs_never_raises():
    resp = call("metamodel.query_loss_runs", source_name="pasl", limit=5)
    assert "loss_runs" in resp["result"]
    assert isinstance(resp["result"]["metamodel_available"], bool)


def test_tuning_layer0_status_shape():
    resp = call("tuning.layer0_status", source_name="pasl")
    assert set(resp["result"]["active_weights"]) == {"name_sim", "type_compat", "pattern_bonus"}
    assert isinstance(resp["result"]["recent_runs"], list)
    assert isinstance(resp["result"]["metamodel_available"], bool)


def test_tuning_run_layer0_dry_run_against_small_fixture():
    """Real call (cheap, no API cost, no config mutation since apply
    defaults False) -- the function itself is already unit-tested in
    test_tune_rule_weights_layer0.py; this just proves the bridge wraps it
    with the right param names and passes the result through untouched."""
    resp = call("tuning.run_layer0", source_name="pasl", step=0.25)
    result = resp["result"]
    assert result["source_name"] == "pasl"
    assert result["applied"] is False
    assert set(result["best_metrics"]) == {"mean_loss", "f1", "hard_f1"}


def test_tuning_few_shot_stats_shape():
    resp = call("tuning.few_shot_stats", source_name="pasl")
    result = resp["result"]
    assert isinstance(result["active"], list)
    assert result["active_count"] == len(result["active"])
    assert isinstance(result["by_origin"], dict)


def test_tuning_run_layer1_curation_shape():
    """Real call against pasl's actual mapping_history -- curate() is
    already isolation-tested in test_curate_few_shot_bank.py, this just
    proves the bridge wraps it correctly (right param name, dict passed
    through as-is)."""
    resp = call("tuning.run_layer1_curation", source_name="pasl")
    result = resp["result"]
    assert set(result) == {"hard_tp_inserted", "critic_inserted", "skipped_existing", "skipped_no_signature"}


def _isolated_store(tmp_path, monkeypatch):
    """tuning.retire_few_shot_example/prompt_versions/accept_prompt_version
    all call open_store() fresh per handler invocation (from .metamodel.store
    import open_store, inline in each _m_tuning_* body) -- patching the
    source function here is picked up by every one of those inline imports.
    Real metamodel.db is gitignored/local but still persists across test
    runs, which made an earlier version of these tests order-dependent
    (get_active_prompt found a PRIOR run's already-accepted row for the
    same fake agent_name and returned it as "already active" on a fresh
    run) -- isolation, not "harmless clutter," is the correct fix."""
    from schema_inference.metamodel.store import MetamodelStore

    db_path = tmp_path / "metamodel.db"
    monkeypatch.setattr("schema_inference.metamodel.store.open_store", lambda: MetamodelStore(db_path))
    return db_path


def test_tuning_retire_few_shot_example_round_trip(tmp_path, monkeypatch):
    from schema_inference.metamodel.store import MetamodelStore

    db_path = _isolated_store(tmp_path, monkeypatch)
    store = MetamodelStore(db_path)
    try:
        example_id = store.add_few_shot_example(
            source_name="bridge_smoke_test", source_column="FAKE_COL", target_field="policy_id",
            sql_expression="FAKE_COL", reasoning="bridge test fixture", profile_signature={},
            origin="hard_tp",
        )
    finally:
        store.close()

    resp = call("tuning.retire_few_shot_example", example_id=example_id, reason="bridge test cleanup")
    assert resp["result"]["retired"] is True

    second = call("tuning.retire_few_shot_example", example_id=example_id, reason="already retired")
    assert second["result"]["retired"] is False  # not active anymore, 0 rows updated


def test_tuning_prompt_versions_and_accept_round_trip(tmp_path, monkeypatch):
    from schema_inference.metamodel.store import MetamodelStore

    db_path = _isolated_store(tmp_path, monkeypatch)
    store = MetamodelStore(db_path)
    try:
        version_id = store.record_prompt_version(
            agent_name="bridge_smoke_test_agent", prompt_text="candidate prompt text",
            loss_before=0.5, loss_after=0.3,
        )
    finally:
        store.close()

    listed = call("tuning.prompt_versions", agent_name="bridge_smoke_test_agent")
    assert any(v["version_id"] == version_id for v in listed["result"]["versions"])
    assert listed["result"]["active_prompt"] is None  # nothing accepted yet

    accepted = call("tuning.accept_prompt_version", version_id=version_id)
    assert accepted["result"]["accepted"] is True

    after = call("tuning.prompt_versions", agent_name="bridge_smoke_test_agent")
    assert after["result"]["active_prompt"] == "candidate prompt text"


def test_tuning_accept_prompt_version_unknown_id_errors(tmp_path, monkeypatch):
    _isolated_store(tmp_path, monkeypatch)
    resp = call("tuning.accept_prompt_version", version_id="no-such-version-id")
    assert resp["error"]["code"] == bridge.APP_ERROR


def test_tuning_run_layer2_session_streams_tuning_progress_notifications(monkeypatch):
    """Stubs tune_prompts.run_tuning_session (a real session runs the live
    agent pipeline via run_mapping(use_agent=True) for every _run_and_score
    call, not just the diagnosis/proposal LLM steps) -- this test is only
    about the on_round -> tuning.progress notification plumbing, mirroring
    test_map_run_agent_streams_map_progress_notifications."""
    def fake_run_tuning_session(agent_name, source_name, **kwargs):
        on_round = kwargs.get("on_round")
        round_info = {
            "round": 1, "version_id": "fake-version", "loss_before": 0.5,
            "loss_after": 0.3, "improved": True, "regressed": [],
        }
        if on_round:
            on_round(1, round_info)
        return {"baseline_loss": 0.5, "best_loss": 0.3, "best_version_id": "fake-version",
                "rounds": [round_info], "determinism": None}

    monkeypatch.setattr("tools.tune_prompts.run_tuning_session", fake_run_tuning_session)

    notifications = []
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "tuning.run_layer2_session",
        "params": {"agent_name": "mapping", "source_name": "pasl"},
    }
    response = bridge.dispatch(request, notify=lambda method, params: notifications.append((method, params)))

    assert response["result"]["best_version_id"] == "fake-version"
    assert notifications == [("tuning.progress", {"round": 1, "version_id": "fake-version",
                                                    "loss_before": 0.5, "loss_after": 0.3,
                                                    "improved": True, "regressed": []})]


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

    # unmapped_fields' line numbers must point at the field's own "NULL
    # as {field}" line in the file actually written (MAP-7 demo-ready
    # plan phase 4 -- diagnostics squiggles read this).
    written_lines = written_contents.splitlines()
    for entry in first["result"]["unmapped_fields"]:
        assert f"NULL as {entry['field_name']}" in written_lines[entry["line"]]

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
