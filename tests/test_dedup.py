from schema_inference.mapper import _deduplicate
from schema_inference.models import ColumnMapping


def mk(col, target, conf):
    return ColumnMapping(source_column=col, source_table="t", target_field=target,
                          confidence=conf, method="rule", sql_expression=col, notes="")


def test_dedup_clear_winner_demotes_loser():
    """Confidence gap >= TIE_EPSILON: winner keeps the target, loser demoted."""
    mappings = [mk("A", "premium_amount", 0.95), mk("B", "premium_amount", 0.70)]
    resolved, contested = _deduplicate(mappings)
    targets = {m.source_column: m.target_field for m in resolved}
    assert targets == {"A": "premium_amount", "B": None}
    assert contested == []


def test_dedup_near_tie_promotes_to_secondary_target():
    """Near-tie on a field with a secondary_target: both columns legitimately map."""
    mappings = [mk("POL_NO", "policy_id", 0.90), mk("POL_REF", "policy_id", 0.88)]
    resolved, contested = _deduplicate(mappings)
    targets = {m.source_column: m.target_field for m in resolved}
    assert targets == {"POL_NO": "policy_id", "POL_REF": "policy_number"}
    assert contested == []


def test_dedup_near_tie_no_secondary_is_contested():
    """Near-tie on a field with no secondary_target: surfaced as a genuine contest."""
    mappings = [mk("X", "region_code", 0.80), mk("Y", "region_code", 0.79)]
    resolved, contested = _deduplicate(mappings)
    assert len(contested) == 1
    assert contested[0]["target_field"] == "region_code"
    assert set(contested[0]["competing_columns"]) == {"X", "Y"}
    assert contested[0]["provisional_winner"] == "X"
