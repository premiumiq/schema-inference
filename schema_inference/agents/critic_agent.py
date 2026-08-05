"""CriticAgent — adversarial review of hard and low-confidence mappings.

Runs AFTER the MappingAgent produces a proposal. It re-examines the columns most
likely to be wrong (is_hard columns + columns below their confidence floor) and
challenges each mapping. It receives the catalog NOTES that explain why a column
is tricky, but never the ground-truth canonical_target — it must reason independently.

Per column it returns either:
  confirm                      — the existing mapping stands
  override(target, sql, why)   — the mapping is wrong; replace it

Model: Claude Sonnet by default (stronger reasoning than Haiku for the
adversarial task) — config-driven via agent_config.yml's llm: section
(MAP-8), see _model() below. Single batch call, not a per-column loop.
"""

from __future__ import annotations

import json

import yaml

from ..llm.registry import get_provider, model_for
from ..metamodel.few_shot import format_examples_block, retrieve_examples
from ..canonical.policy import CANONICAL_BY_NAME
from ..models import AgentTrace, ColumnMapping
from .throttle import call_with_retry
from .tools import _CATALOG_DIR, check_value_catalog

# MAP-8: fallback default only, used when agent_config.yml's
# llm.models.critic_agent is missing/partial — see _model() below.
MODEL = "claude-sonnet-4-6"


def _model() -> str:
    """The configured model for the critic agent (agent_config.yml's
    llm.models.critic_agent), falling back to the hardcoded MODEL constant.
    Same pattern as mapping_agent.py's _model()."""
    return model_for("critic_agent") or MODEL

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


def _load_catalog_notes(source_name: str) -> dict:
    """Load source_name's schema catalog but expose ONLY the notes + is_hard +
    confidence_floor. Never expose canonical_target (the answer)."""
    import os
    path = os.path.join(_CATALOG_DIR, f"{source_name}_schema_catalog.yml")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        catalog = yaml.safe_load(f) or {}
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
    system_prompt = system_prompt_override or _active_system_prompt()

    catalog_notes = _load_catalog_notes(source_name)
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

    provider = get_provider("critic_agent")
    # Prompt caching: system_prompt repeats across every CriticAgent call within
    # a session (Layer 2's tuning loop calls this once per train/holdout run)
    # — cache_control lets repeats within the 5-min window skip re-billing the
    # cached prefix. No-op if below the model's minimum cacheable length.
    # MAP-8: applied by the Anthropic provider adapter; silently stripped by
    # the OpenAI adapter (no equivalent concept on that backend).
    response = call_with_retry(provider, dict(
        model=_model(),
        max_tokens=2048,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_prompt}],
    ))

    raw = response.text.strip()
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

# ─── MAP-3: contested near-tie resolution ────────────────────────────────────

_CONTEST_SYSTEM_PROMPT = """You are a senior insurance data engineer resolving a \
contested column mapping. Two or more source columns from a legacy policy system have \
each been proposed as the mapping for the SAME canonical target field, with nearly \
equal confidence. Your job is to decide which one genuinely belongs — or whether they \
actually map to different fields.

You are given, for each competing source column: its name, its data profile (inferred \
type, sample values, null rate), and the catalog note describing what the target field \
is meant to hold.

Reason about which column's actual DATA best fits the target field — not just which \
name looks closest. A column whose values match the target's meaning wins; a column \
whose values clearly represent something else should be rejected (mapped elsewhere or \
to extended_attributes).

Respond with ONLY a JSON object:
{
  "winner": "<source_column that best maps to the target, or null if none fit>",
  "loser_disposition": "extended_attributes",
  "rationale": "<one sentence explaining the decision>"
}"""


def resolve_contests(
    contests: list[dict],
    mappings: list[ColumnMapping],
    profiles_by_name: dict,
    source_name: str = "pasl",
    canonical_by_name: dict | None = None,
) -> tuple[list[ColumnMapping], list[dict]]:
    """MAP-3: send each genuine contest to the critic as a comparison decision.

    For each contest (two+ columns competing for one target with near-equal
    confidence), ask the critic which column genuinely maps to the target. Update
    the mappings accordingly. Contests the critic can't resolve stay in the
    returned list for human review.

    canonical_by_name=None uses the default 'policy' schema — callers with a
    table-specific schema (see canonical/registry.py) pass it explicitly.

    Returns (updated_mappings, unresolved_contests).
    """
    if not contests:
        return mappings, []

    by_name = canonical_by_name if canonical_by_name is not None else CANONICAL_BY_NAME
    provider = get_provider("critic_agent")
    by_col = {m.source_column: m for m in mappings}
    unresolved: list[dict] = []

    for contest in contests:
        target = contest["target_field"]
        competing = contest["competing_columns"]

        # Build the comparison payload
        field = by_name.get(target)
        note = _load_catalog_notes(source_name).get(target, {}).get("note", "") if field else ""
        candidates = []
        for col in competing:
            prof = profiles_by_name.get(col)
            candidates.append({
                "source_column": col,
                "inferred_type": prof.inferred_type if prof else None,
                "sample_values": prof.sample_values[:5] if prof else [],
                "null_rate": prof.null_rate if prof else None,
            })

        user_prompt = (
            f"Target field: {target}\n"
            f"Target field type: {field.target_type if field else 'unknown'}\n"
            f"Catalog note: {note or '(none)'}\n\n"
            f"Competing source columns:\n{json.dumps(candidates, indent=2)}\n\n"
            f"Which column genuinely maps to {target}?"
        )

        response = call_with_retry(provider, dict(
            model=_model(),
            max_tokens=512,
            system=_CONTEST_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        ))

        raw = response.text.strip()
        if "```" in raw:
            import re
            fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
            if fenced:
                raw = fenced[-1]
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end != -1:
            raw = raw[start:end + 1]

        try:
            decision = json.loads(raw)
        except Exception:
            # Unparseable — leave for human review
            unresolved.append(contest)
            continue

        winner = decision.get("winner")
        rationale = decision.get("rationale", "")

        if winner not in competing:
            # Critic couldn't pick a clear winner → human review
            unresolved.append(contest)
            continue

        # Apply: winner keeps target, losers demoted to extended_attributes
        for col in competing:
            m = by_col.get(col)
            if m is None:
                continue
            if col == winner:
                m.target_field = target
                m.notes = (m.notes + " | " if m.notes else "") + f"[critic contest winner: {rationale}]"
            else:
                m.target_field = None
                m.sql_expression = m.source_column
                m.notes = (m.notes + " | " if m.notes else "") + f"[critic contest: lost to {winner}]"

    return list(by_col.values()), unresolved