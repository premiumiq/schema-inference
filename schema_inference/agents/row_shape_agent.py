"""RowShapeAgent (MAP-5): infer row identity + dedup strategy from a TableProfile.

Deterministic heuristics first (no API): score columns for natural-key and
recency signals directly from profile stats. An optional LLM layer (added
later) handles genuinely ambiguous tables. Output is a RowShapeProposal,
scored against the catalog's `row_shape` ground-truth section.
"""
from __future__ import annotations

from ..models import ColumnProfile, RowShapeProposal, TableProfile


def _natural_key_score(col: ColumnProfile, row_count: int) -> float:
    """How strongly this column looks like a natural key: near-unique, non-null, id-like."""
    if row_count <= 0:
        return 0.0
    distinct_ratio = col.distinct_count / row_count      # 1.0 = fully unique
    score = 0.0
    score += distinct_ratio * 0.6                        # uniqueness is the dominant signal
    score += (1.0 - col.null_rate) * 0.2                 # keys shouldn't be null
    if col.is_id_column:
        score += 0.2                                     # *_ID/_NO/_NBR/_SEQ name
    return score


def _is_recency_candidate(col: ColumnProfile) -> tuple[bool, float, str]:
    """Return (is_candidate, score, reason) for version/recency signal."""
    name = col.name.upper()
    # Sequence/version: id-like integer with a _SEQ / _VER / _REV style name, low distinct
    if col.is_id_column and col.inferred_type == "integer" and any(
        tok in name for tok in ("SEQ", "VER", "REV", "VERSION")
    ):
        return True, 0.9, "sequence/version integer"
    # CDC operation flag
    if "CDC" in name or "OPERATION" in name:
        return True, 0.85, "CDC operation flag"
    # Date/timestamp columns are weaker recency candidates
    if col.inferred_type == "date":
        return True, 0.5, "date column"
    return False, 0.0, ""


def infer_row_shape(
    table: TableProfile,
    source_name: str,
    run_id: str | None = None,
) -> RowShapeProposal:
    """Deterministic row-shape inference from profile stats."""
    cols = table.columns
    row_count = table.row_count

    # ── Natural key: highest-scoring near-unique id-like column ──────────────
    key_scored = sorted(
        ((c, _natural_key_score(c, row_count)) for c in cols),
        key=lambda cs: cs[1],
        reverse=True,
    )
    best_key, best_key_score = key_scored[0]
    natural_key = [best_key.name]

    # ── Recency column: best version/date signal, excluding the key itself ───
    recency_scored = []
    for c in cols:
        if c.name == best_key.name:
            continue
        ok, score, reason = _is_recency_candidate(c)
        if ok:
            recency_scored.append((c, score, reason))
    recency_scored.sort(key=lambda cs: cs[1], reverse=True)

    recency_column = None
    recency_reason = ""
    if recency_scored:
        recency_column, _, recency_reason = recency_scored[0]
        recency_column = recency_column.name

    # ── Dedup strategy + pattern ─────────────────────────────────────────────
    has_cdc = any("CDC" in c.name.upper() or "OPERATION" in c.name.upper() for c in cols)
    key_is_unique = best_key.distinct_count >= row_count  # already one row per key

    if has_cdc:
        strategy = "cdc_latest"
        pattern = f"QUALIFY ROW_NUMBER() OVER (PARTITION BY {best_key.name} ORDER BY {recency_column} DESC) = 1"
    elif recency_column is not None:
        strategy = "row_number"
        pattern = (
            f"ROW_NUMBER() OVER (PARTITION BY {best_key.name} "
            f"ORDER BY {recency_column} DESC) = 1"
        )
    elif key_is_unique:
        strategy = "none"
        pattern = None
    else:
        # Duplicate keys but no recency signal — fall back to row_number w/o order confidence
        strategy = "row_number"
        pattern = f"ROW_NUMBER() OVER (PARTITION BY {best_key.name} ORDER BY 1 DESC) = 1"

    # ── Confidence: clean key + clear recency = high ─────────────────────────
    confidence = round(min(best_key_score, 1.0) * 0.6, 3)
    if recency_column is not None:
        confidence = round(confidence + 0.3, 3)
    if strategy == "none" and key_is_unique:
        confidence = round(confidence + 0.1, 3)
    confidence = min(confidence, 1.0)

    reasoning = (
        f"Natural key '{best_key.name}' (distinct={best_key.distinct_count}/"
        f"{row_count}, null={best_key.null_rate}, id={best_key.is_id_column}). "
    )
    if recency_column:
        reasoning += f"Recency '{recency_column}' ({recency_reason}). "
    reasoning += f"Strategy: {strategy}."

    return RowShapeProposal(
        source_name=source_name,
        table_name=table.name,
        natural_key=natural_key,
        recency_column=recency_column,
        dedup_strategy=strategy,
        dedup_pattern=pattern,
        confidence=confidence,
        reasoning=reasoning,
        run_id=run_id,
    )
