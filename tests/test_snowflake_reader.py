"""No Snowflake connection needed -- _map_snowflake_type and
extract_canonical_fields are pure functions operating on plain data.
describe_target_table (the one function that does connect) is exercised
only indirectly, via bridge tests that stub it out -- no reachable
Snowflake instance in this environment."""

from schema_inference.snowflake_reader import _map_snowflake_type, extract_canonical_fields


def test_map_snowflake_type_number_zero_scale_is_integer_or_bigint():
    assert _map_snowflake_type("NUMBER(9,0)") == "integer"
    assert _map_snowflake_type("NUMBER(38,0)") == "bigint"


def test_map_snowflake_type_number_with_scale_is_decimal():
    assert _map_snowflake_type("NUMBER(10,2)") == "decimal"
    assert _map_snowflake_type("DECIMAL(5,1)") == "decimal"


def test_map_snowflake_type_string_family():
    assert _map_snowflake_type("VARCHAR(16777216)") == "string"
    assert _map_snowflake_type("CHAR(10)") == "string"
    assert _map_snowflake_type("TEXT") == "string"


def test_map_snowflake_type_date_family():
    assert _map_snowflake_type("DATE") == "date"
    assert _map_snowflake_type("TIMESTAMP_NTZ(9)") == "date"


def test_map_snowflake_type_boolean():
    assert _map_snowflake_type("BOOLEAN") == "boolean"


def test_map_snowflake_type_float_family_is_decimal():
    assert _map_snowflake_type("FLOAT") == "decimal"
    assert _map_snowflake_type("DOUBLE") == "decimal"


def test_map_snowflake_type_unknown_falls_back_to_string():
    assert _map_snowflake_type("VARIANT") == "string"
    assert _map_snowflake_type("GEOGRAPHY") == "string"


def test_extract_canonical_fields_shape_and_required_from_nullable():
    columns = [
        {"name": "POLICY_ID", "snowflake_type": "NUMBER(38,0)", "nullable": False},
        {"name": "EFFECTIVE_DATE", "snowflake_type": "DATE", "nullable": True},
    ]
    fields = extract_canonical_fields(columns)

    assert fields[0] == {
        "name": "policy_id", "target_type": "bigint", "required": True,
        "description": "", "aliases": [], "secondary_target": None,
    }
    assert fields[1]["name"] == "effective_date"
    assert fields[1]["target_type"] == "date"
    assert fields[1]["required"] is False
