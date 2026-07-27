from schema_inference.canonical.policy import CANONICAL_NAMES
from schema_inference.models import ColumnMapping
from schema_inference.reviewer import (
    UnknownTargetFieldError,
    accept_mapping,
    apply_contest_resolution,
    assign_extended_attr,
    modify_mapping,
    resolve_missing_field,
    skip_mapping,
)

import pytest


def mk(col, target, conf=0.8, method="rule", sql=None, notes=""):
    return ColumnMapping(
        source_column=col, source_table="t", target_field=target, confidence=conf,
        method=method, sql_expression=sql or col, notes=notes,
    )


def test_accept_mapping_preserves_original_fields():
    m = mk("POL_NO", "policy_id", conf=0.91, sql="CAST(POL_NO AS VARCHAR)", notes="rule match")
    am = accept_mapping(m)
    assert am.target_field == "policy_id"
    assert am.sql_expression == "CAST(POL_NO AS VARCHAR)"
    assert am.method == "rule"
    assert am.reviewer_action == "accepted"


def test_skip_mapping_routes_to_extended_attributes():
    m = mk("X", "region_code")
    am = skip_mapping(m)
    assert am.target_field is None
    assert am.sql_expression == "X"
    assert am.reviewer_action == "skipped"


def test_modify_mapping_blank_target_routes_to_extended():
    m = mk("X", "region_code")
    am = modify_mapping(m, target_field=None, sql_expression="X", notes="unsure")
    assert am.target_field is None
    assert am.method == "manual"
    assert am.reviewer_action == "modified"


def test_modify_mapping_valid_target_accepted():
    m = mk("POL_NO", None, conf=0.4)
    target = next(iter(CANONICAL_NAMES))
    am = modify_mapping(m, target_field=target, sql_expression="POL_NO", notes="manual pick")
    assert am.target_field == target
    assert am.method == "manual"


def test_modify_mapping_unknown_target_raises():
    m = mk("X", None)
    with pytest.raises(UnknownTargetFieldError):
        modify_mapping(m, target_field="not_a_real_field", sql_expression="X", notes="")


def test_resolve_missing_field_null():
    r = resolve_missing_field("effective_date", "NULL")
    assert r.resolution == "NULL"
    assert r.hardcoded_value is None


def test_resolve_missing_field_hardcoded():
    r = resolve_missing_field("state_code", "HARDCODED", hardcoded_value="TX")
    assert r.resolution == "HARDCODED"
    assert r.hardcoded_value == "TX"


def test_resolve_missing_field_derived():
    r = resolve_missing_field("premium_amount", "DERIVED", derivation_sql="0")
    assert r.resolution == "DERIVED"
    assert r.derivation_sql == "0"


def test_apply_contest_resolution_winner_keeps_target_loser_demoted():
    approved_by_col = {
        "X": accept_mapping(mk("X", "region_code", conf=0.80)),
        "Y": accept_mapping(mk("Y", "region_code", conf=0.79)),
    }
    apply_contest_resolution("region_code", ["X", "Y"], "X", approved_by_col)
    assert approved_by_col["X"].target_field == "region_code"
    assert approved_by_col["X"].reviewer_action == "modified"
    assert approved_by_col["Y"].target_field is None
    assert approved_by_col["Y"].reviewer_action == "modified"


def test_apply_contest_resolution_no_winner_demotes_all():
    approved_by_col = {
        "X": accept_mapping(mk("X", "region_code")),
        "Y": accept_mapping(mk("Y", "region_code")),
    }
    apply_contest_resolution("region_code", ["X", "Y"], None, approved_by_col)
    assert approved_by_col["X"].target_field is None
    assert approved_by_col["Y"].target_field is None


def test_assign_extended_attr_keep_as_extended():
    kept, warning = assign_extended_attr("COL", keep_as_extended=True)
    assert kept is True
    assert warning is None


def test_assign_extended_attr_map_to_known_field_not_kept_but_warned():
    target = next(iter(CANONICAL_NAMES))
    kept, warning = assign_extended_attr("COL", keep_as_extended=False, target=target)
    assert kept is False
    assert warning is not None


def test_assign_extended_attr_map_to_unknown_field_falls_back_to_kept():
    kept, warning = assign_extended_attr("COL", keep_as_extended=False, target="not_a_real_field")
    assert kept is True
    assert warning is None
