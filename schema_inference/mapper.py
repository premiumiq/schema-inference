"""Mapping Engine — rule-based + LLM pass.

Rule-based pass (rapidfuzz):
  confidence = 0.65 * name_sim + 0.25 * type_compat + 0.10 * pattern_bonus

LLM pass (Claude Haiku, batches of ≤20):
  Triggered for columns with rule-based confidence < llm_threshold (default 0.70).
  Columns below 0.50 after LLM also surface in reviewer as low-confidence.

_CDC_* columns are excluded from all mapping and recorded in excluded_metadata_columns.
"""

from __future__ import annotations

import json
import os
import re
from typing import TYPE_CHECKING

from rapidfuzz import fuzz

from .canonical.policy import CANONICAL_BY_NAME, CANONICAL_FIELDS, CANONICAL_NAMES
from .models import ColumnMapping, ColumnProfile, MappingProposal, TableProfile

if TYPE_CHECKING:
    from .canonical.policy import CanonicalField

# Columns matching this pattern are metadata, not business data
_CDC_RE = re.compile(r"^_CDC_", re.IGNORECASE)

# Pattern recognition for bonus scoring
_DATE_SUFFIX_RE = re.compile(r"(_DT|_DATE|_DAT)$", re.IGNORECASE)
_AMOUNT_SUFFIX_RE = re.compile(r"(_AMT|_PREM|_LIM|_DED|_VAL|_REV)$", re.IGNORECASE)
_ID_SUFFIX_RE = re.compile(r"(_ID|_NO|_NBR|_SEQ|_NUM|_KEY|_REF)$", re.IGNORECASE)
_FLAG_SUFFIX_RE = re.compile(r"(_FLG|_FLAG|_IND)$", re.IGNORECASE)
_CODE_SUFFIX_RE = re.compile(r"(_CD|_CODE|_STAT|_TYP|_TYPE)$", re.IGNORECASE)

# Prefix detection: 2-5 uppercase letters followed by '-'
_PREFIX_RE = re.compile(r"^([A-Z]{2,5})-")

LLM_BATCH_SIZE = 20
DEFAULT_LLM_THRESHOLD = 0.70

_SYSTEM_PROMPT = """You are an insurance data engineering assistant specializing in mapping \
source system columns to a canonical insurance policy schema.

INSURANCE DOMAIN GLOSSARY:
- PAS: Policy Administration System
- POL_NO / POL_NUM / SYS_POL_ID: policy identifier
- EFF_DT / EXP_DT: effective / expiration date
- WRT_PREM / TOT_PREM_AMT: written premium (integer cents in legacy PAS-L, decimal dollars in PAS-M)
- POL_STAT_CD: A=Active, C=Cancelled, X=Expired, P=Pending
- LOB_CD: line of business (PA=Personal Auto, HO=Homeowners, CGL=Commercial GL, WC=Workers Comp)
- INSRD: insured (customer/policyholder)
- AGCY/AGT: agency / producing agent
- DIST_CHNL: distribution channel
- REGN/TERR: region / territory
- DED: deductible  LIM: limit  COV: coverage  PREM/AMT: premium/amount
- FLG: boolean flag (Y/N in PAS-L, true/false strings in PAS-M)
- CD: code field  NM: name  DT: date  AMT: monetary amount  PCT: percent

Respond with valid JSON only. No explanation outside the JSON structure."""


def _normalize(name: str) -> str:
    return name.lstrip("_").lower().replace("_", " ")


def _name_similarity(source_col: str, field: "CanonicalField") -> float:
    src = _normalize(source_col)
    best = 0.0
    for target_name in field.all_names():
        tgt = _normalize(target_name)
        r1 = fuzz.ratio(src, tgt) / 100.0
        r2 = fuzz.token_set_ratio(src, tgt) / 100.0
        best = max(best, r1, r2)
    return best


def _type_compatibility(
    source_type: str, target_type: str, col: ColumnProfile
) -> float:
    if source_type == target_type:
        return 1.0

    compat_map = {
        ("integer", "decimal"): 0.85,     # cents conversion
        ("integer", "bigint"): 0.90,
        ("decimal", "integer"): 0.60,
        ("string", "string"): 1.0,
        ("string", "integer"): 0.75 if col.is_id_column else 0.40,
        ("string", "bigint"): 0.70 if col.is_id_column else 0.35,
        ("string", "date"): 0.70 if col.date_format else 0.25,
        ("string", "boolean"): 0.75 if col.is_coded_column else 0.30,
        ("boolean", "string"): 0.60,
        ("integer", "string"): 0.50,
    }
    return compat_map.get((source_type, target_type), 0.20)


