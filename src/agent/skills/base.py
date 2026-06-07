from __future__ import annotations

import time
from abc import ABC, abstractmethod

from agent.observability.logging import get_logger

log = get_logger(__name__)

_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 2.0  # seconds — doubles each attempt (2s, 4s, 8s)


class SkillError(Exception):
    """Raised when a skill exhausts all retries."""

    def __init__(self, skill: str, cause: Exception):
        super().__init__(f"Skill '{skill}' failed after all retries: {cause}")
        self.skill = skill
        self.cause = cause


class BaseSkill(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def description(self) -> str: ...

    @abstractmethod
    def execute(self, input_data: dict) -> dict: ...

    # ------------------------------------------------------------------
    # Public entry-points
    # ------------------------------------------------------------------

    def run(self, input_data: dict, *, retries: int = 0) -> dict:
        """
        Execute the skill with optional exponential-backoff retries.

        Args:
            input_data: Skill-specific payload.
            retries:    How many times to retry on failure (0 = no retry).

        Returns:
            Whatever ``execute()`` returns.

        Raises:
            SkillError: When the skill fails all attempts.
        """
        log.info("skill.start", skill=self.name, max_attempts=retries + 1)
        t = time.perf_counter()
        last_exc: Exception | None = None

        for attempt in range(max(1, retries + 1)):
            try:
                result = self.execute(input_data)
                elapsed_ms = round((time.perf_counter() - t) * 1000, 1)
                log.info(
                    "skill.end",
                    skill=self.name,
                    elapsed_ms=elapsed_ms,
                    attempt=attempt,
                )
                return result
            except Exception as exc:
                last_exc = exc
                elapsed_ms = round((time.perf_counter() - t) * 1000, 1)
                log.warning(
                    "skill.attempt_failed",
                    skill=self.name,
                    attempt=attempt,
                    error=str(exc)[:300],
                    elapsed_ms=elapsed_ms,
                )
                if attempt < retries:
                    delay = _RETRY_BASE_DELAY * (2 ** attempt)
                    log.info(
                        "skill.retry",
                        skill=self.name,
                        next_attempt=attempt + 1,
                        delay_s=delay,
                    )
                    time.sleep(delay)

        log.error(
            "skill.exhausted",
            skill=self.name,
            elapsed_ms=round((time.perf_counter() - t) * 1000, 1),
        )
        raise SkillError(self.name, last_exc)  # type: ignore[arg-type]

    def run_with_retry(self, input_data: dict) -> dict:
        """Convenience wrapper: run with the default MAX_RETRIES."""
        return self.run(input_data, retries=_MAX_RETRIES)
