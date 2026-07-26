from schema_inference.models import SchemaProfile
from schema_inference.mapper import map_table


def test_map_table_attaches_row_shape():
    """map_table() (the legacy non-agent path) should attach the same MAP-5
    row-shape proposal as the agent orchestrator path. use_llm=False keeps
    this deterministic/free — row_shape inference doesn't touch the LLM pass."""
    p = SchemaProfile.model_validate_json(
        open("schema_inference/registry/pasl/profile_pasl_policy.json", encoding="utf-8").read()
    )
    proposal = map_table(p.tables[0], source_name="pasl", use_llm=False)

    assert proposal.row_shape is not None
    assert proposal.row_shape["natural_key"] == ["POL_NO"]
    assert proposal.row_shape["recency_column"] == "POL_NO_SEQ"
    assert proposal.row_shape["dedup_strategy"] == "row_number"
