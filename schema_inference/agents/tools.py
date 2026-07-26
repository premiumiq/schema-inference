"""Agent tools — pure Python functions the agents call during the mapping loop.

Each tool is registered as an Anthropic tool schema (see TOOL_SCHEMAS at bottom).
No I/O except reading the ground-truth value catalog and schema catalog once.

The six tools (per Shanth's spec):
  lookup_canonical(query)              -> top-3 canonical fields
  check_value_catalog(column_name)     -> value catalog entry or null
  score_name_similarity(source, target)-> float 0-1 (wraps rapidfuzz logic)
  get_column_profile(column_name)      -> ColumnProfile dict
  get_hard_columns()                   -> list of is_hard column names
  generate_sql(column_name, target_field, col_profile) -> SQL expression string
"""

from __future__ import annotations

import json
import os
from functools import lru_cache

import yaml
from rapidfuzz import fuzz

from ..canonical import registry as canonical_registry
from ..mapper import _generate_sql, _name_similarity
from ..models import ColumnProfile

# ─── Catalog file locations (repo-root/ground_truth/) ─────────────────────────
# Catalogs are per-source (pasl_*, pasm_*, ...) — which one to load is driven by
# the _SOURCE_NAME_VAR context set by register_profiles(), NOT hardcoded, so
# these tools return the right source's data instead of always PAS-L's.

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CATALOG_DIR = os.environ.get("SCHEMA_INFERENCE_CATALOG_DIR") or os.path.join(
    _REPO_ROOT, "examples", "insurance", "ground_truth"
)


@lru_cache(maxsize=8)
def _load_value_catalog(source_name: str) -> dict:
    """Load source_name's value catalog once. Returns the full dict (keys:
    version, source, table, columns), or {} if this source has none yet."""
    path = os.path.join(_CATALOG_DIR, f"{source_name}_value_catalog.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=8)
def _load_schema_catalog(source_name: str) -> dict:
    """Load source_name's schema catalog (scoring answer key), or {} if this
    source has none yet. Used ONLY for get_hard_columns — never to read
    canonical_target (that would leak the answer to the agent)."""
    path = os.path.join(_CATALOG_DIR, f"{source_name}_schema_catalog.yml")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ─── Module-level registry of run-scoped state ─────────────────────────────────
# The orchestrator populates this before invoking agents so get_column_profile,
# generate_sql, lookup_canonical etc. can look up the live run's data.

import contextvars

# Per-context state (thread-safe and async-task-safe, unlike a plain module global).
# Each execution context — thread, async task, or concurrent run — gets its own copy,
# so concurrent mapping runs cannot clobber each other's profiles.
_COLUMN_PROFILES_VAR: contextvars.ContextVar[dict[str, ColumnProfile]] = \
    contextvars.ContextVar("column_profiles", default={})
_IS_EMPTY_STRING_NULL_VAR: contextvars.ContextVar[bool] = \
    contextvars.ContextVar("is_empty_string_null", default=True)
_SOURCE_NAME_VAR: contextvars.ContextVar[str] = \
    contextvars.ContextVar("source_name", default="pasl")
_CANONICAL_SCHEMA_VAR: contextvars.ContextVar[str] = \
    contextvars.ContextVar("canonical_schema", default=canonical_registry.DEFAULT_SCHEMA)


def register_profiles(
    columns: list[ColumnProfile],
    is_empty_string_null: bool = True,
    source_name: str = "pasl",
    canonical_schema: str = canonical_registry.DEFAULT_SCHEMA,
) -> None:
    """Called by the orchestrator before the agent loop. Stores profiles and
    run context for tool lookups in context-local variables (safe under
    concurrency)."""
    _COLUMN_PROFILES_VAR.set({c.name: c for c in columns})
    _IS_EMPTY_STRING_NULL_VAR.set(is_empty_string_null)
    _SOURCE_NAME_VAR.set(source_name)
    _CANONICAL_SCHEMA_VAR.set(canonical_schema)


# ─── Tool 1: lookup_canonical ─────────────────────────────────────────────────

def lookup_canonical(query: str) -> list[dict]:
    """Return the top-3 canonical fields most similar to the query string.

    Args:
        query: a source column name or descriptive phrase.
    Returns:
        list of up to 3 dicts: {name, type, required, description, aliases}
    """
    scored = []
    q = query.lower().replace("_", " ")
    for field in canonical_registry.get_fields(_CANONICAL_SCHEMA_VAR.get()):
        best = 0.0
        for name in field.all_names():
            tgt = name.lower().replace("_", " ")
            r = max(fuzz.ratio(q, tgt), fuzz.token_set_ratio(q, tgt)) / 100.0
            best = max(best, r)
        scored.append((best, field))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {
            "name": f.name,
            "type": f.target_type,
            "required": f.required,
            "description": f.description,
            "aliases": f.aliases[:6],
            "similarity": round(score, 3),
        }
        for score, f in scored[:3]
    ]


# ─── Tool 2: check_value_catalog ──────────────────────────────────────────────

def check_value_catalog(column_name: str) -> dict | None:
    """Return the value catalog entry for a source column, or None if not present.

    The entry includes type, format, valid_values, value_map, transformation, notes
    and any seeded defects. This is the agent's reference for hard columns
    (e.g. discovering ANNU_PREM_AMT is integer_cents before mapping it).
    """
    catalog = _load_value_catalog(_SOURCE_NAME_VAR.get())
    return catalog.get("columns", {}).get(column_name)


