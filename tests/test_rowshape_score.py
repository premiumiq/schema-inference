import sys
sys.path.insert(0, "scripts")
import yaml
from score_mappings import _score_row_shape, _print_row_shape
from schema_inference.models import SchemaProfile
from schema_inference.agents.row_shape_agent import infer_row_shape

catalog = yaml.safe_load(open("examples/insurance/ground_truth/pasl_schema_catalog.yml", encoding="utf-8"))
gt = catalog.get("row_shape")

p = SchemaProfile.model_validate_json(
    open("schema_inference/registry/pasl/profile_pasl_policy.json", encoding="utf-8").read()
)
proposal = infer_row_shape(p.tables[0], source_name="pasl")

score = _score_row_shape(gt, proposal.model_dump())
_print_row_shape(score, use_color=False)

# Negative control: a deliberately wrong proposal should score badly
wrong = {"natural_key": ["EFF_DT"], "recency_column": "INS_ST", "dedup_strategy": "none"}
print("Negative control (deliberately wrong):")
_print_row_shape(_score_row_shape(gt, wrong), use_color=False)
