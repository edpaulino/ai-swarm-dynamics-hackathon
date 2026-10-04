"""One minimal call per model with its real config, plus the grader (CP1; costs cents).

    uv run python -m experiment.ping
    uv run python -m experiment.ping --models gemini --gemini-via openrouter

Prints status, the model name the API says it served, tokens and cost. Never prints keys.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from inspect_ai.model import GenerateConfig, get_model

from experiment import config, cost

PROMPT = "Reply with the single word OK."
PING_CONFIG = GenerateConfig(max_retries=1, timeout=180)

HINTS = {
    "gpt5": "If this is a 400 about reasoning summaries (unverified organization), re-run with "
    "--no-reasoning-summary and record the deviation.",
    "gemini": "If Google refuses access to gemini-2.5-pro, re-run with --gemini-via openrouter "
    "(needs OPENROUTER_API_KEY) and record the deviation.",
}


async def ping(label: str, name: str, model_config: GenerateConfig, hint: str | None) -> bool:
    provider = name.split("/", 1)[0]
    key_var = config.PROVIDER_KEY_VARS.get(provider)
    if key_var and not os.environ.get(key_var):
        print(f"[FAIL] {label:7s} {name}: {key_var} is not set")
        return False
    try:
        model = get_model(name, config=model_config)
        output = await model.generate(PROMPT, config=PING_CONFIG)
    except Exception as ex:  # report and continue with the next model
        print(f"[FAIL] {label:7s} {name}: {type(ex).__name__}: {str(ex)[:600]}")
        if hint:
            print(f"       hint: {hint}")
        return False
    if output.error:
        print(f"[FAIL] {label:7s} {name}: {output.error[:600]}")
        return False
    usage = output.usage
    tokens = f"in={usage.input_tokens} out={usage.output_tokens}" if usage else "no usage"
    usd = cost.usage_cost(name, usage) if usage else float("nan")
    text = output.completion.strip().replace("\n", " ")[:40]
    print(f"[ OK ] {label:7s} {name} served={output.model!r} {tokens} ${usd:.5f} reply={text!r}")
    return True


async def main_async(args: argparse.Namespace) -> bool:
    specs = config.model_specs(args.gemini_via, not args.no_reasoning_summary)
    results = []
    for key in args.models or config.MODEL_KEYS:
        spec = specs[key]
        results.append(await ping(key, spec.name, spec.config, HINTS.get(key)))
    if not args.skip_grader:
        grader_config = GenerateConfig(reasoning_effort="minimal")
        results.append(await ping("grader", config.GRADER_MODEL, grader_config, None))
    return all(results)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--models", nargs="+", choices=config.MODEL_KEYS)
    p.add_argument("--gemini-via", choices=["google", "openrouter"], default="google")
    p.add_argument("--no-reasoning-summary", action="store_true")
    p.add_argument("--skip-grader", action="store_true")
    args = p.parse_args(argv)

    state = config.code_state()
    if not state["prereg_tag_commit"]:
        print(f"Refusing: tag {config.PREREG_TAG} not found; tag the pre-registration first.")
        return 2

    from dotenv import find_dotenv, load_dotenv

    load_dotenv(find_dotenv(usecwd=True))
    print(
        f"code: {state['describe']}  gemini_via={args.gemini_via}  "
        f"reasoning_summary={not args.no_reasoning_summary}"
    )
    return 0 if asyncio.run(main_async(args)) else 1


if __name__ == "__main__":
    sys.exit(main())
