"""Score cross-source column clustering against ground truth (MAP-10).

No new ground truth was needed for this. PAS-L and PAS-M already map to a
shared canonical model, so their existing schema catalogs encode the answer:
any two columns from different sources that share a non-null canonical_target
are the same concept and SHOULD land in the same cluster.

Columns whose canonical_target is null (routed to extended_attributes) are
excluded from scoring entirely - two nulls do not imply two columns mean the
same thing, so they can neither confirm nor refute a cluster. They are counted
separately so the excluded volume is visible rather than silently ignored.

Scoring is over PAIRS, not clusters: it asks "should these two columns be
together?" for every cross-source pair, which avoids having to decide whether a
partially-correct cluster counts as right or wrong.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

import yaml

from .cluster import ColumnCluster

GROUND_TRUTH_DIR = Path("examples/insurance/ground_truth")


def load_targets(source_name: str, catalog_dir: Path | None = None) -> dict[str, str]:
    """column_name -> canonical_target, for columns that have a non-null target."""
    d = catalog_dir or GROUND_TRUTH_DIR
    path = d / f"{source_name}_schema_catalog.yml"
    with open(path, encoding="utf-8") as f:
        catalog = yaml.safe_load(f) or {}

    targets: dict[str, str] = {}
    for col_name, entry in (catalog.get("columns") or {}).items():
        if not isinstance(entry, dict):
            continue
        target = entry.get("canonical_target")
        if target:
            targets[col_name] = target
    return targets


@dataclass
class ClusterScore:
    true_positives:  int
    false_positives: int
    false_negatives: int
    precision:       float
    recall:          float
    f1:              float
    scored_pairs:    int   # pairs where both columns had a non-null target
    fp_pairs:        list[tuple[str, str]]
    fn_pairs:        list[tuple[str, str]]


def score_clusters(
    clusters: list[ColumnCluster],
    targets_by_source: dict[str, dict[str, str]],
) -> ClusterScore:
    """Precision/recall over cross-source pairs.

    Args:
        clusters:          output of cluster_columns()
        targets_by_source: source_name -> {column_name: canonical_target}
    """
    # ── Truth: every cross-source pair sharing a canonical target ────────────
    by_target: dict[str, list[tuple[str, str]]] = {}
    for source, targets in targets_by_source.items():
        for col, target in targets.items():
            by_target.setdefault(target, []).append((source, col))

    should: set[frozenset[tuple[str, str]]] = set()
    for _target, cols in by_target.items():
        for a, b in combinations(cols, 2):
            if a[0] != b[0]:  # cross-source only
                should.add(frozenset((a, b)))

    # ── Prediction: every cross-source pair inside a cluster ────────────────
    did: set[frozenset[tuple[str, str]]] = set()
    for cluster in clusters:
        keys = [(m.source_name, m.column.name) for m in cluster.members]
        for a, b in combinations(keys, 2):
            if a[0] != b[0]:
                did.add(frozenset((a, b)))

    # Only pairs where BOTH columns carry a non-null target are scoreable.
    def scoreable(pair: frozenset) -> bool:
        return all(
            col in targets_by_source.get(src, {})
            for src, col in pair
        )

    did_scoreable = {p for p in did if scoreable(p)}

    tp = len(did_scoreable & should)
    fp = len(did_scoreable - should)
    fn = len(should - did_scoreable)

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    def fmt(pair: frozenset) -> tuple[str, str]:
        a, b = sorted(pair)
        return (f"{a[0]}.{a[1]}", f"{b[0]}.{b[1]}")

    return ClusterScore(
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        precision=round(precision, 4),
        recall=round(recall, 4),
        f1=round(f1, 4),
        scored_pairs=len(did_scoreable),
        fp_pairs=sorted(fmt(p) for p in (did_scoreable - should)),
        fn_pairs=sorted(fmt(p) for p in (should - did_scoreable)),
    )


def print_score(score: ClusterScore, show_pairs: bool = True) -> None:
    print("─" * 62)
    print("  CROSS-SOURCE CLUSTERING")
    print("─" * 62)
    print(f"  TP {score.true_positives}   FP {score.false_positives}   FN {score.false_negatives}")
    print(f"  Precision {score.precision:.3f}   Recall {score.recall:.3f}   F1 {score.f1:.3f}")

    if show_pairs and score.fp_pairs:
        print("\n  False positives (clustered, shouldn't be):")
        for a, b in score.fp_pairs:
            print(f"    {a}  <->  {b}")

    if show_pairs and score.fn_pairs:
        print("\n  False negatives (missed):")
        for a, b in score.fn_pairs:
            print(f"    {a}  <->  {b}")
    print("─" * 62)