def _pattern_bonus(source_col: str, field: "CanonicalField") -> float:
    col_upper = source_col.upper()
    ttype = field.target_type
    if ttype == "date" and _DATE_SUFFIX_RE.search(col_upper):
        return 1.0
    if ttype == "decimal" and _AMOUNT_SUFFIX_RE.search(col_upper):
        return 1.0
    if ttype in ("integer", "bigint") and _ID_SUFFIX_RE.search(col_upper):
        return 1.0
    if ttype == "boolean" and _FLAG_SUFFIX_RE.search(col_upper):
        return 1.0
    if ttype == "string" and _CODE_SUFFIX_RE.search(col_upper):
        return 0.5
    return 0.0


def _compute_confidence(
    name_sim: float, type_compat: float, pat_bonus: float
) -> float:
    return min(1.0, 0.65 * name_sim + 0.25 * type_compat + 0.10 * pat_bonus)


def _detect_prefix(sample_values: list[str]) -> str | None:
    """Detect a consistent string prefix like 'PCL-' or 'PCM-' from sample values."""
    if not sample_values:
        return None
    hits = [_PREFIX_RE.match(v) for v in sample_values if v]
    matched = [m.group(0) for m in hits if m]
    if len(matched) >= len(sample_values) * 0.5 and matched:
        # All matching samples have the same prefix
        if len(set(matched)) == 1:
            return matched[0]
    return None


def _generate_sql(
    col: ColumnProfile,
    field: "CanonicalField",
    is_empty_string_null: bool,
) -> str:
    """Generate the dbt-macro-aware SQL expression for this source→target mapping."""
    src = col.name
    ttype = field.target_type
    stype = col.inferred_type
    null_wrap = is_empty_string_null

    def _nullif(expr: str) -> str:
        return f"NULLIF({expr}, '')" if null_wrap else expr

    # ── Date ─────────────────────────────────────────────────────────────────
    if ttype == "date":
        if col.date_format == "YYYYMMDD":
            return f"{{{{ common_assets.parse_compact_date({_nullif(src)!r}) }}}}"
        if col.date_format == "US":
            return f"{{{{ common_assets.parse_us_date({_nullif(src)!r}) }}}}"
        if col.date_format == "ISO8601":
            return f"CAST({_nullif(src)} AS DATE)"
        # Unknown date format — try compact as default
        return f"{{{{ common_assets.parse_compact_date({_nullif(src)!r}) }}}}"

    # ── Boolean ──────────────────────────────────────────────────────────────
    if ttype == "boolean":
        vals = {v.lower() for v in col.sample_values if v}
        if vals <= {"true", "false"}:
            return f"CAST(LOWER({src}) AS BOOLEAN)"
        # Default: Y/N flag
        return f"{{{{ common_assets.flag_to_boolean('{src}') }}}}"

    # ── Integer / bigint ID with prefix ──────────────────────────────────────
    if ttype in ("integer", "bigint") and col.is_id_column:
        prefix = _detect_prefix(col.sample_values)
        cast_type = "BIGINT" if ttype == "bigint" else "INTEGER"
        if prefix:
            inner = f"REPLACE({_nullif(src)}, '{prefix}', '')"
            return f"CAST({inner} AS {cast_type})"
        return f"CAST({_nullif(src)} AS {cast_type})"

    # ── Decimal from integer cents ────────────────────────────────────────────
    if ttype == "decimal" and stype == "integer" and col.is_cents_integer:
        return (
            f"CAST({_nullif(src)} AS {{{{ common_assets.decimal_type(12, 2) }}}}) / 100.0"
        )

    # ── Decimal ───────────────────────────────────────────────────────────────
    if ttype == "decimal":
        return f"CAST({_nullif(src)} AS {{{{ common_assets.decimal_type(12, 2) }}}})"

    # ── Plain integer ─────────────────────────────────────────────────────────
    if ttype in ("integer", "bigint"):
        return f"CAST({_nullif(src)} AS INTEGER)"

    # ── String passthrough ────────────────────────────────────────────────────
    if null_wrap:
        return _nullif(src)
    return src


# ─── Rule-based pass ──────────────────────────────────────────────────────────

