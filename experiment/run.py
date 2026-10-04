"""Run one pre-registered stage.

    uv run python -m experiment.run --stage dryrun
    uv run python -m experiment.run --stage smoke --i-approve-spend
    uv run python -m experiment.run --stage stage1 --i-approve-spend \
        --cost-per-run-json results/smoke/cost_per_run.json

Paid stages need --i-approve-spend, the prereg-v1 tag and clean tracked code. Stage 1,
the positive control and Stage 2 also need a measured cost per run (from analyzing the smoke
stage) and refuse to start if the projection exceeds --max-usd.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from experiment import config

DEFAULT_MAX_USD = 75.0
MAX_TASKS = 6
RETRY_ON_ERROR = 3
MAX_TOOL_OUTPUT = 320 * 1024  # as upstream: keep the document bundle from being truncated
STOPPING_RULE_PATH = Path("results/stage1/stopping_rule.json")
STAGES_NEEDING_COST = ("stage1", "posctrl", "stage2")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--stage", required=True, choices=config.STAGES)
    p.add_argument("--models", nargs="+", choices=config.MODEL_KEYS, help="subset of models")
    p.add_argument("--i-approve-spend", action="store_true", help="allow paid API calls")
    p.add_argument(
        "--gemini-via",
        choices=["google", "openrouter"],
        default="google",
        help="deviation: route Gemini 2.5 Pro through OpenRouter (same thinking budget)",
    )
    p.add_argument(
        "--no-reasoning-summary",
        action="store_true",
        help="deviation: drop GPT-5 reasoning_summary (e.g. 400 for an unverified org)",
    )
    p.add_argument(
        "--cost-per-run-json", type=Path, help="written by analyze.py on the smoke stage"
    )
    p.add_argument("--max-usd", type=float, default=DEFAULT_MAX_USD)
    p.add_argument("--log-dir", type=Path, help="default: logs/<stage> (dryrun: timestamped)")
    p.add_argument("--epochs", type=int, help="dryrun only")
    p.add_argument("--display", help="Inspect display: full, rich, plain, log, none")
    return p.parse_args(argv)


def load_cost_per_run(path: Path) -> dict[str, float]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {key: float(v["mean_usd_total"]) for key, v in data["by_model"].items()}


def project_cost(plan: config.StagePlan, cost_per_run: dict[str, float]) -> dict[str, float]:
    """Projected dollars per model; raises KeyError if a model has no measured cost."""
    missing = [k for k in plan.model_keys if k not in cost_per_run]
    if missing:
        raise KeyError(f"no measured cost per run for {missing}")
    return {k: plan.runs_per_model * cost_per_run[k] for k in plan.model_keys}


def eval_set_kwargs(
    log_dir: Path, epochs: int, metadata: dict[str, Any], display: str | None = None
) -> dict[str, Any]:
    """Eval-level arguments. Deliberately no reasoning_* and no max_connections here: those
    would override every model's own config. Per-sample limits come from the vendored task
    (message_limit=30)."""
    kwargs: dict[str, Any] = {
        "log_dir": str(log_dir),
        "epochs": epochs,
        "max_tasks": MAX_TASKS,
        "retry_on_error": RETRY_ON_ERROR,
        "max_tool_output": MAX_TOOL_OUTPUT,
        "metadata": metadata,
    }
    if display:
        kwargs["display"] = display
    return kwargs


def continuing_models(path: Path = STOPPING_RULE_PATH) -> list[str]:
    if not path.exists():
        raise SystemExit(f"{path} not found; analyze Stage 1 first.")
    rule = json.loads(path.read_text(encoding="utf-8"))
    if not rule.get("review_complete"):
        raise SystemExit(f"{path}: the blinded review is incomplete; Stage 2 refuses to start.")
    return [k for k, v in rule["models"].items() if v["continue"]]


def check_log_dir(log_dir: Path, metadata: dict[str, Any]) -> None:
    """Refuse to reuse a stage log directory written by different code or settings."""
    if not log_dir.exists():
        return
    from inspect_ai.log import read_eval_log

    for log_path in sorted(log_dir.rglob("*.eval")):
        header = read_eval_log(str(log_path), header_only=True)
        existing = header.eval.metadata or {}
        for field in ("code_commit", "gemini_via", "reasoning_summary", "stage"):
            if existing.get(field) != metadata.get(field):
                raise SystemExit(
                    f"{log_dir} holds logs with {field}={existing.get(field)!r} (now "
                    f"{metadata.get(field)!r}). Use a new --log-dir; never reuse a stage's "
                    "log directory after a code or settings change."
                )


def missing_key_vars(model_names: list[str]) -> list[str]:
    needed = {
        config.PROVIDER_KEY_VARS[name.split("/", 1)[0]]
        for name in model_names
        if name.split("/", 1)[0] in config.PROVIDER_KEY_VARS
    }
    return sorted(v for v in needed if not os.environ.get(v))


def print_plan(plan: config.StagePlan, names: dict[str, str]) -> None:
    print(f"Stage: {plan.stage}  (epochs={plan.epochs}, tasks={len(plan.params)})")
    for key in plan.model_keys:
        print(f"  {key:7s} {names[key]:38s} {plan.runs_per_model:4d} runs")
    print(f"  total   {plan.total_runs} runs")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    reasoning_summary = not args.no_reasoning_summary

    keys = args.models
    if args.stage == "stage2":
        allowed = continuing_models()
        stopped = [k for k in (keys or []) if k not in allowed]
        if stopped:
            raise SystemExit(f"Stopping rule stopped {stopped}; Stage 2 continues only {allowed}.")
        keys = keys or allowed
        if not keys:
            print("Stopping rule: no model continues to Stage 2.")
            return 0

    plan = config.stage_plan(args.stage, keys, args.epochs)
    specs = config.model_specs(args.gemini_via, reasoning_summary)
    mock = not plan.paid
    names = {k: (f"mockllm/{k}" if mock else specs[k].name) for k in plan.model_keys}
    print_plan(plan, names)
    if args.gemini_via != "google" or not reasoning_summary:
        print(
            f"Deviation flags: gemini_via={args.gemini_via}, reasoning_summary={reasoning_summary}"
        )

    projection: dict[str, float] | None = None
    if args.cost_per_run_json:
        projection = project_cost(plan, load_cost_per_run(args.cost_per_run_json))
        for key, usd in projection.items():
            print(f"  projected {key:7s} ${usd:8.2f}")
        total = sum(projection.values())
        print(f"  projected total   ${total:8.2f}  (limit --max-usd ${args.max_usd:.2f})")
        if not math.isfinite(total) or total > args.max_usd:
            print("Refusing to start: projected cost exceeds --max-usd.")
            return 2
    elif args.stage in STAGES_NEEDING_COST:
        print(
            "Refusing to start: pass --cost-per-run-json results/smoke/cost_per_run.json "
            "(measured on the smoke stage) for a cost projection."
        )
        return 2
    elif plan.paid:
        print("No measured cost per run yet (expected for the smoke stage).")

    if plan.paid and not args.i_approve_spend:
        print("Plan only. Paid API calls need --i-approve-spend.")
        return 2

    state = config.code_state()
    if plan.paid:
        if not state["prereg_tag_commit"]:
            print(
                f"Refusing: tag {config.PREREG_TAG} not found. Commit and tag the "
                "pre-registration before any paid call."
            )
            return 2
        if state["code_dirty"]:
            print(
                "Refusing: tracked code (highagency/, experiment/, pyproject.toml, uv.lock) "
                "has uncommitted changes."
            )
            return 2
        if state["prereg_tag_commit"] != state["code_commit"]:
            print(
                f"Note: HEAD differs from {config.PREREG_TAG} ({state['describe']}); "
                "record any code change as a deviation."
            )

    metadata: dict[str, Any] = {
        "experiment": "report_incident_tool",
        "stage": plan.stage,
        "gemini_via": args.gemini_via,
        "reasoning_summary": reasoning_summary,
        "grader_model": plan.params[0].grader_model,
        "models": names,
        "epochs": plan.epochs,
        "projected_usd": projection,
        **state,
    }

    if plan.paid:
        from dotenv import find_dotenv, load_dotenv

        load_dotenv(find_dotenv(usecwd=True))
        missing = missing_key_vars([*names.values(), config.GRADER_MODEL])
        if missing:
            print(f"Refusing: missing environment variables {missing} (set them in .env).")
            return 2

    if args.log_dir:
        log_dir = args.log_dir
    elif plan.stage == "dryrun":
        log_dir = Path("logs") / "dryrun" / datetime.now().strftime("%Y%m%d-%H%M%S")
    else:
        log_dir = Path("logs") / plan.stage
    if plan.paid:
        check_log_dir(log_dir, metadata)
    print(f"Log dir: {log_dir}")

    from inspect_ai import eval_set

    from highagency.whistlebench.tasks import high_agency

    models = config.build_models(plan.model_keys, args.gemini_via, reasoning_summary, mock=mock)
    tasks = [high_agency(params=p) for p in plan.params]
    success, logs = eval_set(
        tasks=tasks, model=models, **eval_set_kwargs(log_dir, plan.epochs, metadata, args.display)
    )
    statuses: dict[str, int] = {}
    for log in logs:
        statuses[log.status] = statuses.get(log.status, 0) + 1
    print(f"eval_set success={success}; logs by status: {statuses}; log dir: {log_dir}")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
