from schema_inference.metamodel.store import MetamodelStore
from tools.curate_few_shot_bank import curate


def _seeded_store(tmp_path):
    return MetamodelStore(tmp_path / "metamodel.db")


def test_curate_inserts_hard_tp_and_critic_override_examples(tmp_path, monkeypatch):
    store = _seeded_store(tmp_path)
    monkeypatch.setattr("tools.curate_few_shot_bank.open_store", lambda: store)

    # INS_ST is a real is_hard=true column in pasl's ground-truth catalog
    # (confirmed via _load_hard_columns -- this test would silently see
    # zero hard_tp inserts if GROUND_TRUTH_DIR pointed at the wrong path).
    store.record_mapping(
        run_id="run1", source_name="pasl", table_name="pasl_policy",
        source_column="INS_ST", target_field="region_code", confidence=0.95,
        method="rule", sql_expression="INS_ST", verdict="TP",
        profile_signature={"inferred_type": "string"},
    )
    # A critic-overridden mapping the reviewer accepted.
    store.record_mapping(
        run_id="run1", source_name="pasl", table_name="pasl_policy",
        source_column="WRTG_AGT", target_field="agent_id", confidence=0.6,
        method="critic", sql_expression="WRTG_AGT", reviewer_action="accepted",
        profile_signature={"inferred_type": "string"},
    )
    # A hard TP with no profile_signature -- pre-Layer-1 history, must be skipped.
    store.record_mapping(
        run_id="run1", source_name="pasl", table_name="pasl_policy",
        source_column="PROD_CD", target_field="product_code", confidence=0.9,
        method="rule", sql_expression="PROD_CD", verdict="TP",
    )

    try:
        counts = curate("pasl")
    finally:
        store.close()

    assert counts["hard_tp_inserted"] == 1
    assert counts["critic_inserted"] == 1
    assert counts["skipped_no_signature"] == 1
    assert counts["skipped_existing"] == 0

    store = _seeded_store(tmp_path)
    active = store.get_few_shot_examples("pasl", status="active")
    store.close()
    by_column = {r["source_column"]: r for r in active}
    assert by_column["INS_ST"]["origin"] == "hard_tp"
    assert by_column["WRTG_AGT"]["origin"] == "critic_override_accepted"


def test_curate_skips_columns_already_in_the_bank(tmp_path, monkeypatch):
    store = _seeded_store(tmp_path)
    monkeypatch.setattr("tools.curate_few_shot_bank.open_store", lambda: store)

    store.record_mapping(
        run_id="run1", source_name="pasl", table_name="pasl_policy",
        source_column="INS_ST", target_field="region_code", confidence=0.95,
        method="rule", sql_expression="INS_ST", verdict="TP",
        profile_signature={"inferred_type": "string"},
    )
    store.add_few_shot_example(
        source_name="pasl", source_column="INS_ST", target_field="region_code",
        sql_expression="INS_ST", reasoning="already banked",
        profile_signature={"inferred_type": "string"}, origin="hard_tp",
    )

    try:
        counts = curate("pasl")
    finally:
        store.close()

    assert counts["hard_tp_inserted"] == 0
    assert counts["skipped_existing"] == 1
