"""List prices and per-call cost from Inspect's ModelUsage.

Inspect's `input_tokens` excludes cached tokens for all providers used here (the true input
is input_tokens + input_tokens_cache_read + input_tokens_cache_write), and `output_tokens`
includes reasoning/thinking tokens (Gemini thoughts are folded in by the provider).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from inspect_ai.model import ModelUsage

PRICES_AS_OF = "2026-10-03"


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input: float
    cache_read: float
    cache_write: float
    output: float


# Longest match first. Gemini 2.5 Pro prices are the <=200k-token prompt tier (all prompts here
# are far below it). OpenAI and Gemini do not bill cache writes separately, so a reported
# write is priced as ordinary input. OpenRouter passes through the provider's list price.
PRICES_PER_MTOK: tuple[tuple[str, Price], ...] = (
    ("gpt-5-mini", Price(input=0.25, cache_read=0.025, cache_write=0.25, output=2.0)),
    ("gpt-5", Price(input=1.25, cache_read=0.125, cache_write=1.25, output=10.0)),
    ("gemini-2.5-pro", Price(input=1.25, cache_read=0.125, cache_write=1.25, output=10.0)),
    ("claude-sonnet-5-5", Price(input=2.0, cache_read=0.20, cache_write=2.50, output=10.0)),
    # LLM judge (Amendment 1). Cache write is the standard 1.25x of input (5-minute TTL).
    ("claude-opus-5-5", Price(input=4.0, cache_read=0.20, cache_write=5.0, output=20.0)),
)

FREE = Price(input=0.0, cache_read=0.0, cache_write=0.0, output=0.0)


def price_for(model_name: str) -> Price | None:
    name = model_name.lower()
    if name.startswith("mockllm/"):
        return FREE
    for pattern, price in PRICES_PER_MTOK:
        if pattern in name:
            return price
    return None


def usage_cost(model_name: str, usage: ModelUsage) -> float:
    """Dollar cost of `usage`; NaN for a model without a known price."""
    price = price_for(model_name)
    if price is None:
        return math.nan
    return (
        usage.input_tokens * price.input
        + (usage.input_tokens_cache_read or 0) * price.cache_read
        + (usage.input_tokens_cache_write or 0) * price.cache_write
        + usage.output_tokens * price.output
    ) / 1_000_000
