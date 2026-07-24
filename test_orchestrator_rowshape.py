from schema_inference.models import SchemaProfile
from schema_inference.agents.orchestrator import run_mapping

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
print("row_shape on proposal:", rs)
