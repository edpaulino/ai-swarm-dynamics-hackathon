"""Pre-registered configuration: models, conditions, stages, provenance helpers.

Per-model reasoning settings and max_connections live on each Model (get_model(config=...)).
They are never passed at the eval level, because eval-level settings override every model's
own config (the vendored eval.run_eval_set does that, so it is not used).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from inspect_ai.model import GenerateConfig, Model, get_model

from highagency.whistlebench.storylines import EXPERIMENT_STORYLINES
from highagency.whistlebench.types import (
    BoldnessPromptDetail,
    HighAgencyEvalParams,
    ReportToolCondition,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PREREG_TAG = "prereg-v1"

GRADER_MODEL = "openai/gpt-5-mini-2025-08-07"
DRYRUN_GRADER_MODEL = "mockllm/grader"

CONDITIONS: tuple[ReportToolCondition, ...] = ("none", "minimal", "informative")
STORYLINES: tuple[str, ...] = EXPERIMENT_STORYLINES
MODEL_KEYS: tuple[str, ...] = ("gpt5", "gemini", "sonnet")

# (group, reference) pairs; each model gets these 3 comparisons, 9 tests in all.
COMPARISONS: tuple[tuple[str, str], ...] = (
    ("minimal", "none"),
    ("informative", "none"),
    ("informative", "minimal"),
)

GeminiVia = Literal["google", "openrouter"]
Stage = Literal["dryrun", "smoke", "stage1", "posctrl", "stage2"]
STAGES: tuple[str, ...] = ("dryrun", "smoke", "stage1", "posctrl", "stage2")
PAID_STAGES: tuple[str, ...] = ("smoke", "stage1", "posctrl", "stage2")

GEMINI_NAMES: dict[str, str] = {
    "google": "google/gemini-2.5-pro",
    "openrouter": "openrouter/google/gemini-2.5-pro",
}

# Environment variable each provider prefix needs (names only; values are never read here).
PROVIDER_KEY_VARS: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


@dataclass(frozen=True)
class ModelSpec:
    key: str
    name: str
    config: GenerateConfig


def model_specs(
    gemini_via: GeminiVia = "google", reasoning_summary: bool = True
) -> dict[str, ModelSpec]:
    """The three agent models with their own generate configs.

    gemini_via="openrouter" and reasoning_summary=False are the two pre-registered fallbacks
    (deviations, recorded in each log's eval metadata).
    """
    return {
        "gpt5": ModelSpec(
            key="gpt5",
            name="openai/gpt-5-2025-08-07",
            config=GenerateConfig(
                reasoning_effort="medium",
                reasoning_summary="detailed" if reasoning_summary else None,
                max_connections=10,
            ),
        ),
        "gemini": ModelSpec(
            key="gemini",
            name=GEMINI_NAMES[gemini_via],
            # The paper's thinking budget. The google provider sends it as
            # thinking_config.thinking_budget; openrouter sends reasoning.max_tokens.
            config=GenerateConfig(reasoning_tokens=1536, max_connections=10),
        ),
        "sonnet": ModelSpec(
            key="sonnet",
            name="anthropic/claude-sonnet-5-5",
            # No reasoning_tokens: Claude 5.x rejects a thinking budget; reasoning_effort maps
            # to adaptive thinking with output_config.effort.
            config=GenerateConfig(reasoning_effort="medium", max_connections=6),
        ),
    }


def model_key_for(model_name: str) -> str | None:
    """Map a logged model name (agent or mock slot) to its key; None for graders/unknown."""
    name = model_name.lower()
    if name.startswith("mockllm/"):
        slot = name.split("/", 1)[1]
        return slot if slot in MODEL_KEYS else None
    if "gpt-5-mini" in name:
        return None
    if "gpt-5" in name:
        return "gpt5"
    if "gemini-2.5-pro" in name:
        return "gemini"
    if "claude-sonnet-5-5" in name:
        return "sonnet"
    return None


def build_models(
    keys: tuple[str, ...] | list[str],
    gemini_via: GeminiVia = "google",
    reasoning_summary: bool = True,
    mock: bool = False,
) -> list[Model]:
    specs = model_specs(gemini_via, reasoning_summary)
    return [
        get_model(f"mockllm/{key}" if mock else specs[key].name, config=specs[key].config)
        for key in keys
    ]


def experiment_params(
    report_tool: ReportToolCondition,
    storyline: str,
    boldness: BoldnessPromptDetail = "none",
    grader_model: str = GRADER_MODEL,
) -> HighAgencyEvalParams:
    """Summarization task, bare environment (routine documents on, no extra tools, no workflow
    instructions), no boldness nudge unless stated (the positive control uses "medium")."""
    return HighAgencyEvalParams(
        storyline=storyline,  # type: ignore[arg-type]
        agent_responsibility="summarization",
        boldness_prompt_detail=boldness,
        include_environment_and_workflow_details=False,
        include_distractor_docs=True,
        include_distractor_tools=False,
        include_harmful_tools=False,
        include_evaluation_deliberation=False,
        grader_model=grader_model,
        use_loop_protection=False,
        report_tool=report_tool,
    )


STAGE_EPOCHS: dict[str, int] = {"dryrun": 1, "smoke": 1, "stage1": 12, "posctrl": 12, "stage2": 24}


@dataclass(frozen=True)
class StagePlan:
    stage: str
    model_keys: tuple[str, ...]
    params: tuple[HighAgencyEvalParams, ...]
    epochs: int

    @property
    def paid(self) -> bool:
        return self.stage in PAID_STAGES

    @property
    def runs_per_model(self) -> int:
        return len(self.params) * self.epochs

    @property
    def total_runs(self) -> int:
        return self.runs_per_model * len(self.model_keys)


def stage_plan(
    stage: str,
    model_keys: tuple[str, ...] | list[str] | None = None,
    epochs: int | None = None,
) -> StagePlan:
    """Tasks, models and epochs for a stage.

    dryrun: Stage 1 design on mockllm (36 tasks at 1 epoch), $0.
    smoke: per model, Informative x 4 storylines + No tool x 1 storyline; 15 runs.
    stage1: 3 conditions x 4 storylines x 12 epochs per model; 432 runs.
    posctrl: Gemini, "medium" nudge, No tool, 4 storylines x 12 epochs; 48 runs.
    stage2: 3 conditions x 4 storylines x 24 epochs per continuing model; 288 runs each.
    """
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}")
    if epochs is not None and stage != "dryrun":
        raise ValueError("epochs are pre-registered; only dryrun accepts an override")
    grader = DRYRUN_GRADER_MODEL if stage == "dryrun" else GRADER_MODEL

    if stage == "posctrl":
        default_keys: tuple[str, ...] = ("gemini",)
        params = tuple(experiment_params("none", s, "medium", grader) for s in STORYLINES)
    elif stage == "smoke":
        default_keys = MODEL_KEYS
        params = tuple(experiment_params("informative", s, "none", grader) for s in STORYLINES) + (
            experiment_params("none", STORYLINES[0], "none", grader),
        )
    else:
        default_keys = MODEL_KEYS
        params = tuple(
            experiment_params(c, s, "none", grader) for c in CONDITIONS for s in STORYLINES
        )

    keys = tuple(model_keys) if model_keys else default_keys
    unknown = [k for k in keys if k not in MODEL_KEYS]
    if unknown:
        raise ValueError(f"unknown model keys {unknown}; choose from {MODEL_KEYS}")
    return StagePlan(
        stage=stage,
        model_keys=keys,
        params=params,
        epochs=epochs if epochs is not None else STAGE_EPOCHS[stage],
    )


# ---------- provenance ----------

CODE_PATHS = ("highagency", "experiment", "pyproject.toml", "uv.lock")


def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def code_state() -> dict[str, object]:
    """Current commit, whether tracked code differs from it, and the pre-registration tag."""
    commit = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain", "--untracked-files=no", "--", *CODE_PATHS)
    tag_commit = _git("rev-parse", "-q", "--verify", f"refs/tags/{PREREG_TAG}^{{commit}}")
    return {
        "code_commit": commit,
        "code_dirty": bool(status),
        "prereg_tag": PREREG_TAG,
        "prereg_tag_commit": tag_commit,
        "describe": _git("describe", "--tags", "--always", "--dirty"),
    }
