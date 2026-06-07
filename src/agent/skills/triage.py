from __future__ import annotations

import asyncio
import json
import re

from agent.core import context
from agent.core import llm
from agent.core.models import Category, Email, Priority, TriageResult
from agent.memory.retrieval import remember, retrieve_context
from agent.skills.base import BaseSkill

def _parse_json(content: str) -> dict:
    """Parse JSON from model output, stripping markdown fences if present."""
    text = content.strip()
    # Strip ```json ... ``` or ``` ... ``` wrappers
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```\s*$', '', text).strip()
    return json.loads(text)


SYSTEM = """You are an expert email triage assistant.

Given an email and any relevant context from past interactions, respond with
ONLY valid JSON matching this exact shape:
{
  "priority": "high" | "medium" | "low",
  "category": "action" | "meeting" | "info" | "spam",
  "suggested_action": "<reply | archive | delegate | defer>",
  "draft_reply": "<short reply string, or null>"
}

Rules:
- high   = needs response today (outages, security alerts, critical clients)
- medium = needs response this week (meetings, partnerships, project updates)
- low    = FYI / no response needed (newsletters, order confirmations)
- spam   = unsolicited commercial or phishing — suggested_action must be "archive"
- Return only JSON. No markdown fences. No explanation."""


class EmailTriageSkill(BaseSkill):
    @property
    def name(self) -> str:
        return "email-triage"

    @property
    def description(self) -> str:
        return "Classify, prioritise, and suggest action for a single email."

    def execute(self, input_data: dict) -> dict:
        email = Email(**input_data)

        # Pull any prior memories about this sender for context.
        memories = retrieve_context(email.sender, limit=3)

        system_prompt = context.build_system_prompt(SYSTEM, memories)
        user_text = (
            f"From: {email.sender}\n"
            f"Subject: {email.subject}\n\n"
            f"{email.body}"
        )

        llm_response = asyncio.run(
            llm.chat(
                messages=[context.user_message(user_text)],
                system=system_prompt,
                json_mode=True,
            )
        )

        parsed = _parse_json(llm_response.content)
        result = TriageResult(
            email_id=email.id,
            priority=Priority(parsed["priority"]),
            category=Category(parsed["category"]),
            suggested_action=parsed["suggested_action"],
            draft_reply=parsed.get("draft_reply"),
        )

        # Persist what we learned about this sender.
        remember(
            content=(
                f"Email from {email.sender} — '{email.subject}' — "
                f"triaged as {result.priority.value}/{result.category.value}, "
                f"action: {result.suggested_action}"
            ),
            source="triage",
            metadata={
                "email_id": email.id,
                "sender": email.sender,
                "priority": result.priority.value,
                "category": result.category.value,
            },
        )

        return result.model_dump()


# Keep old name importable from cli.py and any existing code.
TriageSkill = EmailTriageSkill
