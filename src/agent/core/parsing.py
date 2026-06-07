"""
agent/core/parsing.py

Centralised JSON parser for LLM responses.

Every skill currently has its own copy of _parse_json() — identical code,
no error context. This module replaces all of them with one function that:
  - strips markdown fences (```json ... ```)
  - raises LLMParseError with the raw output on failure
  - is importable from anywhere

Usage:
    from agent.core.parsing import parse_llm_json, LLMParseError

    try:
        data = parse_llm_json(llm_response.content)
    except LLMParseError as exc:
        log.error("llm_parse_failed", raw=exc.raw[:200], cause=str(exc.cause))
        # handle gracefully instead of propagating a cryptic JSONDecodeError
"""
from __future__ import annotations

import json
import re


class LLMParseError(ValueError):
    """Raised when Claude's response is not valid JSON."""

    def __init__(self, raw: str, cause: Exception):
        snippet = raw[:400].replace("\n", "\\n")
        super().__init__(
            f"LLM returned invalid JSON: {cause}\n"
            f"Raw output (first 400 chars): {snippet}"
        )
        self.raw   = raw
        self.cause = cause


def parse_llm_json(content: str) -> dict:
    """
    Parse JSON from an LLM response, stripping markdown fences if present.

    Raises:
        LLMParseError: with full raw content attached for logging/debugging.
    """
    text = content.strip()
    # Strip ```json ... ``` or ``` ... ``` wrappers
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$",      "", text).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMParseError(text, exc) from exc
