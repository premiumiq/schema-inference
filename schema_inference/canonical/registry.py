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

# Runtime-registered schemas (MAP-7: "extract target schema from a live
# Snowflake table" bridge feature) — layered on top of the two static dicts
# above, never mutating them. In-memory only, process-lifetime, never
# persisted to a .py file: this is deliberately a live/session mechanism,
# not the "generate a draft, human commits" pattern used elsewhere in this
# project (dbt scaffolding, few-shot bank, prompt tuning) — considered and
# explicitly declined for this feature, since the whole point is to let a
# mapping run target a live table's schema immediately, in the same
# session, without a file-based round trip.
_DYNAMIC_SCHEMAS: dict[str, list[CanonicalField]] = {}
_DYNAMIC_TABLE_SCHEMA: dict[str, str] = {}


def register_dynamic_schema(
    schema_key: str, fields: list[CanonicalField], table_names: list[str]
) -> None:
    """Registers (or re-registers, overwriting) a schema_key for the
    lifetime of this process, and points the given source table_name(s) at
    it. Every caller of schema_for_table()/get_fields() below already
    re-resolves fresh on every call (nothing is cached beyond these dicts
    themselves), so this takes effect immediately for any subsequent
    map_table()/run_mapping()/generate_staging_model_sql() call against
    those table_names — no changes needed in any of them."""
    _DYNAMIC_SCHEMAS[schema_key] = fields
    for t in table_names:
        _DYNAMIC_TABLE_SCHEMA[t] = schema_key


def schema_for_table(table_name: str) -> str:
    """Resolve a table_name to its canonical schema key. Checks dynamically
    registered tables first, then the static TABLE_SCHEMA, then falls back
    to DEFAULT_SCHEMA ('policy') — pre-refactor behavior for anything not
    explicitly onboarded here."""
    if table_name in _DYNAMIC_TABLE_SCHEMA:
        return _DYNAMIC_TABLE_SCHEMA[table_name]
    return TABLE_SCHEMA.get(table_name, DEFAULT_SCHEMA)


def get_fields(schema_key: str) -> list[CanonicalField]:
    if schema_key in _DYNAMIC_SCHEMAS:
        return _DYNAMIC_SCHEMAS[schema_key]
    return _SCHEMAS.get(schema_key, _POLICY_FIELDS)


def get_by_name(schema_key: str) -> dict[str, CanonicalField]:
    return {f.name: f for f in get_fields(schema_key)}


def get_names(schema_key: str) -> frozenset[str]:
    return frozenset(get_by_name(schema_key))
