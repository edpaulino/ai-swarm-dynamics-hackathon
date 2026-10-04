"""Per-model configs, stage plans and run.py guards (no API calls; dummy keys only)."""

import json
from pathlib import Path

import pytest
from inspect_ai.model import GenerateConfig, get_model

from experiment import config, run

FAKE_KEY = "test-key-not-real"


def test_anthropic_has_no_reasoning_tokens():
    spec = config.model_specs()["sonnet"]
    assert spec.name == "anthropic/claude-sonnet-5-5"
    assert spec.config.reasoning_tokens is None
    assert spec.config.reasoning_effort == "medium"
    assert spec.config.max_connections == 6


@pytest.mark.parametrize(
    "via, name",
    [("google", "google/gemini-2.5-pro"), ("openrouter", "openrouter/google/gemini-2.5-pro")],
)
def test_gemini_budget_is_1536_on_both_routes(via: str, name: str):
    spec = config.model_specs(gemini_via=via)["gemini"]  # type: ignore[arg-type]
    assert spec.name == name
    assert spec.config.reasoning_tokens == 1536
    assert spec.config.reasoning_effort is None
    assert spec.config.max_connections == 10


def test_gpt5_reasoning_summary_flag():
    with_summary = config.model_specs()["gpt5"].config
    without = config.model_specs(reasoning_summary=False)["gpt5"].config
    assert with_summary.reasoning_effort == without.reasoning_effort == "medium"
    assert with_summary.reasoning_summary == "detailed"
    assert without.reasoning_summary is None
    assert with_summary.max_connections == 10


def test_no_eval_level_reasoning_settings():
    kwargs = run.eval_set_kwargs(Path("logs/x"), epochs=12, metadata={})
    assert not [k for k in kwargs if k.startswith("reasoning") or k == "max_connections"]
    assert kwargs["max_tasks"] == 6
    assert kwargs["retry_on_error"] == 3


def test_model_key_mapping():
    assert config.model_key_for("openai/gpt-5-2025-08-07") == "gpt5"
    assert config.model_key_for("openai/gpt-5-mini-2025-08-07") is None
    assert config.model_key_for("google/gemini-2.5-pro") == "gemini"
    assert config.model_key_for("openrouter/google/gemini-2.5-pro") == "gemini"
    assert config.model_key_for("anthropic/claude-sonnet-5-5") == "sonnet"
    assert config.model_key_for("mockllm/sonnet") == "sonnet"
    assert config.model_key_for("mockllm/grader") is None


# ---------- provider request mapping (clients built with dummy keys; nothing is sent) ----------


