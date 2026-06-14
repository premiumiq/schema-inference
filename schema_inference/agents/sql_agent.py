"""SQLAgent — finalizes SQL expressions for the mappings that need it.

Runs AFTER the critic. Per Shanth's spec, it only processes mappings where:
  - method == "critic" (the critic overrode the mapping, so its SQL may be rough), OR
  - the sql_expression is a bare column passthrough (no real transformation generated).

Everything else already has good rule-generated SQL and is left untouched.

For each candidate, the agent produces a finalized SQL expression that:
  - uses common_assets. macros where appropriate (dates, cents, flags), and
  - is valid for the target field's type.

Falls back to the rule-based _generate_sql() result on malformed output.

Model: claude-haiku-4-5-20251001. Single batch call.
"""

from __future__ import annotations

import json
import re

from ..canonical.policy import CANONICAL_BY_NAME
from ..mapper import _generate_sql
from ..models import AgentTrace, ColumnMapping

MODEL = "claude-haiku-4-5-20251001"

_SYSTEM_PROMPT = """You are an insurance data engineer finalizing dbt SQL expressions for \
approved column mappings from a legacy PAS-L source to a canonical policy schema.

For each mapping you receive the source column, its target field and type, sample values,
and any encoding flags. Produce the correct SQL expression to transform the source column
into the target field's type.

Use these dbt macros where appropriate:
- YYYYMMDD string date:      {{ common_assets.parse_compact_date('COL') }}
- US date (MM/DD/YYYY):      {{ common_assets.parse_us_date('COL') }}
- Y/N flag to boolean:       {{ common_assets.flag_to_boolean('COL') }}
- Decimal type:              CAST(COL AS {{ common_assets.decimal_type(12, 2) }})
- Integer cents to dollars:  CAST(COL AS {{ common_assets.decimal_type(12, 2) }}) / 100.0

Rules:
- If the source is empty-string-nullable, wrap with NULLIF(COL, '') before casting.
- If the target is null (extended_attributes), just return the bare column name.
- Use the ACTUAL column name, not 'COL'.

Respond with ONLY a JSON object:
{
  "expressions": [
    { "source_column": "<name>", "sql_expression": "<final sql>" }
  ]
}"""


def _needs_sql_agent(m: ColumnMapping) -> bool:
    """True if this mapping should be finalized by the SQL agent."""
    if m.target_field is None:
        return False  # extended_attributes — passthrough is correct, nothing to finalize
    if m.method == "critic":
        return True   # critic overrode it; SQL may be rough
    # passthrough: sql is just the bare column name despite having a target
    if m.sql_expression.strip() == m.source_column.strip():
        return True
    return False


def run_sql_agent(
    mappings: list[ColumnMapping],
    profiles_by_name: dict,
    is_empty_string_null: bool = True,
) -> tuple[list[ColumnMapping], list[AgentTrace]]:
    """Finalize SQL for the mappings that need it. Returns (updated_mappings, traces)."""
    import anthropic

    candidates = [m for m in mappings if _needs_sql_agent(m)]
    if not candidates:
        return mappings, []

    items = []
    for m in candidates:
        prof = profiles_by_name.get(m.source_column)
        field = CANONICAL_BY_NAME.get(m.target_field)
        items.append({
            "source_column": m.source_column,
            "target_field": m.target_field,
            "target_type": field.target_type if field else "string",
            "inferred_type": prof.inferred_type if prof else None,
            "sample_values": prof.sample_values[:5] if prof else [],
            "is_cents_integer": prof.is_cents_integer if prof else None,
            "date_format": prof.date_format if prof else None,
            "is_empty_string_null": is_empty_string_null,
        })

    user_prompt = (
        "Finalize the SQL expression for each mapping:\n\n"
        + json.dumps(items, indent=2)
    )

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=1536,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = "".join(b.text for b in response.content if b.type == "text").strip()
    if "```" in raw:
        fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if fenced:
            raw = fenced[-1]
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end != -1:
        raw = raw[start:end + 1]

    try:
        data = json.loads(raw)
        sql_by_col = {e["source_column"]: e["sql_expression"] for e in data.get("expressions", [])}
    except Exception:
        sql_by_col = {}

    candidate_names = {m.source_column for m in candidates}
    updated: list[ColumnMapping] = []
    traces: list[AgentTrace] = []

    for m in mappings:
        if m.source_column not in candidate_names:
            updated.append(m)
            continue

        new_sql = sql_by_col.get(m.source_column)

        # Validate: must be non-empty and reference the column. Else fall back to rule SQL.
        valid = bool(new_sql) and m.source_column in new_sql
        if not valid:
            field = CANONICAL_BY_NAME.get(m.target_field)
            prof = profiles_by_name.get(m.source_column)
            if field and prof:
                new_sql = _generate_sql(prof, field, is_empty_string_null)
            else:
                new_sql = m.sql_expression  # last resort: keep what we had

        updated.append(ColumnMapping(
            source_column=m.source_column,
            source_table=m.source_table,
            target_field=m.target_field,
            confidence=m.confidence,
            method=m.method,
            sql_expression=new_sql,
            notes=m.notes,
        ))
        traces.append(AgentTrace(
            column_name=m.source_column,
            agent="sql",
            tool_calls=[],
            final_target=m.target_field,
            final_confidence=m.confidence,
            reasoning_summary=f"finalized SQL: {new_sql}",
        ))

    return updated, traces