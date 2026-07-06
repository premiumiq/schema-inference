from schema_inference.mapper import _deduplicate
from schema_inference.models import ColumnMapping

def mk(col, target, conf):
    return ColumnMapping(source_column=col, source_table="t", target_field=target,
                         confidence=conf, method="rule", sql_expression=col, notes="")

# Case 1: clear winner (gap >= 0.05) -> loser demoted
clear = [mk("A", "premium_amount", 0.95), mk("B", "premium_amount", 0.70)]
# Case 2: near-tie WITH secondary_target (policy_id -> policy_number) -> both promoted
tie_secondary = [mk("POL_NO", "policy_id", 0.90), mk("POL_REF", "policy_id", 0.88)]
# Case 3: near-tie NO secondary -> contested
tie_contested = [mk("X", "region_code", 0.80), mk("Y", "region_code", 0.79)]

for label, ms in [("CLEAR", clear), ("SECONDARY", tie_secondary), ("CONTESTED", tie_contested)]:
    resolved, contested = _deduplicate(ms)
    print(f"\n=== {label} ===")
    for m in resolved:
        print(f"  {m.source_column} -> {m.target_field}")
    if contested:
        print(f"  CONTESTED: {contested}")
