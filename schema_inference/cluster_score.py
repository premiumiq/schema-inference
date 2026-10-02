"""Score cross-source column clustering against ground truth (MAP-10).

No new ground truth was needed for this. PAS-L and PAS-M already map to a
shared canonical model, so their existing schema catalogs encode the answer:
any two columns from different sources that share a non-null canonical_target
are the same concept and SHOULD land in the same cluster.

A column whose canonical_target is null (routed to extended_attributes) still
carries information: it is NOT any canonical field. So a predicted pair of one
targeted column and one null-target column is a false positive - the catalog
says they are different concepts. Only pairs where NEITHER column has a target
(or a column is absent from the catalog entirely, e.g. _cdc_* metadata) are
unscoreable: two nulls do not imply two columns mean the same thing. Those are
reported separately as unscored pairs so the volume the metric cannot see is
visible rather than silently ignored - INS_ADDR <-> insured_ein lands there.

Scoring is over PAIRS, not clusters: it asks "should these two columns be
together?" for every cross-source pair, which avoids having to decide whether a
partially-correct cluster counts as right or wrong.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

import yaml

from .cluster import ColumnCluster

_REPO_ROOT = Path(__file__).resolve().parent.parent
GROUND_TRUTH_DIR = Path(
    os.environ.get("SCHEMA_INFERENCE_CATALOG_DIR")
    or str(_REPO_ROOT / "examples" / "insurance" / "ground_truth")
)


_SOURCE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _catalog_columns(source_name: str, catalog_dir: Path | None) -> dict[str, dict]:
    # source_name comes from a profile JSON; keep it from walking out of the
    # catalog directory.
    if not _SOURCE_NAME_RE.match(source_name):
        raise ValueError(f"invalid source name for catalog lookup: {source_name!r}")
    d = catalog_dir or GROUND_TRUTH_DIR
    path = d / f"{source_name}_schema_catalog.yml"
    with open(path, encoding="utf-8") as f:
        catalog = yaml.safe_load(f) or {}
    return {
        col: entry for col, entry in (catalog.get("columns") or {}).items()
        if isinstance(entry, dict)
    }


def load_targets(source_name: str, catalog_dir: Path | None = None) -> dict[str, str | None]:
    """column_name -> canonical_target for every catalogued column.

    Null-target columns are kept (as None): "maps to no canonical field" is
    ground truth too, and score_clusters() needs it to tell a refuted pair from
    an uncatalogued one.
    """
    return {
        col: entry.get("canonical_target") or None
        for col, entry in _catalog_columns(source_name, catalog_dir).items()
    }


def load_secondary_targets(source_name: str, catalog_dir: Path | None = None) -> dict[str, str]:
    """column_name -> secondary_target, for columns that declare one."""
    return {
        col: entry["secondary_target"]
        for col, entry in _catalog_columns(source_name, catalog_dir).items()
        if entry.get("secondary_target")
    }


@dataclass
class ClusterScore:
    true_positives:  int
    false_positives: int
    false_negatives: int
    precision:       float
    recall:          float
    f1:              float
    truth_pairs:     int   # cross-source pairs sharing a canonical target (TP + FN)
    predicted_pairs: int   # scoreable predicted pairs (TP + FP)
    fp_pairs:        list[tuple[str, str]]
    fn_pairs:        list[tuple[str, str]]
    unscored_pairs:  list[tuple[str, str]]  # predicted, but neither side has a target
    secondary_pairs: list[tuple[str, str]]  # predicted, primaries differ, joined via a secondary_target


def score_clusters(
    clusters: list[ColumnCluster],
    targets_by_source: dict[str, dict[str, str | None]],
    secondary_by_source: dict[str, dict[str, str]] | None = None,
) -> ClusterScore:
    """Precision/recall over cross-source pairs.

    Args:
        clusters:            output of cluster_columns()
        targets_by_source:   source_name -> {column_name: canonical_target | None},
                             as returned by load_targets()
        secondary_by_source: source_name -> {column_name: secondary_target}, as
                             returned by load_secondary_targets(). A predicted
                             pair whose primaries differ but which shares a
                             field through a secondary target (POL_NO's
                             secondary is policy_number) is defensible, not
                             wrong: it is reported in secondary_pairs and
                             counted as neither TP nor FP. The truth set stays
                             primary-only, so the primary partner is still an FN.
    """
    secondary_by_source = secondary_by_source or {}
    # ── Truth: every cross-source pair sharing a canonical target ────────────
    by_target: dict[str, list[tuple[str, str]]] = {}
    for source, targets in targets_by_source.items():
        for col, target in targets.items():
            if target:
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

    # Scoreable: both columns catalogued, and at least one has a target. A
    # targeted column paired with a null-target one is refuted by the catalog;
    # two nulls (or an uncatalogued column) can neither confirm nor refute.
    def scoreable(pair: frozenset) -> bool:
        cats = [targets_by_source.get(src, {}) for src, _ in pair]
        if not all(col in cat for cat, (_, col) in zip(cats, pair)):
            return False
        return any(cat[col] for cat, (_, col) in zip(cats, pair))

    def fields(src: str, col: str) -> set[str]:
        out = {targets_by_source.get(src, {}).get(col), secondary_by_source.get(src, {}).get(col)}
        return {f for f in out if f}

    did_scoreable = {p for p in did if scoreable(p)}
    did_unscored = did - did_scoreable
    did_secondary = {
        p for p in did_scoreable - should
        if set.intersection(*(fields(src, col) for src, col in p))
    }
    did_scoreable -= did_secondary

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
        truth_pairs=len(should),
        predicted_pairs=len(did_scoreable),
        fp_pairs=sorted(fmt(p) for p in (did_scoreable - should)),
        fn_pairs=sorted(fmt(p) for p in (should - did_scoreable)),
        unscored_pairs=sorted(fmt(p) for p in did_unscored),
        secondary_pairs=sorted(fmt(p) for p in did_secondary),
    )


def print_score(score: ClusterScore, show_pairs: bool = True) -> None:
    print("─" * 62)
    print("  CROSS-SOURCE CLUSTERING")
    print("─" * 62)
    print(f"  TP {score.true_positives}   FP {score.false_positives}   FN {score.false_negatives}")
    print(f"  Precision {score.precision:.3f}   Recall {score.recall:.3f}   F1 {score.f1:.3f}")
    print(f"  {score.truth_pairs} truth pairs   {score.predicted_pairs} scoreable predicted   "
          f"{len(score.unscored_pairs)} unscored   {len(score.secondary_pairs)} via secondary")

    if show_pairs and score.fp_pairs:
        print("\n  False positives (clustered, shouldn't be):")
        for a, b in score.fp_pairs:
            print(f"    {a}  <->  {b}")

    if show_pairs and score.fn_pairs:
        print("\n  False negatives (missed):")
        for a, b in score.fn_pairs:
            print(f"    {a}  <->  {b}")

    if show_pairs and score.secondary_pairs:
        print("\n  Via secondary_target (defensible, not counted):")
        for a, b in score.secondary_pairs:
            print(f"    {a}  <->  {b}")

    if show_pairs and score.unscored_pairs:
        print("\n  Unscored (clustered, no target on either side - review by hand):")
        for a, b in score.unscored_pairs:
            print(f"    {a}  <->  {b}")
    print("─" * 62)