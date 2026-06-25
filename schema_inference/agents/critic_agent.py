"""CriticAgent — adversarial review of hard and low-confidence mappings.

Runs AFTER the MappingAgent produces a proposal. It re-examines the columns most
likely to be wrong (is_hard columns + columns below their confidence floor) and
challenges each mapping. It receives the catalog NOTES that explain why a column
is tricky, but never the ground-truth canonical_target — it must reason independently.

Per column it returns either:
  confirm                      — the existing mapping stands
  override(target, sql, why)   — the mapping is wrong; replace it

Model: claude-sonnet-4-6 (stronger reasoning than Haiku for the adversarial task).
Single batch call, not a per-column loop.
"""

from __future__ import annotations

import json

import yaml

from ..metamodel.few_shot import format_examples_block, retrieve_examples
from ..models import AgentTrace, ColumnMapping
from .throttle import call_with_retry
from .tools import _SCHEMA_CATALOG_PATH, check_value_catalog

MODEL = "claude-sonnet-4-6"

_SYSTEM_PROMPT = """You are a senior insurance data engineer performing an adversarial \
review of proposed column mappings from a legacy policy admin system (PAS-L) to a \
canonical policy schema.

Your job is to CHALLENGE each proposed mapping and catch errors. The mappings you are \
reviewing are the hard cases — the ones most likely to be wrong. Common failure modes:
- A column name that resembles a canonical field but actually means something different
  once you check its true type, value map, or what it's actually measuring — don't take
  the name at face value.
- A field whose granularity or time period doesn't match the canonical target (e.g. a
  sub-period or derived figure mapped to a field that should hold the base/primary value).
- An attribute belonging to a different entity than the canonical field implies (e.g.
  describing the wrong party or the wrong geography level).
- A correct field mapping that has the WRONG transformation (e.g. an encoding or unit
  conversion that wasn't applied).

For each column you receive: the source column name, its profile, the proposed target,
the proposed SQL, the confidence, and a catalog NOTE explaining why the column is tricky.
The note explains the difficulty but does NOT tell you the correct answer — you must
decide independently.

Respond with ONLY a JSON object in this exact form:
{
  "reviews": [
    {
      "source_column": "<name>",
      "verdict": "confirm" | "override",
      "target_field": "<canonical field or null>",   // only if override
      "sql_expression": "<sql>",                       // only if override
      "rationale": "<one sentence>"
    }
  ]
}"""


def _active_system_prompt() -> str:
    """MAP-4 Layer 2: the metamodel's most recently human-accepted prompt for
    'critic', if any; else the hardcoded _SYSTEM_PROMPT above. Same pattern as
    mapping_agent.py's _active_system_prompt() / mapper.py's _rule_weights()."""
    try:
        from ..metamodel.store import open_store
    except ImportError:
        return _SYSTEM_PROMPT
    store = open_store()
    if not store:
        return _SYSTEM_PROMPT
    try:
        return store.get_active_prompt("critic") or _SYSTEM_PROMPT
    finally:
        store.close()


def _load_catalog_notes() -> dict:
    """Load the schema catalog but expose ONLY the notes + is_hard + confidence_floor.
    Never expose canonical_target (the answer)."""
    with open(_SCHEMA_CATALOG_PATH, encoding="utf-8") as f:
        catalog = yaml.safe_load(f)
    safe = {}
    for col, meta in catalog.get("columns", {}).items():
        if not meta:
            continue
        safe[col] = {
            "is_hard": meta.get("is_hard", False),
            "confidence_floor": meta.get("confidence_floor"),
            "note": (meta.get("notes") or "").strip(),
        }
    return safe


def _select_targets(
    mappings: list[ColumnMapping],
    catalog_notes: dict,
) -> list[ColumnMapping]:
    """Pick the columns the critic should review: hard columns + below-floor columns."""
    targets = []
    for m in mappings:
        meta = catalog_notes.get(m.source_column, {})
        is_hard = meta.get("is_hard", False)
        floor = meta.get("confidence_floor")
        below_floor = floor is not None and m.confidence < floor
        if is_hard or below_floor:
            targets.append(m)
    return targets


