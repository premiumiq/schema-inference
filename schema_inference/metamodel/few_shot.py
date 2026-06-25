"""Few-shot example bank — retrieval for MAP-4 Layer 1.

The bank's contents are the tuned parameter; curation (tools/curate_few_shot_bank.py)
is the update rule. This module is the retrieval half: given a column being mapped
right now, find the most similar past examples and render them for prompt injection.

Bank entries come from two origins (curated offline, not at agent runtime):
  hard_tp                  — a hard column the rule/agent pipeline got right (verdict=TP)
  critic_override_accepted — a CriticAgent override that a human later accepted/auto-approved

Similarity: no embedding model — the bank is small (tens to low hundreds of rows per
source), so cheap scoring is enough. Score = 0.5 * profile-flag agreement
(inferred_type, is_id_column, is_coded_column, is_cents_integer, date_format) +
0.5 * fuzzy name similarity (rapidfuzz, same style as mapper.py's rule engine).

See docs/self-tuning-mapper-agent-plan.md (Layer 1).
"""

from __future__ import annotations

import json

from rapidfuzz import fuzz

from ..models import ColumnProfile
from .store import open_store

_SIGNATURE_KEYS = ("inferred_type", "is_id_column", "is_coded_column", "is_cents_integer", "date_format")


def build_profile_signature(col: ColumnProfile) -> dict:
    return {k: getattr(col, k) for k in _SIGNATURE_KEYS}


def _flag_agreement(sig_a: dict, sig_b: dict) -> float:
    matches = sum(1 for k in _SIGNATURE_KEYS if sig_a.get(k) == sig_b.get(k))
    return matches / len(_SIGNATURE_KEYS)


def _name_similarity(a: str, b: str) -> float:
    a_n, b_n = a.lower().replace("_", " "), b.lower().replace("_", " ")
    return max(fuzz.ratio(a_n, b_n), fuzz.token_set_ratio(a_n, b_n)) / 100.0


def score_similarity(col: ColumnProfile, example: dict) -> float:
    """0.0-1.0. example is a row dict from MetamodelStore.get_few_shot_examples()."""
    ex_sig = json.loads(example["profile_signature_json"])
    flag_score = _flag_agreement(build_profile_signature(col), ex_sig)
    name_score = _name_similarity(col.name, example["source_column"])
    return 0.5 * flag_score + 0.5 * name_score


def retrieve_examples(
    source_name: str,
    col: ColumnProfile,
    top_k: int = 3,
    min_score: float = 0.35,
) -> list[dict]:
    """Best-effort: returns [] if the store is unavailable or the bank is empty
    or nothing clears min_score. Never raises — retrieval is optional, the
    agent must still run with no examples if the bank can't be reached."""
    try:
        store = open_store()
        if not store:
            return []
        try:
            examples = store.get_few_shot_examples(source_name, status="active")
        finally:
            store.close()
    except Exception:
        return []

    if not examples:
        return []

    # No self-citation: don't show a column its own past mapping as an "example"
    scored = [
        (score_similarity(col, ex), ex) for ex in examples
        if ex["source_column"] != col.name
    ]
    scored = [pair for pair in scored if pair[0] >= min_score]
    scored.sort(key=lambda pair: -pair[0])
    return [ex for _, ex in scored[:top_k]]


def format_examples_block(examples: list[dict]) -> str:
    """Render retrieved examples as a prompt-injectable text block.
    Empty string when there's nothing to show — callers can unconditionally
    append this without an extra branch."""
    if not examples:
        return ""

    lines = [
        "PAST EXAMPLES (similar columns correctly resolved before — useful context, "
        "not a guarantee this column works the same way):"
    ]
    for ex in examples:
        target = ex.get("target_field") or "extended_attributes (no canonical mapping)"
        lines.append(
            f"- Column '{ex['source_column']}' -> {target}\n"
            f"  Reasoning: {ex.get('reasoning') or '(no reasoning recorded)'}"
        )
    return "\n".join(lines)
