"""Pydantic models — single source of truth for all schema inference data structures."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# ─── Profiler output ──────────────────────────────────────────────────────────

class ColumnProfile(BaseModel):
    name: str
    inferred_type: Literal["string", "integer", "decimal", "date", "boolean"]
    null_rate: float                        # 0.0–1.0; empty string counts as null
    distinct_count: int                     # capped at DISTINCT_CAP (1000) in profiler
    sample_values: list[str]                # up to 10 non-null, non-empty representative values
    value_distribution: dict[str, int]      # top-5 value → count (empty dict if >20 distinct)
    date_format: str | None = None          # "YYYYMMDD" | "ISO8601" | "US" | None
    is_id_column: bool = False              # name matches *_ID / *_NO / *_NBR / *_SEQ
    is_coded_column: bool = False           # distinct_count < 20 and short string values
    is_cents_integer: bool = False          # integer column whose name contains AMT/PREM/LIM/DED


class TableProfile(BaseModel):
    name: str
    row_count: int
    columns: list[ColumnProfile]
    delimiter: str                          # "|" or ","
    source_file: str                        # original filename
    is_empty_string_null: bool = True       # True for PAS-L style files


class SchemaProfile(BaseModel):
    source_name: str
    tables: list[TableProfile]
    profiled_at: datetime
    profile_hash: str                       # SHA256 of sorted column fingerprint


# ─── Mapper output ────────────────────────────────────────────────────────────

class ColumnMapping(BaseModel):
    source_column: str
    source_table: str
    target_field: str | None = None         # None = route to extended_attributes
    confidence: float
    method: Literal["rule", "llm", "manual"]
    sql_expression: str
    notes: str
    name_similarity: float = 0.0            # stored for reviewer display
    type_compatibility: float = 0.0
    pattern_bonus: float = 0.0


class MappingProposal(BaseModel):
    source_name: str
    table_name: str
    mappings: list[ColumnMapping]
    unmapped_columns: list[str]             # routed to extended_attributes
    missing_standard_fields: list[str]      # slv_policy fields with no source match
    excluded_metadata_columns: list[str]    # _CDC_* columns excluded from mapping


# ─── Reviewer output ─────────────────────────────────────────────────────────

class ApprovedMapping(BaseModel):
    source_column: str
    source_table: str
    target_field: str | None = None
    sql_expression: str
    confidence: float
    method: Literal["rule", "llm", "manual"]
    notes: str
    reviewer_action: Literal["auto_approved", "accepted", "modified", "skipped"]


class MissingFieldResolution(BaseModel):
    target_field: str
    resolution: Literal["NULL", "HARDCODED", "DERIVED"]
    hardcoded_value: str | None = None
    derivation_sql: str | None = None


class MappingDefinition(BaseModel):
    source_name: str
    table_name: str
    approved_mappings: list[ApprovedMapping]
    extended_attributes: list[str]          # source columns routed to JSON blob
    missing_field_resolutions: list[MissingFieldResolution]
    reviewer_identity: str
    reviewed_at: datetime
    profile_hash: str                       # links back to SchemaProfile version


# ─── Tracker storage ─────────────────────────────────────────────────────────

class ColumnFingerprint(BaseModel):
    name: str
    inferred_type: str
    is_id_column: bool
    is_coded_column: bool
    date_format: str | None


class SchemaVersion(BaseModel):
    source_name: str
    version: int
    fingerprint_hash: str
    columns: list[ColumnFingerprint]
    recorded_at: datetime
    linked_mapping: str | None = None       # path to approved MappingDefinition JSON


class ColumnChange(BaseModel):
    change_type: Literal["added", "removed", "renamed", "type_changed"]
    column_name: str
    old_value: str | None = None            # old type or old name (for renames)
    new_value: str | None = None            # new type or new name (for renames)
    rename_similarity: float | None = None  # rapidfuzz score when change_type == "renamed"
    is_breaking: bool = False               # removed or type_changed = True


class SchemaChangeReport(BaseModel):
    source_name: str
    from_version: int
    to_version: int
    changes: list[ColumnChange]
    has_breaking_changes: bool
    new_columns_for_mapping: list[str]      # added columns to route through mapper


# ─── Agent workflow output ────────────────────────────────────────────────────

class AgentToolCall(BaseModel):
    tool_name: str
    inputs: dict
    output: str

class AgentTrace(BaseModel):
    column_name: str
    agent: Literal["mapping", "critic", "sql"]
    tool_calls: list[AgentToolCall]
    final_target: str | None = None
    final_confidence: float
    reasoning_summary: str

class AgentMappingRun(BaseModel):
    run_id: str
    source_name: str
    table_name: str
    proposal: MappingProposal
    traces: list[AgentTrace]
    rule_pass_count: int
    agent_pass_count: int
    critic_overrides: int
    eval_score: dict | None = None
    started_at: datetime
    duration_seconds: float