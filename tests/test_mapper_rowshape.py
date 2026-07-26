from schema_inference.models import SchemaProfile
from schema_inference.mapper import map_table

p = SchemaProfile.model_validate_json(
    open("schema_inference/registry/pasl/profile_pasl_policy.json", encoding="utf-8").read()
)
proposal = map_table(p.tables[0], source_name="pasl")
print("mapper path row_shape:", proposal.row_shape)
