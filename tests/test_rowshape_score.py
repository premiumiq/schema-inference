import sys
sys.path.insert(0, "scripts")

import yaml

from score_mappings import _score_row_shape
from schema_inference.models import SchemaProfile
from schema_inference.agents.row_shape_agent import infer_row_shape


def test_row_shape_scoring_perfect_and_wrong_proposals():
    catalog = yaml.safe_load(
        open("examples/insurance/ground_truth/pasl_schema_catalog.yml", encoding="utf-8")
    )
    gt = catalog.get("row_shape")

    p = SchemaProfile.model_validate_json(
        open("schema_inference/registry/pasl/profile_pasl_policy.json", encoding="utf-8").read()
    )
    proposal = infer_row_shape(p.tables[0], source_name="pasl")

    score = _score_row_shape(gt, proposal.model_dump())
    assert score is not None
    assert score.natural_key_correct is True
    assert score.recency_correct is True
    assert score.strategy_correct is True
    assert score.loss == 0.0

    # Negative control: a deliberately wrong proposal should score worse on every axis.
    wrong = {"natural_key": ["EFF_DT"], "recency_column": "INS_ST", "dedup_strategy": "none"}
    wrong_score = _score_row_shape(gt, wrong)
    assert wrong_score.natural_key_correct is False
    assert wrong_score.recency_correct is False
    assert wrong_score.strategy_correct is False
    assert wrong_score.loss == 1.0
