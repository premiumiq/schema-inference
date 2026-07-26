import os

import pytest

from schema_inference.models import SchemaProfile, ColumnMapping
from schema_inference.agents.critic_agent import resolve_contests

pytestmark = pytest.mark.anthropic


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="requires ANTHROPIC_API_KEY")
def test_resolve_contests_picks_the_column_with_matching_data():
    """MAP-3: REGN_CD and INS_ST both claim region_code, near-tie. The critic should
    pick REGN_CD (the column whose actual data profile fits a region code) over
    INS_ST, not just the higher provisional confidence."""
    profile = SchemaProfile.model_validate_json(
        open("schema_inference/registry/pasl/profile_pasl_policy.json", encoding="utf-8").read()
    )
    table = profile.tables[0]
    profiles_by_name = {c.name: c for c in table.columns}

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

    updated, unresolved = resolve_contests(contests, mappings, profiles_by_name)

    by_col = {m.source_column: m.target_field for m in updated}
    assert by_col["REGN_CD"] == "region_code"
    assert by_col["INS_ST"] is None
    assert unresolved == []
