"""MAP-10: pairwise scoring of cross-source clusters (schema_inference/cluster_score.py)."""
from pathlib import Path

import pytest

from schema_inference.cluster import ClusterMember, ColumnCluster, cluster_columns
from schema_inference.cluster_score import load_secondary_targets, load_targets, score_clusters
from schema_inference.models import ColumnProfile
from schema_inference.profiler import profile_file

_SAMPLE_DIR = Path(__file__).resolve().parent.parent / "examples" / "insurance" / "test_data"


def _cluster(*keys: str) -> ColumnCluster:
    members = []
    for key in keys:
        source, col = key.split(".")
        members.append(ClusterMember(source, f"{source}_t", ColumnProfile(
            name=col, inferred_type="string", null_rate=0.0, distinct_count=1,
            sample_values=[], value_distribution={},
        )))
    return ColumnCluster(members=members)


TARGETS = {
    "a": {"POL_NO": "policy_id", "EFF_DT": "start_date", "TERM_DT": None, "INS_ADDR": None},
    "b": {"policy_id": "policy_id", "effective_date": "start_date", "insured_ein": None},
}


def test_perfect_clustering_scores_one():
    s = score_clusters(
        [_cluster("a.POL_NO", "b.policy_id"), _cluster("a.EFF_DT", "b.effective_date")],
        TARGETS,
    )
    assert (s.true_positives, s.false_positives, s.false_negatives) == (2, 0, 0)
    assert s.f1 == 1.0
    assert s.truth_pairs == 2


def test_targeted_column_paired_with_null_target_is_a_false_positive():
    """TERM_DT maps to no canonical field, effective_date maps to start_date:
    the catalog says they are different concepts, so the pair counts against
    precision instead of disappearing from the metric."""
    s = score_clusters([_cluster("a.TERM_DT", "b.effective_date")], TARGETS)
    assert s.false_positives == 1
    assert s.fp_pairs == [("a.TERM_DT", "b.effective_date")]
    assert s.unscored_pairs == []


def test_two_null_targets_are_unscored_but_reported():
    s = score_clusters([_cluster("a.INS_ADDR", "b.insured_ein")], TARGETS)
    assert (s.true_positives, s.false_positives) == (0, 0)
    assert s.unscored_pairs == [("a.INS_ADDR", "b.insured_ein")]


def test_uncatalogued_column_is_unscored():
    s = score_clusters([_cluster("a.POL_NO", "b._cdc_timestamp")], TARGETS)
    assert s.false_positives == 0
    assert s.unscored_pairs == [("a.POL_NO", "b._cdc_timestamp")]


def test_missed_truth_pairs_are_false_negatives():
    s = score_clusters([_cluster("a.POL_NO", "b.policy_id")], TARGETS)
    assert s.false_negatives == 1
    assert s.fn_pairs == [("a.EFF_DT", "b.effective_date")]
    assert s.recall == 0.5


def test_load_targets_keeps_null_target_columns():
    targets = load_targets("pasl")
    assert targets["POL_NO"] == "policy_id"
    assert "TERM_EFF_DT" in targets and targets["TERM_EFF_DT"] is None


def test_pair_joined_through_secondary_target_is_not_a_false_positive():
    """POL_NO's primary is policy_id but its catalog secondary_target is
    policy_number: clustering it with b.policy_number is defensible, so it is
    reported separately instead of counted against precision. The primary
    partner is still missed."""
    targets = {"a": {"POL_NO": "policy_id"},
               "b": {"policy_id": "policy_id", "policy_number": "policy_number"}}
    secondary = {"a": {"POL_NO": "policy_number"}}
    s = score_clusters([_cluster("a.POL_NO", "b.policy_number")], targets, secondary)
    assert s.false_positives == 0
    assert s.secondary_pairs == [("a.POL_NO", "b.policy_number")]
    assert s.fn_pairs == [("a.POL_NO", "b.policy_id")]

    # Without secondary info it stays a plain FP.
    assert score_clusters([_cluster("a.POL_NO", "b.policy_number")], targets).false_positives == 1


def test_catalog_lookup_rejects_path_like_source_names():
    with pytest.raises(ValueError, match="invalid source name"):
        load_targets("../../etc/x")


def test_pasl_pasm_sample_baseline():
    """Regression floor for the deterministic layer on PAS-L x PAS-M sample
    data (secondary_target-aware). Baseline at the time of writing:
    P 0.900 / R 0.692 / F1 0.783 —
    raise these floors when the clustering improves, never lower them to
    make a change pass."""
    tables = []
    for source in ("pasl", "pasm"):
        profile = profile_file(_SAMPLE_DIR / f"{source}_policy_sample.dat", source_name=source)
        tables.append((source, profile.tables[0]))

    clusters = cluster_columns(tables)
    s = score_clusters(
        clusters,
        {src: load_targets(src) for src, _ in tables},
        {src: load_secondary_targets(src) for src, _ in tables},
    )
    reversed_clusters = cluster_columns(list(reversed(tables)))
    assert {frozenset(c.column_names()) for c in clusters} == \
        {frozenset(c.column_names()) for c in reversed_clusters}

    assert s.precision >= 0.89
    assert s.recall >= 0.69
    assert s.f1 >= 0.78
