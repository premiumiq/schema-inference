import sys
from types import SimpleNamespace

sys.path.insert(0, "scripts")
from score_mappings import ColumnScore  # noqa: E402

import tools.tune_prompts as tp  # noqa: E402

FIXTURE = "examples/insurance/test_data/pasl_policy.dat"


def _score(col, verdict, correct):
    return ColumnScore(
        column_name=col, gt_target="policy_id", mapper_target="policy_id" if correct else None,
        mapper_confidence=0.9, correct=correct, is_hard=False, confidence_floor=None,
        below_floor=False, verdict=verdict, sql_correct=None,
        calibration_penalty=0.0, loss=0.1,
    )


def _stub_out_llm_and_heavy_calls(monkeypatch, holdout_improves: bool):
    """Every _run_and_score call is real agent-pipeline + real LLM cost
    (run_mapping(use_agent=True) inside it) -- stubbed out entirely so this
    test proves on_round's plumbing, not the tuning loop's actual quality.
    summarize_failures/propose_edit are also stubbed rather than given fake
    Anthropic clients, since their real behavior isn't what's under test
    here (diff-ratio guardrails, JSON extraction, etc. are tune_prompts.py's
    existing, separately-testable concerns)."""
    def fake_run_and_score(data_file, source_name, columns_subset, mapping_prompt=None, critic_prompt=None, label=""):
        if "HOLDOUT" in label and "BASELINE" not in label:
            return SimpleNamespace(mean_loss=0.1 if holdout_improves else 0.9), [_score("POL_NO", "TP", True)]
        return SimpleNamespace(mean_loss=0.5), [_score("POL_NO", "FN", False)]

    monkeypatch.setattr(tp, "_run_and_score", fake_run_and_score)
    monkeypatch.setattr(tp, "summarize_failures", lambda failures, current_prompt, client=None: {"failure_mode": "fake"})
    monkeypatch.setattr(
        tp, "propose_edit",
        lambda current_prompt, failure_mode, client=None: {"prompt": current_prompt + " EDIT", "rationale": "x", "diff_ratio": 0.9},
    )
    monkeypatch.setattr(tp, "open_store", lambda: None)
    monkeypatch.setattr(tp, "_current_prompt_text", lambda agent_name: "BASE PROMPT")
    monkeypatch.setattr(tp, "check_determinism", lambda *a, **k: {"losses": [], "mean": 0, "stdev": 0})


def test_on_round_fires_once_per_round_with_the_rounds_log_entry(monkeypatch):
    _stub_out_llm_and_heavy_calls(monkeypatch, holdout_improves=True)
    seen: list[tuple[int, dict]] = []

    result = tp.run_tuning_session(
        agent_name="mapping", source_name="pasl", data_file=FIXTURE,
        max_rounds=2, early_stop_after=3,
        on_round=lambda n, info: seen.append((n, info)),
    )

    assert len(seen) == 2
    assert [n for n, _ in seen] == [1, 2]
    assert seen[0][1]["improved"] is True
    assert seen[0][1]["loss_after"] == 0.1
    assert result["rounds"] == [info for _, info in seen]


def test_on_round_none_is_a_no_op(monkeypatch):
    _stub_out_llm_and_heavy_calls(monkeypatch, holdout_improves=False)
    # Must not raise -- on_round defaults to None for every existing caller
    # (CLI's main(), tests that predate this hook).
    result = tp.run_tuning_session(
        agent_name="mapping", source_name="pasl", data_file=FIXTURE,
        max_rounds=1, early_stop_after=3,
    )
    assert result["rounds"][0]["improved"] is False
