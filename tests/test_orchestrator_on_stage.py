from schema_inference.models import SchemaProfile
from schema_inference.agents.orchestrator import run_mapping


def test_on_stage_fires_at_rule_pass_row_shape_and_done_with_use_agent_false():
    """use_agent=False keeps this deterministic/free (no LLM calls) --
    mirrors test_orchestrator_rowshape.py's rationale. critic_agent/
    sql_agent stages are gated on use_agent=True so they're absent here;
    rule_pass/mapping_agent/row_shape/done fire regardless of use_agent
    (mapping_agent reports 0 columns processed when skipped, not absent)."""
    p = SchemaProfile.model_validate_json(
        open("schema_inference/registry/pasl/profile_pasl_policy.json", encoding="utf-8").read()
    )
    table = p.tables[0]

    seen: list[tuple[str, dict]] = []
    run_mapping(
        table,
        source_name="pasl",
        use_agent=False,
        record_to_metamodel=False,
        on_stage=lambda stage, info: seen.append((stage, info)),
    )

    stages = [s for s, _ in seen]
    assert stages == ["rule_pass", "mapping_agent", "row_shape", "done"]
    rule_pass_info = dict(seen[0][1])
    assert rule_pass_info["columns"] > 0
    mapping_agent_info = dict(seen[1][1])
    assert mapping_agent_info["columns"] == 0


def test_on_stage_none_is_a_no_op():
    p = SchemaProfile.model_validate_json(
        open("schema_inference/registry/pasl/profile_pasl_policy.json", encoding="utf-8").read()
    )
    table = p.tables[0]
    # Must not raise -- on_stage defaults to None for every existing caller.
    run_mapping(table, source_name="pasl", use_agent=False, record_to_metamodel=False)
