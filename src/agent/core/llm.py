"""
The ONLY place in the codebase that talks to Claude.

Every call is:
  - timed           (latency_ms in LLMResponse)
  - cost-tracked    (log_usage from observability/costs.py)
  - structured-logged (get_logger from observability/logging.py)
  - retried on rate-limit (exponential back-off: 1s → 2s → 4s, up to 3 retries)
"""
from __future__ import annotations

import asyncio
import time

import anthropic

from agent.config import settings
from agent.core.models import LLMResponse
from agent.observability.costs import calculate_cost, log_usage
from agent.observability.logging import get_logger

log = get_logger(__name__)

# Module-level singleton — created once, reused for the process lifetime.
_client: anthropic.AsyncAnthropic | None = None

_MAX_RETRIES = 3
_BACKOFF_SECONDS = [1, 2, 4]          # wait before attempt 1, 2, 3
_JSON_SYSTEM_SUFFIX = (
    "\n\nIMPORTANT: Your response must be valid JSON only. "
    "No markdown fences, no explanation — raw JSON."
)


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


async def chat(
    messages: list[dict],
    system: str = "",
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int = 1024,
    json_mode: bool = False,
) -> LLMResponse:
    """
    Send *messages* to Claude and return a fully-populated LLMResponse.

    Args:
        messages:    List of {"role": "user"|"assistant", "content": str} dicts.
        system:      System prompt text.
        model:       Override the default model from config.
        temperature: Override the default temperature from config.
        max_tokens:  Maximum tokens in the completion.
        json_mode:   When True, appends a strict JSON instruction to the system
                     prompt so Claude returns only raw JSON.
    """
    resolved_model = model or settings.llm_model
    resolved_temp  = temperature if temperature is not None else settings.llm_temperature
    resolved_system = (system + _JSON_SYSTEM_SUFFIX) if json_mode else system

    client = _get_client()
    last_exc: Exception | None = None

    for attempt in range(_MAX_RETRIES + 1):          # attempts 0, 1, 2, 3
        try:
            log.debug(
                "llm.request",
                model=resolved_model,
                attempt=attempt,
                messages=len(messages),
                json_mode=json_mode,
            )

            t_start = time.perf_counter()
            response = await client.messages.create(
                model=resolved_model,
                max_tokens=max_tokens,
                system=resolved_system,
                messages=messages,
                temperature=resolved_temp,
            )
            latency_ms = (time.perf_counter() - t_start) * 1000

            content     = response.content[0].text
            in_tokens   = response.usage.input_tokens
            out_tokens  = response.usage.output_tokens
            cost        = calculate_cost(resolved_model, in_tokens, out_tokens)

            log_usage(
                model=resolved_model,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                latency_ms=latency_ms,
            )

            log.info(
                "llm.response",
                model=resolved_model,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                latency_ms=round(latency_ms, 1),
                cost_usd=round(cost, 6),
            )

            return LLMResponse(
                content=content,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                latency_ms=round(latency_ms, 1),
                cost=cost,
            )

        except anthropic.RateLimitError as exc:
            last_exc = exc
            if attempt == _MAX_RETRIES:
                log.error(
                    "llm.rate_limit.exhausted",
                    model=resolved_model,
                    attempts=attempt + 1,
                )
                raise

            wait = _BACKOFF_SECONDS[attempt]
            log.warning(
                "llm.rate_limit.retry",
                model=resolved_model,
                attempt=attempt,
                wait_seconds=wait,
            )
            await asyncio.sleep(wait)

        except anthropic.APIError as exc:
            log.error(
                "llm.api_error",
                model=resolved_model,
                error_type=type(exc).__name__,
                status_code=getattr(exc, "status_code", None),
                message=str(exc),
            )
            raise


async def chat_expensive(
    messages: list[dict],
    system: str = "",
    max_tokens: int = 2048,
    json_mode: bool = False,
) -> LLMResponse:
    """Convenience wrapper that forces the expensive (Opus) model."""
    return await chat(
        messages=messages,
        system=system,
        model=settings.llm_expensive_model,
        max_tokens=max_tokens,
        json_mode=json_mode,
    )
