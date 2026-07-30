import uuid

from schema_inference.canonical import registry as canonical_registry
from schema_inference.canonical.policy import CanonicalField


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def test_register_dynamic_schema_resolves_for_its_table_names():
    schema_key = _unique("test_schema")
    table_name = _unique("test_table")
    fields = [
        CanonicalField(name="foo", target_type="string", required=True, description=""),
        CanonicalField(name="bar", target_type="integer", required=False, description=""),
    ]

    canonical_registry.register_dynamic_schema(schema_key, fields, [table_name])

    assert canonical_registry.schema_for_table(table_name) == schema_key
    assert canonical_registry.get_fields(schema_key) == fields
    assert set(canonical_registry.get_by_name(schema_key)) == {"foo", "bar"}
    assert canonical_registry.get_names(schema_key) == frozenset({"foo", "bar"})


def test_register_dynamic_schema_does_not_affect_unrelated_static_schemas():
    schema_key = _unique("test_schema")
    table_name = _unique("test_table")
    canonical_registry.register_dynamic_schema(
        schema_key,
        [CanonicalField(name="x", target_type="string", required=False, description="")],
        [table_name],
    )

    # Existing static schema/table resolution must be completely untouched.
    assert canonical_registry.schema_for_table("pasm_coverage") == "pasm_coverage"
    assert canonical_registry.schema_for_table("some_never_registered_table") == "policy"
    policy_names = canonical_registry.get_names("policy")
    assert "policy_id" in policy_names
    assert "x" not in policy_names


def test_register_dynamic_schema_overwrites_cleanly_on_reregistration():
    schema_key = _unique("test_schema")
    table_name = _unique("test_table")

    canonical_registry.register_dynamic_schema(
        schema_key,
        [CanonicalField(name="first_version", target_type="string", required=False, description="")],
        [table_name],
    )
    assert canonical_registry.get_names(schema_key) == frozenset({"first_version"})

    canonical_registry.register_dynamic_schema(
        schema_key,
        [CanonicalField(name="second_version", target_type="string", required=False, description="")],
        [table_name],
    )
    assert canonical_registry.get_names(schema_key) == frozenset({"second_version"})
