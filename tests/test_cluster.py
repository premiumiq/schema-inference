"""MAP-10: cross-source column clustering (schema_inference/cluster.py)."""
import pytest

from schema_inference.cluster import (
    _normalize_name,
    cluster_columns,
    score_pair,
)
from schema_inference.models import ColumnProfile, TableProfile


def _col(name, inferred_type="string", distinct=10, null_rate=0.0,
         values=None, **flags) -> ColumnProfile:
    values = values or {}
    return ColumnProfile(
        name=name,
        inferred_type=inferred_type,
        null_rate=null_rate,
        distinct_count=distinct,
        sample_values=list(values),
        value_distribution=values,
        **flags,
    )


def _table(name, cols, rows=10) -> TableProfile:
    return TableProfile(name=name, row_count=rows, columns=cols, delimiter="|", source_file=f"{name}.dat")


def _pairs(clusters):
    return {frozenset(c.column_names()) for c in clusters if len(c.members) > 1}


# ── Name normalization ───────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("EFF_DT", "effective date"),
    ("POL_NO", "policy number"),
    ("ANNU_PREM_AMT", "annual premium amount"),
    ("effective_date", "effective date"),
    ("risk-score", "risk score"),
])
def test_normalize_name_expands_abbreviations(raw, expected):
    assert _normalize_name(raw) == expected


# ── Pair scoring ─────────────────────────────────────────────────────────────

def test_metadata_columns_never_match():
    a = _col("_cdc_timestamp", "date")
    b = _col("_cdc_timestamp", "date")
    assert score_pair(a, b, 10, 10) == (0.0, [])


def test_shape_alone_is_not_a_match():
    """Identical profiles with unrelated names and no shared vocabulary score
    zero: the gate can veto but never create a match."""
    a = _col("INS_ADDR", distinct=9)
    b = _col("carrier_xyz", distinct=9)
    assert score_pair(a, b, 10, 10) == (0.0, [])


def test_gate_only_reduces_a_name_match():
    a = _col("RSK_SCR", "decimal", distinct=10)
    b = _col("risk_score", "decimal", distinct=10)
    score, evidence = score_pair(a, b, 10, 10)
    # name-only matches are capped at 0.85 x name similarity (1.0 here)
    assert score == pytest.approx(0.85)
    assert evidence == ["name 1.00"]

    # Same names, incompatible shape: the gate pulls the score down, never up.
    c = _col("risk_score", "boolean", distinct=2, null_rate=0.9)
    worse, worse_evidence = score_pair(a, c, 10, 10)
    assert worse < score
    assert any(e.startswith("shape gate") for e in worse_evidence)


def test_value_overlap_leads_for_coded_columns():
    vals = {"N": 4, "S": 3, "E": 3}
    a = _col("REGN_CD", values=vals, distinct=3, is_coded_column=True)
    b = _col("sales_area", values=vals, distinct=3, is_coded_column=True)
    score, evidence = score_pair(a, b, 10, 10)
    assert evidence[0] == "value overlap 1.00"
    assert score >= 0.75  # 0.75 x overlap alone, before any name credit


# ── Clustering ───────────────────────────────────────────────────────────────

def test_unmatched_columns_survive_as_singletons():
    clusters = cluster_columns([
        ("a", _table("t_a", [_col("RSK_SCR", "decimal"), _col("FOO_BAR")])),
        ("b", _table("t_b", [_col("risk_score", "decimal"), _col("qux")])),
    ])
    assert _pairs(clusters) == {frozenset({"a.RSK_SCR", "b.risk_score"})}
    singles = {c.column_names()[0] for c in clusters if len(c.members) == 1}
    assert singles == {"a.FOO_BAR", "b.qux"}
    assert all(c.confidence == 0.0 for c in clusters if len(c.members) == 1)


def test_same_source_columns_never_cluster_together():
    clusters = cluster_columns([
        ("a", _table("t_a", [_col("RSK_SCR", "decimal"), _col("RISK_SCORE", "decimal")])),
        ("b", _table("t_b", [_col("risk_score", "decimal")])),
    ])
    for c in clusters:
        assert len(c.sources) == len(c.members)