def _rule_map_column(
    col: ColumnProfile, is_empty_string_null: bool
) -> ColumnMapping:
    """Find the best canonical field match for a single column."""
    best_field: "CanonicalField | None" = None
    best_conf = 0.0
    best_nsim = 0.0
    best_tcomp = 0.0
    best_pat = 0.0

    for field in CANONICAL_FIELDS:
        nsim = _name_similarity(col.name, field)
        tcomp = _type_compatibility(col.inferred_type, field.target_type, col)
        pat = _pattern_bonus(col.name, field)
        conf = _compute_confidence(nsim, tcomp, pat)

        if conf > best_conf:
            best_conf = conf
            best_field = field
            best_nsim = nsim
            best_tcomp = tcomp
            best_pat = pat

    target = best_field.name if best_field and best_conf >= 0.30 else None
    sql = (
        _generate_sql(col, best_field, is_empty_string_null)
        if best_field and target
        else col.name
    )
    notes = (
        f"rule: name_sim={best_nsim:.2f}, type_compat={best_tcomp:.2f}, "
        f"pattern={best_pat:.2f}"
    )

    return ColumnMapping(
        source_column=col.name,
        source_table="",
        target_field=target,
        confidence=round(best_conf, 4),
        method="rule",
        sql_expression=sql,
        notes=notes,
        name_similarity=round(best_nsim, 4),
        type_compatibility=round(best_tcomp, 4),
        pattern_bonus=round(best_pat, 4),
    )


# ─── LLM pass ────────────────────────────────────────────────────────────────

def _run_llm_batch(
    batch: list[ColumnProfile],
    table_name: str,
    delimiter: str,
) -> list[ColumnMapping]:
    """Call Claude Haiku for up to LLM_BATCH_SIZE columns. Returns ColumnMapping list."""
    try:
        import anthropic
    except ImportError:
        raise ImportError("anthropic package required for LLM pass. Install: pip install anthropic")

    canonical_summary = [
        {
            "name": f.name,
            "type": f.target_type,
            "required": f.required,
            "description": f.description,
            "aliases": f.aliases[:6],
        }
        for f in CANONICAL_FIELDS
    ]

    col_data = [
        {
            "name": c.name,
            "inferred_type": c.inferred_type,
            "null_rate": round(c.null_rate, 3),
            "sample_values": c.sample_values[:5],
            "value_distribution": dict(list(c.value_distribution.items())[:5]),
            "is_coded_column": c.is_coded_column,
            "is_id_column": c.is_id_column,
            "is_cents_integer": c.is_cents_integer,
            "date_format": c.date_format,
        }
        for c in batch
    ]

    user_prompt = f"""Map these source columns to the canonical insurance policy schema, \
or to "extended_attributes" if no mapping exists.

CANONICAL TARGET SCHEMA:
{json.dumps(canonical_summary, indent=2)}

SOURCE TABLE: {table_name}
DELIMITER: {delimiter!r}

COLUMNS TO MAP:
{json.dumps(col_data, indent=2)}

For each column return exactly this JSON structure:
{{
  "mappings": [
    {{
      "source_column": "<column_name>",
      "target_field": "<canonical_field_name or null for extended_attributes>",
      "confidence": <0.0-1.0>,
      "rationale": "<brief explanation>",
      "sql_expression": "<SQL expression using SOURCE_COL as placeholder for the column name>"
    }}
  ]
}}

SQL expression notes:
- Use SOURCE_COL as the literal placeholder for the column name
- Use dbt macro syntax where appropriate:
  - YYYYMMDD date: {{{{ common_assets.parse_compact_date('SOURCE_COL') }}}}
  - US date (MM/DD/YYYY): {{{{ common_assets.parse_us_date('SOURCE_COL') }}}}
  - Y/N boolean: {{{{ common_assets.flag_to_boolean('SOURCE_COL') }}}}
  - Decimal type: CAST(SOURCE_COL AS {{{{ common_assets.decimal_type(12, 2) }}}})
  - Cents to dollars: CAST(SOURCE_COL AS {{{{ common_assets.decimal_type(12, 2) }}}}) / 100.0
- Return ONLY the JSON. No other text."""

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = response.content[0].text.strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()

    data = json.loads(raw)
    results: list[ColumnMapping] = []

    col_by_name = {c.name: c for c in batch}

    for item in data.get("mappings", []):
        src_name = item["source_column"]
        col = col_by_name.get(src_name)
        if col is None:
            continue

        raw_target = item.get("target_field")
        target = raw_target if raw_target in CANONICAL_NAMES else None

        # Replace SOURCE_COL placeholder with actual column name
        sql = item.get("sql_expression", src_name).replace("SOURCE_COL", src_name)
        confidence = float(item.get("confidence", 0.40))

        results.append(
            ColumnMapping(
                source_column=src_name,
                source_table="",
                target_field=target,
                confidence=round(confidence, 4),
                method="llm",
                sql_expression=sql,
                notes=item.get("rationale", ""),
                name_similarity=0.0,
                type_compatibility=0.0,
                pattern_bonus=0.0,
            )
        )

    # Any columns the LLM omitted → keep rule-based result
    llm_mapped = {r.source_column for r in results}
    for c in batch:
        if c.name not in llm_mapped:
            results.append(
                ColumnMapping(
                    source_column=c.name,
                    source_table="",
                    target_field=None,
                    confidence=0.30,
                    method="llm",
                    sql_expression=c.name,
                    notes="LLM omitted this column; routed to extended_attributes",
                )
            )

    return results


