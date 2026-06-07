"""
Token usage and cost tracking for every Claude API call.

All costs are in USD.  Pricing is per million tokens.
"""
from __future__ import annotations

import time
from typing import TypedDict

from agent.observability.logging import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Pricing table  (USD per million tokens, mid-2025)
# ---------------------------------------------------------------------------
_PRICING: dict[str, dict[str, float]] = {
    "claude-haiku-4-5":  {"input": 0.80,  "output": 4.00},
    "claude-opus-4-7":   {"input": 15.00, "output": 75.00},
    "claude-sonnet-4-6": {"input": 3.00,  "output": 15.00},
}

_FALLBACK_MODEL = "claude-haiku-4-5"


# ---------------------------------------------------------------------------
# Session accumulator (in-memory, reset on process restart)
# ---------------------------------------------------------------------------
class _SessionTotals(TypedDict):
    calls: int
    input_tokens: int
    output_tokens: int
    total_cost_usd: float


_session: _SessionTotals = {
    "calls": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "total_cost_usd": 0.0,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return the USD cost for one API call given token counts."""
    prices = _PRICING.get(model, _PRICING[_FALLBACK_MODEL])
    return (
        input_tokens  * prices["input"] +
        output_tokens * prices["output"]
    ) / 1_000_000


def log_usage(
    model: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: float,
) -> None:
    """
    Record one Claude API call: update session totals and emit a structured
    log line so every call is visible in the log stream.
    """
    cost = calculate_cost(model, input_tokens, output_tokens)

    _session["calls"]          += 1
    _session["input_tokens"]   += input_tokens
    _session["output_tokens"]  += output_tokens
    _session["total_cost_usd"] += cost

    log.info(
        "llm.usage",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=round(cost, 6),
        latency_ms=round(latency_ms, 1),
        session_total_usd=round(_session["total_cost_usd"], 6),
    )


def get_session_total() -> dict:
    """Return a snapshot of accumulated usage for this process lifetime."""
    return {
        "calls":          _session["calls"],
        "input_tokens":   _session["input_tokens"],
        "output_tokens":  _session["output_tokens"],
        "total_cost_usd": round(_session["total_cost_usd"], 6),
    }