def run_critic_agent(
    mappings: list[ColumnMapping],
    profiles_by_name: dict,
    source_name: str = "pasl",
    system_prompt_override: str | None = None,
) -> tuple[list[ColumnMapping], list[AgentTrace], int]:
    """Adversarially review hard/below-floor mappings. Returns (updated_mappings, traces, override_count).

    Args:
        mappings:           the current ColumnMapping list (post MappingAgent).
        profiles_by_name:   {column_name: ColumnProfile} for context.
        source_name:        logical source name — used to retrieve the
                            relevant few-shot example bank (MAP-4 Layer 1).
        system_prompt_override: MAP-4 Layer 2 — see mapping_agent.py's param
                            of the same name. Only the tuning script's
                            VALIDATE step should pass this.

    Returns:
        (updated_mappings, traces, override_count)
    """
    import anthropic

    system_prompt = system_prompt_override or _active_system_prompt()

    catalog_notes = _load_catalog_notes()
    targets = _select_targets(mappings, catalog_notes)

    if not targets:
        return mappings, [], 0

    # Build the review payload (no canonical_target leaked)
    review_items = []
    for m in targets:
        prof = profiles_by_name.get(m.source_column)
        note = catalog_notes.get(m.source_column, {}).get("note", "")
        examples = retrieve_examples(source_name, prof) if prof else []
        item = {
            "source_column": m.source_column,
            "proposed_target": m.target_field,
            "proposed_sql": m.sql_expression,
            "confidence": round(m.confidence, 3),
            "inferred_type": prof.inferred_type if prof else None,
            "sample_values": prof.sample_values[:5] if prof else [],
            "is_cents_integer": prof.is_cents_integer if prof else None,
            "is_coded_column": prof.is_coded_column if prof else None,
            "catalog_note": note,
        }
        examples_block = format_examples_block(examples)
        if examples_block:
            item["similar_past_examples"] = examples_block
        review_items.append(item)

    user_prompt = (
        "Review these proposed mappings. Challenge each one and decide confirm or override.\n\n"
        + json.dumps(review_items, indent=2)
    )

    client = anthropic.Anthropic()
    # Prompt caching: system_prompt repeats across every CriticAgent call within
    # a session (Layer 2's tuning loop calls this once per train/holdout run)
    # — cache_control lets repeats within the 5-min window skip re-billing the
    # cached prefix. No-op if below the model's minimum cacheable length.
    response = call_with_retry(client, dict(
        model=MODEL,
        max_tokens=2048,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_prompt}],
    ))

    raw = "".join(b.text for b in response.content if b.type == "text").strip()
    # Strip fences / extract JSON
    if "```" in raw:
        import re
        fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if fenced:
            raw = fenced[-1]
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end != -1:
        raw = raw[start:end + 1]

    try:
        data = json.loads(raw)
    except Exception:
        # If the critic's output is unparseable, leave mappings unchanged
        return mappings, [], 0

    reviews = {r["source_column"]: r for r in data.get("reviews", [])}

    updated = []
    traces: list[AgentTrace] = []
    override_count = 0

    for m in mappings:
        r = reviews.get(m.source_column)
        if r is None or r.get("verdict") == "confirm":
            updated.append(m)
            if r is not None:
                traces.append(AgentTrace(
                    column_name=m.source_column,
                    agent="critic",
                    tool_calls=[],
                    final_target=m.target_field,
                    final_confidence=m.confidence,
                    reasoning_summary=f"CONFIRM: {r.get('rationale', '')}",
                ))
            continue

        # Override
        new_target = r.get("target_field")
        if new_target in ("", "null", "None", "extended_attributes"):
            new_target = None
        new_sql = r.get("sql_expression") or (m.source_column if new_target is None else m.sql_expression)
        rationale = r.get("rationale", "")

        updated.append(ColumnMapping(
            source_column=m.source_column,
            source_table=m.source_table,
            target_field=new_target,
            confidence=m.confidence,   # keep confidence; critic changes the decision not the score
            method="critic",
            sql_expression=new_sql,
            notes=f"[critic override] {rationale}",
        ))
        override_count += 1
        traces.append(AgentTrace(
            column_name=m.source_column,
            agent="critic",
            tool_calls=[],
            final_target=new_target,
            final_confidence=m.confidence,
            reasoning_summary=f"OVERRIDE → {new_target}: {rationale}",
        ))

    return updated, traces, override_count