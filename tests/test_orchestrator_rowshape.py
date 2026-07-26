from schema_inference.models import SchemaProfile
from schema_inference.agents.orchestrator import run_mapping


def test_orchestrator_attaches_row_shape():
    """run_mapping() (the --agent pipeline path) should attach the same MAP-5
    row-shape proposal as the legacy mapper.map_table() path. use_agent=False
    keeps this deterministic/free — row_shape inference doesn't touch the LLM
    pass, and record_to_metamodel=False keeps it out of mapping_history."""
    p = SchemaProfile.model_validate_json(
        open("schema_inference/registry/pasl/profile_pasl_policy.json", encoding="utf-8").read()
    )
    table = p.tables[0]

    run = run_mapping(
        table,
        source_name="pasl",
        use_agent=False,
        record_to_metamodel=False,
    )

    rs = run.proposal.row_shape
    assert rs is not None
    assert rs["natural_key"] == ["POL_NO"]
    assert rs["recency_column"] == "POL_NO_SEQ"
    assert rs["dedup_strategy"] == "row_number"
