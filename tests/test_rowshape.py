from schema_inference.models import SchemaProfile
from schema_inference.agents.row_shape_agent import infer_row_shape


def test_infer_row_shape_matches_ground_truth():
    """RowShapeAgent's deterministic heuristics should recover PAS-L's known
    row shape from profile stats alone: natural_key=[POL_NO], recency=POL_NO_SEQ,
    dedup=ROW_NUMBER() OVER (PARTITION BY POL_NO ORDER BY POL_NO_SEQ DESC)."""
    p = SchemaProfile.model_validate_json(
        open("schema_inference/registry/pasl/profile_pasl_policy.json", encoding="utf-8").read()
    )
    table = p.tables[0]

    proposal = infer_row_shape(table, source_name="pasl")

    assert proposal.natural_key == ["POL_NO"]
    assert proposal.recency_column == "POL_NO_SEQ"
    assert proposal.dedup_strategy == "row_number"
    assert proposal.confidence > 0.0
