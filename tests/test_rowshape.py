from schema_inference.models import SchemaProfile
from schema_inference.agents.row_shape_agent import infer_row_shape

p = SchemaProfile.model_validate_json(
    open("schema_inference/registry/pasl/profile_pasl_policy.json", encoding="utf-8").read()
)
table = p.tables[0]

proposal = infer_row_shape(table, source_name="pasl")

print("=== RowShape proposal for PAS-L ===")
print("  natural_key:    ", proposal.natural_key)
print("  recency_column: ", proposal.recency_column)
print("  dedup_strategy: ", proposal.dedup_strategy)
print("  dedup_pattern:  ", proposal.dedup_pattern)
print("  confidence:     ", proposal.confidence)
print("  reasoning:      ", proposal.reasoning)
print()
print("Ground truth: natural_key=[POL_NO], recency=POL_NO_SEQ,")
print("  dedup=ROW_NUMBER() OVER (PARTITION BY POL_NO ORDER BY POL_NO_SEQ DESC)")
