"""MAP-9 Step 1 -- MetamodelStore.record_tool_usage() / get_tool_usage_history()."""

import json

from schema_inference.metamodel.store import MetamodelStore
from schema_inference.models import AgentToolCall, AgentTrace


def _seeded_store(tmp_path):
    return MetamodelStore(tmp_path / "metamodel.db")


def test_record_tool_usage_round_trips_agent_trace_objects(tmp_path):
    store = _seeded_store(tmp_path)
    try:
        trace = AgentTrace(
            column_name="INS_ST",
            agent="mapping",
            tool_calls=[
                AgentToolCall(
                    tool_name="lookup_canonical",
                    inputs={"name": "INS_ST"},
                    output='{"candidates": ["region_code"]}',
                ),
                AgentToolCall(
                    tool_name="check_value_catalog",
                    inputs={"column": "INS_ST"},
                    output='{"type": "string"}',
                ),
            ],
            final_target="region_code",
            final_confidence=0.9,
            reasoning_summary="matched region code",
        )

        n = store.record_tool_usage(run_id="run1", source_name="pasl", traces=[trace])
        assert n == 2

        rows = store.get_tool_usage_history("pasl")
        assert len(rows) == 2
        rows.sort(key=lambda r: r["call_order"])

        assert rows[0]["tool_name"] == "lookup_canonical"
        assert rows[0]["call_order"] == 0
        assert rows[0]["source_column"] == "INS_ST"
        assert rows[0]["agent"] == "mapping"
        assert rows[0]["run_id"] == "run1"
        assert json.loads(rows[0]["inputs_json"]) == {"name": "INS_ST"}

        assert rows[1]["tool_name"] == "check_value_catalog"
        assert rows[1]["call_order"] == 1
    finally:
        store.close()


def test_record_tool_usage_accepts_plain_dicts(tmp_path):
    store = _seeded_store(tmp_path)
    try:
        trace = {
            "column_name": "PROD_CD",
            "agent": "mapping",
            "tool_calls": [
                {"tool_name": "get_column_profile", "inputs": {}, "output": "{}"},
            ],
        }
        n = store.record_tool_usage(run_id="run2", source_name="pasl", traces=[trace])
        assert n == 1

        rows = store.get_tool_usage_history("pasl", run_id="run2")
        assert len(rows) == 1
        assert rows[0]["tool_name"] == "get_column_profile"
        assert rows[0]["source_column"] == "PROD_CD"
    finally:
        store.close()


def test_record_tool_usage_writes_multiple_traces_with_independent_call_orders(tmp_path):
    store = _seeded_store(tmp_path)
    try:
        traces = [
            {
                "column_name": "COL_A",
                "agent": "mapping",
                "tool_calls": [
                    {"tool_name": "t1", "inputs": {}, "output": "{}"},
                    {"tool_name": "t2", "inputs": {}, "output": "{}"},
                ],
            },
            {
                "column_name": "COL_B",
                "agent": "critic",
                "tool_calls": [
                    {"tool_name": "t3", "inputs": {}, "output": "{}"},
                ],
            },
        ]
        n = store.record_tool_usage(run_id="run3", source_name="pasl", traces=traces)
        assert n == 3

        rows = store.get_tool_usage_history("pasl", run_id="run3")
        col_a_orders = sorted(r["call_order"] for r in rows if r["source_column"] == "COL_A")
        col_b_orders = sorted(r["call_order"] for r in rows if r["source_column"] == "COL_B")
        assert col_a_orders == [0, 1]
        assert col_b_orders == [0]
    finally:
        store.close()


def test_record_tool_usage_empty_traces_writes_nothing(tmp_path):
    store = _seeded_store(tmp_path)
    try:
        n = store.record_tool_usage(run_id="run4", source_name="pasl", traces=[])
        assert n == 0
        assert store.get_tool_usage_history("pasl") == []
    finally:
        store.close()


def test_record_tool_usage_never_raises_on_malformed_trace(tmp_path):
    store = _seeded_store(tmp_path)
    try:
        # tool_calls is not iterable -- would raise TypeError internally,
        # must be swallowed and return 0 rather than propagate.
        n = store.record_tool_usage(
            run_id="run5", source_name="pasl",
            traces=[{"column_name": "X", "agent": "mapping", "tool_calls": 5}],
        )
        assert n == 0

        # A trace object with none of the expected attributes at all.
        n2 = store.record_tool_usage(run_id="run5", source_name="pasl", traces=[object()])
        assert n2 == 0
    finally:
        store.close()


def test_record_tool_usage_never_raises_on_closed_store(tmp_path):
    store = _seeded_store(tmp_path)
    store.close()

    trace = {
        "column_name": "X",
        "agent": "mapping",
        "tool_calls": [{"tool_name": "t", "inputs": {}, "output": "{}"}],
    }
    n = store.record_tool_usage(run_id="run6", source_name="pasl", traces=[trace])
    assert n == 0