# ─── Public API ───────────────────────────────────────────────────────────────

def map_table(
    table: TableProfile,
    source_name: str,
    llm_threshold: float = DEFAULT_LLM_THRESHOLD,
    use_llm: bool = True,
) -> MappingProposal:
    """Run rule-based (+ optional LLM) mapping for one table.

    Args:
        table:          TableProfile from the profiler.
        source_name:    Logical source name (for the proposal header).
        llm_threshold:  Columns with rule-based confidence < this trigger LLM pass.
        use_llm:        Set False to skip LLM entirely.
    """
    mappings: list[ColumnMapping] = []
    excluded_metadata: list[str] = []

    # Separate CDC metadata columns
    business_cols = []
    for col in table.columns:
        if _CDC_RE.match(col.name):
            excluded_metadata.append(col.name)
        else:
            business_cols.append(col)

    # Rule-based pass
    rule_results: dict[str, ColumnMapping] = {}
    for col in business_cols:
        m = _rule_map_column(col, table.is_empty_string_null)
        m.source_table = table.name
        rule_results[col.name] = m

    # Identify columns for LLM pass
    llm_candidates = [
        col for col in business_cols
        if rule_results[col.name].confidence < llm_threshold
    ]

    # LLM pass
    llm_results: dict[str, ColumnMapping] = {}
    if use_llm and llm_candidates:
        for i in range(0, len(llm_candidates), LLM_BATCH_SIZE):
            batch = llm_candidates[i : i + LLM_BATCH_SIZE]
            batch_results = _run_llm_batch(batch, table.name, table.delimiter)
            for r in batch_results:
                r.source_table = table.name
                llm_results[r.source_column] = r

    # Merge: LLM result replaces rule result if confidence is higher
    for col in business_cols:
        rule_m = rule_results[col.name]
        llm_m = llm_results.get(col.name)
        if llm_m and llm_m.confidence > rule_m.confidence:
            mappings.append(llm_m)
        else:
            mappings.append(rule_m)

    # Separate mapped vs unmapped
    unmapped = [m.source_column for m in mappings if m.target_field is None]
    # De-duplicate: if two source cols map to the same target, keep higher confidence
    final_mappings = _deduplicate(mappings)

    # Missing required fields
    mapped_targets = {m.target_field for m in final_mappings if m.target_field}
    missing = [
        f.name
        for f in CANONICAL_FIELDS
        if f.required and f.name not in mapped_targets
    ]

    return MappingProposal(
        source_name=source_name,
        table_name=table.name,
        mappings=final_mappings,
        unmapped_columns=unmapped,
        missing_standard_fields=missing,
        excluded_metadata_columns=excluded_metadata,
    )


def _deduplicate(mappings: list[ColumnMapping]) -> list[ColumnMapping]:
    """If multiple source columns map to the same target, keep the highest-confidence one."""
    best_by_target: dict[str, ColumnMapping] = {}
    no_target: list[ColumnMapping] = []

    for m in mappings:
        if m.target_field is None:
            no_target.append(m)
        else:
            existing = best_by_target.get(m.target_field)
            if existing is None or m.confidence > existing.confidence:
                best_by_target[m.target_field] = m

    # Demote losers to unmapped
    winning_by_source = {m.source_column for m in best_by_target.values()}
    demoted = [
        ColumnMapping(
            **{
                **m.model_dump(),
                "target_field": None,
                "notes": m.notes + " [demoted: lower confidence than another source column mapping to same target]",
            }
        )
        for m in mappings
        if m.target_field is not None and m.source_column not in winning_by_source
    ]

    return list(best_by_target.values()) + no_target + demoted