def test_mutual_best_match_stops_a_column_claiming_its_second_choice(monkeypatch):
    """Scores: a2-b1 0.90, a1-b1 0.88, a1-b2 0.85.

    Greedy alone pairs a2-b1, then (a1 locked out of b1) falls through to
    a1-b2 - a match a1 ranks second. Mutual best keeps only a2-b1: a1's best
    in b is b1, not b2, so a1-b2 is dropped and a1/b2 stay singletons. This
    fails if the mutual-best filter is removed."""
    import schema_inference.cluster as cluster_mod

    fixed = {
        frozenset({"a1", "b1"}): 0.88,
        frozenset({"a1", "b2"}): 0.85,
        frozenset({"a2", "b1"}): 0.90,
    }
    monkeypatch.setattr(
        cluster_mod, "score_pair",
        lambda a, b, ra, rb: (fixed.get(frozenset({a.name, b.name}), 0.0), []),
    )
    clusters = cluster_columns([
        ("a", _table("t_a", [_col("a1"), _col("a2")])),
        ("b", _table("t_b", [_col("b1"), _col("b2")])),
    ])
    assert _pairs(clusters) == {frozenset({"a.a2", "b.b1"})}


def test_score_and_clusters_independent_of_source_order():
    """Regression: _TYPE_KIN was looked up one way only (date lists string,
    string does not list date), so a date x string pair scored 0.85-gated
    one way and 0.25-gated the other, and swapping the profiles on the
    command line changed the clusters."""
    a = _col("EFF_DT", "date", distinct=10)
    b = _col("effective_date", "string", distinct=10)
    assert score_pair(a, b, 10, 10) == score_pair(b, a, 10, 10)

    ta = ("a", _table("t_a", [a, _col("RSK_SCR", "decimal")]))
    tb = ("b", _table("t_b", [b, _col("risk_score", "decimal")]))
    assert _pairs(cluster_columns([ta, tb])) == _pairs(cluster_columns([tb, ta]))
    assert frozenset({"a.EFF_DT", "b.effective_date"}) in _pairs(cluster_columns([ta, tb]))


def test_thin_value_overlap_does_not_sink_a_strong_name_match():
    a = _col("CNCL_RSN_CD", values={"NP": 1, "UW": 1, "IR": 1, "OT": 1, "MV": 1}, is_coded_column=True)
    b = _col("cancellation_reason", values={"NP": 1, "XX": 1, "YY": 1, "ZZ": 1, "QQ": 1}, is_coded_column=True)
    score, evidence = score_pair(a, b, 10, 10)
    assert evidence[0].startswith("value overlap")
    assert score >= 0.85 * 0.72  # never below the name-only branch at the gate floor


@pytest.mark.parametrize("bad", [0.0, -0.1, 1.5])
def test_threshold_out_of_range_rejected(bad):
    t = ("a", _table("t_a", [_col("x")]))
    u = ("b", _table("t_b", [_col("y")]))
    with pytest.raises(ValueError, match="threshold"):
        cluster_columns([t, u], threshold=bad)


def test_three_sources_form_a_single_cluster():
    """Regression: mutual-best was tracked per column rather than per
    (column, other source), so with three sources each column's best partner
    in ONE source vetoed its pairs with every other source and no cluster
    could grow past two members."""
    clusters = cluster_columns([
        ("a", _table("t_a", [_col("RSK_SCR", "decimal")])),
        ("b", _table("t_b", [_col("risk_score", "decimal")])),
        ("c", _table("t_c", [_col("RISK_SCORE", "decimal")])),
    ])
    multi = [c for c in clusters if len(c.members) > 1]
    assert len(multi) == 1
    assert set(multi[0].column_names()) == {"a.RSK_SCR", "b.risk_score", "c.RISK_SCORE"}


def test_duplicate_source_names_rejected():
    t = _table("t", [_col("x")])
    with pytest.raises(ValueError, match="duplicate source names"):
        cluster_columns([("a", t), ("a", t)])
