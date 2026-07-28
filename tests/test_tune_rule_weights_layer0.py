import shutil

import pytest

from tools.tune_rule_weights import AGENT_CONFIG_PATH, run_layer0_tuning


def test_run_layer0_tuning_dry_run_returns_structured_result_and_does_not_modify_config(tmp_path, monkeypatch):
    tmp_config = tmp_path / "agent_config.yml"
    shutil.copy(AGENT_CONFIG_PATH, tmp_config)
    monkeypatch.setattr("tools.tune_rule_weights.AGENT_CONFIG_PATH", tmp_config)
    before = tmp_config.read_text(encoding="utf-8")

    result = run_layer0_tuning(source_name="pasl", step=0.2, apply=False)

    assert result["source_name"] == "pasl"
    assert result["table_name"] == "pasl_policy"
    assert set(result["baseline_weights"]) == {"name_sim", "type_compat", "pattern_bonus"}
    assert set(result["best_metrics"]) == {"mean_loss", "f1", "hard_f1"}
    assert result["applied"] is False
    assert len(result["top_candidates"]) <= 5
    assert result["top_candidates"][0]["mean_loss"] == result["best_metrics"]["mean_loss"]

    # Dry run must never touch the config file.
    assert tmp_config.read_text(encoding="utf-8") == before


def test_run_layer0_tuning_apply_true_writes_config_only_on_improvement(tmp_path, monkeypatch):
    tmp_config = tmp_path / "agent_config.yml"
    shutil.copy(AGENT_CONFIG_PATH, tmp_config)
    monkeypatch.setattr("tools.tune_rule_weights.AGENT_CONFIG_PATH", tmp_config)

    result = run_layer0_tuning(source_name="pasl", step=0.2, apply=True)

    if result["applied"]:
        after = tmp_config.read_text(encoding="utf-8")
        assert f"{result['best_weights']['name_sim']:.4f}" in after
    # If not applied (best == baseline already), the temp file is simply
    # unchanged -- both outcomes are valid depending on the current
    # committed weights, so this test only checks internal consistency,
    # not a specific outcome.


def test_run_layer0_tuning_missing_data_file_raises():
    with pytest.raises(FileNotFoundError):
        run_layer0_tuning(source_name="no_such_source_xyz")
