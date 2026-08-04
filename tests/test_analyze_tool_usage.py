"""MAP-9 Step 2 -- tools/analyze_tool_usage.py's report logic, against
synthetic tool_usage_history + mapping_history rows (no live agent run
required)."""

from schema_inference.metamodel.store import MetamodelStore
from tools.analyze_tool_usage import run_tool_usage_analysis

_SIG = {
    "inferred_type": "string",
    "is_id_column": False,
    "is_coded_column": True,
    "is_cents_integer": False,
    "date_format": None,
}


def _seeded_store(tmp_path):
    return MetamodelStore(tmp_path / "metamodel.db")


def test_empty_store_returns_gracefully_with_zero_rows(tmp_path, monkeypatch, capsys):
    store = _seeded_store(tmp_path)
    monkeypatch.setattr("tools.analyze_tool_usage.open_store", lambda: store)
    try:
        result = run_tool_usage_analysis(source_name="pasl")
    finally:
        store.close()

    assert result == {"source_name": "pasl", "rows": 0}
    out = capsys.readouterr().out
    assert "No tool_usage_history found" in out


def test_store_unavailable_returns_gracefully(monkeypatch):
    monkeypatch.setattr("tools.analyze_tool_usage.open_store", lambda: None)
    result = run_tool_usage_analysis(source_name="pasl")
    assert result == {"source_name": "pasl", "rows": 0}


def test_marginal_value_and_under_triggering_flag_a_tool(tmp_path, monkeypatch):
    store = _seeded_store(tmp_path)
    monkeypatch.setattr("tools.analyze_tool_usage.open_store", lambda: store)
    try:
        # Column A: same profile-signature group, called check_value_catalog,
        # mapping came out correct.
        store.record_mapping(
            run_id="run1", source_name="pasl", table_name="pasl_policy",
            source_column="COL_A", target_field="region_code", confidence=0.9,
            method="llm", sql_expression="COL_A", verdict="TP",
            profile_signature=_SIG,
        )
        store.record_tool_usage(run_id="run1", source_name="pasl", traces=[
            {
                "column_name": "COL_A", "agent": "mapping",
                "tool_calls": [
                    {"tool_name": "check_value_catalog", "inputs": {"column": "COL_A"}, "output": "{}"},
                ],
            },
        ])

        # Column B: same group, did NOT call check_value_catalog, mapping wrong.
        store.record_mapping(
            run_id="run1", source_name="pasl", table_name="pasl_policy",
            source_column="COL_B", target_field="region_code", confidence=0.6,
            method="llm", sql_expression="COL_B", verdict="FP",
            profile_signature=_SIG,
        )
        store.record_tool_usage(run_id="run1", source_name="pasl", traces=[
            {
                "column_name": "COL_B", "agent": "mapping",
                "tool_calls": [
                    {"tool_name": "lookup_canonical", "inputs": {"name": "COL_B"}, "output": "{}"},
                ],
            },
        ])

        result = run_tool_usage_analysis(source_name="pasl")
    finally:
        store.close()

    assert result["rows"] == 2

    marginal_by_tool = {row["tool"]: row for row in result["marginal_value"]}
    assert "check_value_catalog" in marginal_by_tool
    row = marginal_by_tool["check_value_catalog"]
    assert row["called_acc"] == 1.0
    assert row["called_n"] == 1
    assert row["not_called_acc"] == 0.0
    assert row["not_called_n"] == 1
    assert row["delta"] == 1.0

    under_by_tool = {row["tool"]: row for row in result["under_triggering"]}
    assert "check_value_catalog" in under_by_tool
    urow = under_by_tool["check_value_catalog"]
    assert urow["error_rate_without"] == 1.0
    assert urow["error_rate_with"] == 0.0
    assert urow["delta"] == 1.0


def test_call_efficiency_flags_cutoff_and_duplicate_calls(tmp_path, monkeypatch):
    store = _seeded_store(tmp_path)
    monkeypatch.setattr("tools.analyze_tool_usage.open_store", lambda: store)
    monkeypatch.setattr("tools.analyze_tool_usage._max_tool_calls", lambda: 2)
    try:
        store.record_mapping(
            run_id="run2", source_name="pasl", table_name="pasl_policy",
            source_column="COL_C", target_field=None, confidence=0.3,
            method="llm", sql_expression="COL_C", verdict="TN",
            profile_signature=_SIG,
        )
        store.record_tool_usage(run_id="run2", source_name="pasl", traces=[
            {
                "column_name": "COL_C", "agent": "mapping",
                "tool_calls": [
                    {"tool_name": "lookup_canonical", "inputs": {"name": "COL_C"}, "output": "{}"},
                    {"tool_name": "lookup_canonical", "inputs": {"name": "COL_C"}, "output": "{}"},
                ],
            },
        ])

        result = run_tool_usage_analysis(source_name="pasl")
    finally:
        store.close()

    eff = result["call_efficiency"]
    assert eff["total"] == 1
    assert eff["max_tool_calls_per_column"] == 2
    assert eff["cutoff_count"] == 1
    assert eff["cutoff_pct"] == 1.0
    assert eff["duplicate_count"] == 1


def test_columns_without_scored_verdict_are_excluded_from_marginal_value(tmp_path, monkeypatch):
    store = _seeded_store(tmp_path)
    monkeypatch.setattr("tools.analyze_tool_usage.open_store", lambda: store)
    try:
        # No mapping_history row at all for this column -> verdict is None,
        # must not be treated as a scored outcome anywhere.
        store.record_tool_usage(run_id="run3", source_name="pasl", traces=[
            {
                "column_name": "COL_D", "agent": "mapping",
                "tool_calls": [
                    {"tool_name": "lookup_canonical", "inputs": {}, "output": "{}"},
                ],
            },
        ])

        result = run_tool_usage_analysis(source_name="pasl")
    finally:
        store.close()

    assert result["rows"] == 1
    assert result["marginal_value"] == []
    assert result["under_triggering"] == []
