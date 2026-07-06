import os
from schema_inference.models import SchemaProfile, ColumnMapping
from schema_inference.agents.critic_agent import resolve_contests

# Load real profiles so the critic sees actual data shapes
profile = SchemaProfile.model_validate_json(
    open("schema_inference/registry/pasl/profile_pasl_policy.json", encoding="utf-8").read()
)
table = profile.tables[0]
profiles_by_name = {c.name: c for c in table.columns}

# Fabricate a contest: REGN_CD and INS_ST both claiming region_code, near-tie
mappings = [
    ColumnMapping(source_column="REGN_CD", source_table=table.name,
                  target_field="region_code", confidence=0.80,
                  method="rule", sql_expression="REGN_CD", notes=""),
    ColumnMapping(source_column="INS_ST", source_table=table.name,
                  target_field=None, confidence=0.78,
                  method="rule", sql_expression="INS_ST", notes=""),
]
contests = [{
    "target_field": "region_code",
    "competing_columns": ["REGN_CD", "INS_ST"],
    "confidences": {"REGN_CD": 0.80, "INS_ST": 0.78},
    "provisional_winner": "REGN_CD",
}]

print("Sending contest to critic (one Sonnet call, ~10s)...\n")
updated, unresolved = resolve_contests(contests, mappings, profiles_by_name)

print("Result:")
for m in updated:
    print(f"  {m.source_column} -> {m.target_field}")
    if m.notes:
        print(f"     notes: {m.notes}")
print(f"\nUnresolved contests: {len(unresolved)}")
