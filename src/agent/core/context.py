from __future__ import annotations
from agent.core.models import Memory


def build_system_prompt(skill_instructions: str, memories: list[Memory] | None = None) -> str:
    parts = [skill_instructions.strip()]

    if memories:
        parts.append("\n## Relevant memory from past sessions")
        for m in memories:
            source = m.source or "agent"
            parts.append(f"- [{source}] {m.content}")

    return "\n".join(parts)


def user_message(text: str) -> dict:
    return {"role": "user", "content": text}


def assistant_message(text: str) -> dict:
    return {"role": "assistant", "content": text}