def test_openrouter_maps_budget_to_reasoning_max_tokens(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    spec = config.model_specs(gemini_via="openrouter")["gemini"]
    api = get_model(spec.name, config=spec.config, memoize=False).api
    params = api.completion_params(spec.config, tools=True)  # type: ignore[attr-defined]
    assert params["extra_body"]["reasoning"] == {"max_tokens": 1536}


def test_google_maps_budget_to_thinking_budget(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GOOGLE_API_KEY", FAKE_KEY)
    spec = config.model_specs()["gemini"]
    api = get_model(spec.name, config=spec.config, memoize=False).api
    thinking = api.chat_thinking_config(spec.config)  # type: ignore[attr-defined]
    assert thinking.thinking_budget == 1536


def test_anthropic_effort_maps_to_adaptive_thinking(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    spec = config.model_specs()["sonnet"]
    api = get_model(spec.name, config=spec.config, memoize=False).api
    cfg = spec.config.merge(GenerateConfig(max_tokens=api.max_tokens_for_config(spec.config)))  # type: ignore[attr-defined]
    params, _, _, _ = api.completion_config(cfg)  # type: ignore[attr-defined]
    assert params["thinking"]["type"] == "adaptive"
    assert "budget_tokens" not in params["thinking"]
    assert params["output_config"]["effort"] == "medium"
    # A thinking budget is rejected up front for Claude 5.x, which is why none is set.
    with pytest.raises(Exception, match="reasoning_tokens"):
        api.completion_config(cfg.merge(GenerateConfig(reasoning_tokens=1536)))  # type: ignore[attr-defined]


# ---------- stage plans ----------


@pytest.mark.parametrize(
    "stage, per_model, models, total",
    [
        ("dryrun", 12, 3, 36),
        ("smoke", 5, 3, 15),
        ("stage1", 144, 3, 432),
        ("posctrl", 48, 1, 48),
        ("stage2", 288, 3, 864),
    ],
)
def test_stage_run_counts(stage: str, per_model: int, models: int, total: int):
    plan = config.stage_plan(stage)
    assert plan.runs_per_model == per_model
    assert len(plan.model_keys) == models
    assert plan.total_runs == total


def test_stage_designs():
    smoke = config.stage_plan("smoke")
    assert sorted((p.report_tool, p.storyline) for p in smoke.params) == sorted(
        [("informative", s) for s in config.STORYLINES] + [("none", "autonomous_vehicles")]
    )
    stage1 = config.stage_plan("stage1")
    assert {(p.report_tool, p.storyline) for p in stage1.params} == {
        (c, s) for c in config.CONDITIONS for s in config.STORYLINES
    }
    for p in stage1.params:
        assert p.boldness_prompt_detail == "none"
        assert p.agent_responsibility == "summarization"
        assert p.include_distractor_docs and not p.include_distractor_tools
        assert not p.include_environment_and_workflow_details
        assert p.grader_model == config.GRADER_MODEL
    posctrl = config.stage_plan("posctrl")
    assert posctrl.model_keys == ("gemini",)
    assert {(p.report_tool, p.boldness_prompt_detail) for p in posctrl.params} == {
        ("none", "medium")
    }
    assert config.stage_plan("dryrun").params[0].grader_model == "mockllm/grader"


def test_paid_epochs_cannot_be_overridden():
    with pytest.raises(ValueError):
        config.stage_plan("stage1", epochs=2)


# ---------- run.py guards (all return before any eval) ----------


def _cost_json(tmp_path: Path, usd: float) -> Path:
    path = tmp_path / "cost_per_run.json"
    by_model = {k: {"mean_usd_total": usd} for k in config.MODEL_KEYS}
    path.write_text(json.dumps({"by_model": by_model}), encoding="utf-8")
    return path


def test_projection_over_budget_refuses(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    path = _cost_json(tmp_path, usd=0.20)  # 432 runs x $0.20 = $86.40 > $75
    code = run.main(["--stage", "stage1", "--cost-per-run-json", str(path), "--i-approve-spend"])
    assert code == 2
    out = capsys.readouterr().out
    assert "projected total   $   86.40" in out
    assert "exceeds --max-usd" in out


def test_paid_stage_needs_approval_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    path = _cost_json(tmp_path, usd=0.05)
    assert run.main(["--stage", "stage1", "--cost-per-run-json", str(path)]) == 2
    assert "--i-approve-spend" in capsys.readouterr().out


def test_stage1_needs_measured_cost(capsys: pytest.CaptureFixture[str]):
    assert run.main(["--stage", "stage1", "--i-approve-spend"]) == 2
    assert "--cost-per-run-json" in capsys.readouterr().out


def test_smoke_without_approval_prints_plan(capsys: pytest.CaptureFixture[str]):
    assert run.main(["--stage", "smoke"]) == 2
    out = capsys.readouterr().out
    assert "total   15 runs" in out
    assert "openai/gpt-5-2025-08-07" in out


def test_project_cost_requires_every_model():
    plan = config.stage_plan("stage1")
    with pytest.raises(KeyError):
        run.project_cost(plan, {"gpt5": 0.1})
    assert run.project_cost(plan, {"gpt5": 0.1, "gemini": 0.1, "sonnet": 0.1}) == pytest.approx(
        {"gpt5": 14.4, "gemini": 14.4, "sonnet": 14.4}
    )


def test_stage2_refuses_incomplete_review(tmp_path: Path):
    path = tmp_path / "stopping_rule.json"
    path.write_text(json.dumps({"review_complete": False, "models": {}}), encoding="utf-8")
    with pytest.raises(SystemExit, match="incomplete"):
        run.continuing_models(path)
    path.write_text(
        json.dumps(
            {
                "review_complete": True,
                "models": {"gpt5": {"continue": False}, "sonnet": {"continue": True}},
            }
        ),
        encoding="utf-8",
    )
    assert run.continuing_models(path) == ["sonnet"]


def test_network_is_blocked_in_tests():
    import socket

    with pytest.raises(RuntimeError, match="blocked"):
        socket.getaddrinfo("api.openai.com", 443)
