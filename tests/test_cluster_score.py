"""MAP-10: pairwise scoring of cross-source clusters (schema_inference/cluster_score.py)."""
from pathlib import Path

import pytest

from schema_inference.cluster import ClusterMember, ColumnCluster, cluster_columns
from schema_inference.cluster_score import load_targets, score_clusters
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


def test_pasl_pasm_sample_baseline(tmp_path):
    """Regression floor for the deterministic layer on PAS-L x PAS-M sample
    data. Baseline at the time of writing: P 0.818 / R 0.692 / F1 0.750 —
    raise these floors when the clustering improves, never lower them to
    make a change pass."""
    tables = []
    for source in ("pasl", "pasm"):
        profile = profile_file(_SAMPLE_DIR / f"{source}_policy_sample.dat", source_name=source)
        tables.append((source, profile.tables[0]))

    clusters = cluster_columns(tables)
    s = score_clusters(clusters, {src: load_targets(src) for src, _ in tables})

    assert s.precision >= 0.81
    assert s.recall >= 0.69
    assert s.f1 >= 0.75
