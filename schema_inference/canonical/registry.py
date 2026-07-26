"""Registry of canonical target schemas, keyed by schema key.

Each source table maps to exactly one canonical schema. 'policy' (canonical/policy.py)
is the original/default schema mirroring slv_policy; additional schemas onboard as
more source tables get ground-truth coverage (see docs/mapper-agent-roadmap.md's
"PAS-M other tables cataloged" gap). Any table_name not in TABLE_SCHEMA falls back
to 'policy' — this is what preserves every pre-existing table's mapping behavior
unchanged as new schemas are added here.
"""

from __future__ import annotations

from .pasm_coverage import CANONICAL_FIELDS as _PASM_COVERAGE_FIELDS
from .policy import CANONICAL_FIELDS as _POLICY_FIELDS
from .policy import CanonicalField

DEFAULT_SCHEMA = "policy"

_SCHEMAS: dict[str, list[CanonicalField]] = {
    "policy": _POLICY_FIELDS,
    "pasm_coverage": _PASM_COVERAGE_FIELDS,
}

# table_name -> schema key.
TABLE_SCHEMA: dict[str, str] = {
    "pasm_coverage": "pasm_coverage",
}


def schema_for_table(table_name: str) -> str:
    """Resolve a table_name to its canonical schema key. Unknown tables fall
    back to DEFAULT_SCHEMA ('policy') — pre-refactor behavior for anything
    not explicitly onboarded here."""
    return TABLE_SCHEMA.get(table_name, DEFAULT_SCHEMA)


def get_fields(schema_key: str) -> list[CanonicalField]:
    return _SCHEMAS.get(schema_key, _POLICY_FIELDS)


def get_by_name(schema_key: str) -> dict[str, CanonicalField]:
    return {f.name: f for f in get_fields(schema_key)}


def get_names(schema_key: str) -> frozenset[str]:
    return frozenset(get_by_name(schema_key))