# ─── Tool 3: score_name_similarity ────────────────────────────────────────────

def score_name_similarity(source: str, target: str) -> float:
    """Return rapidfuzz name-similarity 0-1 between a source column and a canonical field.

    Wraps the existing _name_similarity logic from mapper.py so the agent uses the
    same scoring the rule engine does.
    """
    field = canonical_registry.get_by_name(_CANONICAL_SCHEMA_VAR.get()).get(target)
    if field is None:
        # target not a known canonical field; fall back to raw string compare
        s = source.lower().replace("_", " ")
        t = target.lower().replace("_", " ")
        return round(max(fuzz.ratio(s, t), fuzz.token_set_ratio(s, t)) / 100.0, 4)
    return round(_name_similarity(source, field), 4)


# ─── Tool 4: get_column_profile ───────────────────────────────────────────────

def get_column_profile(column_name: str) -> dict | None:
    """Return the ColumnProfile for a source column as a dict, or None if unknown."""
    col = _COLUMN_PROFILES_VAR.get().get(column_name)
    return col.model_dump() if col else None


# ─── Tool 5: get_hard_columns ─────────────────────────────────────────────────

def get_hard_columns() -> list[str]:
    """Return the list of column names flagged is_hard=true in the schema catalog.

    NOTE: reads only the is_hard flag, never canonical_target (which is the answer key).
    """
    catalog = _load_schema_catalog(_SOURCE_NAME_VAR.get())
    cols = catalog.get("columns", {})
    return [name for name, meta in cols.items() if meta and meta.get("is_hard") is True]


# ─── Tool 6: generate_sql ─────────────────────────────────────────────────────

def generate_sql(column_name: str, target_field: str, col_profile: dict | None = None) -> str:
    """Generate the dbt-macro-aware SQL expression for a source->target mapping.

    Wraps the existing _generate_sql from mapper.py. Uses the registered live
    profile if col_profile is not supplied.
    """
    field = canonical_registry.get_by_name(_CANONICAL_SCHEMA_VAR.get()).get(target_field)
    if field is None:
        return column_name  # no known target; passthrough

    if col_profile is not None:
        col = ColumnProfile(**col_profile)
    else:
        col = _COLUMN_PROFILES_VAR.get().get(column_name)
        if col is None:
            return column_name

    return _generate_sql(col, field, _IS_EMPTY_STRING_NULL_VAR.get())


# ─── Anthropic tool schemas ───────────────────────────────────────────────────
# These describe each tool to Claude so it knows when and how to call them.

TOOL_SCHEMAS = [
    {
        "name": "lookup_canonical",
        "description": (
            "Search the canonical insurance policy schema for the fields most similar "
            "to a query. Use this to find candidate target fields for a source column. "
            "Returns the top 3 fields with their name, type, required flag, description, "
            "and known aliases."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A source column name or descriptive phrase to match.",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "check_value_catalog",
        "description": (
            "Look up a source column in the ground-truth value catalog. Returns the "
            "column's type, format, valid values, value map, transformation hint, and any "
            "seeded data-quality defects, or null if the column is not catalogued. "
            "Use this to discover non-obvious facts such as integer-cents encoding, "
            "coded value maps, or that a code is a string rather than a numeric ID."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "column_name": {
                    "type": "string",
                    "description": "The exact source column name (e.g. 'ANNU_PREM_AMT').",
                }
            },
            "required": ["column_name"],
        },
    },
    {
        "name": "score_name_similarity",
        "description": (
            "Return a 0-1 name-similarity score between a source column and a canonical "
            "field name, using the same fuzzy matching the rule engine uses. Use this to "
            "compare how well a source column name matches a candidate target field."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Source column name."},
                "target": {"type": "string", "description": "Canonical field name."},
            },
            "required": ["source", "target"],
        },
    },
    {
        "name": "get_column_profile",
        "description": (
            "Return the profiler's analysis of a source column: inferred type, null rate, "
            "distinct count, sample values, value distribution, and flags like "
            "is_id_column, is_coded_column, is_cents_integer, and date_format."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "column_name": {
                    "type": "string",
                    "description": "The source column name to profile.",
                }
            },
            "required": ["column_name"],
        },
    },
    {
        "name": "get_hard_columns",
        "description": (
            "Return the list of source columns known to be hard to map correctly "
            "(requiring domain knowledge or non-obvious transformations). Use this to "
            "decide whether a column needs extra scrutiny."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "generate_sql",
        "description": (
            "Generate the dbt-macro-aware SQL expression for a confirmed source-to-target "
            "mapping. Handles date parsing, cents-to-dollars conversion, prefix stripping, "
            "boolean flags, and null handling. Call this only after you have decided on the "
            "target field."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "column_name": {"type": "string", "description": "Source column name."},
                "target_field": {
                    "type": "string",
                    "description": "Canonical field name the column maps to.",
                },
            },
            "required": ["column_name", "target_field"],
        },
    },
]

# Dispatch table: tool name -> callable
TOOL_DISPATCH = {
    "lookup_canonical": lookup_canonical,
    "check_value_catalog": check_value_catalog,
    "score_name_similarity": score_name_similarity,
    "get_column_profile": get_column_profile,
    "get_hard_columns": get_hard_columns,
    "generate_sql": generate_sql,
}
