from datetime import datetime

from schema_inference.models import ApprovedMapping, MappingDefinition, MissingFieldResolution
from schema_inference.sql_scaffold import generate_staging_model_sql


def am(source_column, target_field, sql=None, action="accepted"):
    return ApprovedMapping(
        source_column=source_column, source_table="t", target_field=target_field,
        sql_expression=sql or source_column, confidence=0.9, method="rule", notes="",
        reviewer_action=action,
    )


def test_generated_sql_has_source_and_renamed_ctes_with_mapped_columns():
    definition = MappingDefinition(
        source_name="pasl",
        table_name="pasl_policy",
        approved_mappings=[
            am("POL_NO", "policy_id", sql="CAST(POL_NO AS INTEGER)"),
            am("POL_NO", "policy_number", sql="POL_NO"),
        ],
        extended_attributes=["WRTG_AGT"],
        missing_field_resolutions=[],
        reviewer_identity="test-reviewer",
        reviewed_at=datetime(2026, 1, 1, 12, 0, 0),
        profile_hash="abc123",
    )

    sql = generate_staging_model_sql(definition)

    assert "with source as" in sql
    assert "select * from {{ source('pasl', 'pasl_policy') }}" in sql
    assert "renamed as" in sql
    assert "CAST(POL_NO AS INTEGER) as policy_id" in sql
    assert "POL_NO as policy_number" in sql
    assert "object_construct('WRTG_AGT', WRTG_AGT) as extended_attributes" in sql
    assert sql.strip().endswith("select * from renamed")


def test_skipped_mapping_target_field_none_does_not_appear_as_approved():
    definition = MappingDefinition(
        source_name="pasl", table_name="pasl_policy",
        approved_mappings=[am("X", None, action="skipped")],
        extended_attributes=["X"],
        missing_field_resolutions=[],
        reviewer_identity="r", reviewed_at=datetime(2026, 1, 1), profile_hash="",
    )
    sql = generate_staging_model_sql(definition)
    # No canonical field should pick up "X" as its expression.
    assert "X as policy_id" not in sql
    assert "object_construct('X', X) as extended_attributes" in sql


def test_missing_field_resolutions_rendered_per_kind():
    definition = MappingDefinition(
        source_name="pasl", table_name="pasl_policy",
        approved_mappings=[],
        extended_attributes=[],
        missing_field_resolutions=[
            MissingFieldResolution(target_field="policy_id", resolution="NULL"),
            MissingFieldResolution(target_field="region_code", resolution="HARDCODED", hardcoded_value="TX"),
            MissingFieldResolution(target_field="premium_amount", resolution="DERIVED", derivation_sql="0"),
        ],
        reviewer_identity="r", reviewed_at=datetime(2026, 1, 1), profile_hash="",
    )
    sql = generate_staging_model_sql(definition)
    assert "NULL as policy_id" in sql
    assert "'TX' as region_code" in sql
    assert "0 as premium_amount" in sql


def test_unmapped_field_with_no_resolution_defaults_to_null():
    definition = MappingDefinition(
        source_name="pasl", table_name="pasl_policy",
        approved_mappings=[], extended_attributes=[], missing_field_resolutions=[],
        reviewer_identity="r", reviewed_at=datetime(2026, 1, 1), profile_hash="",
    )
    sql = generate_staging_model_sql(definition)
    assert "NULL as policy_id" in sql
    assert "NULL as extended_attributes" in sql


def test_hardcoded_value_with_quote_is_escaped():
    definition = MappingDefinition(
        source_name="pasl", table_name="pasl_policy",
        approved_mappings=[], extended_attributes=[],
        missing_field_resolutions=[
            MissingFieldResolution(target_field="region_code", resolution="HARDCODED", hardcoded_value="O'Fallon"),
        ],
        reviewer_identity="r", reviewed_at=datetime(2026, 1, 1), profile_hash="",
    )
    sql = generate_staging_model_sql(definition)
    assert "'O''Fallon' as region_code" in sql
