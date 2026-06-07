"""
Unit tests for EmailTriageSkill.

All external calls are mocked — tests run offline with no API spend.
Patches applied per test via fixtures:
  - agent.skills.triage.llm.chat      → AsyncMock (LLM call)
  - agent.skills.triage.retrieve_context → Mock returning []
  - agent.skills.triage.remember         → Mock (no-op)
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.core.models import LLMResponse, Priority
from agent.skills.triage import EmailTriageSkill


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(payload: dict) -> LLMResponse:
    return LLMResponse(
        content=json.dumps(payload),
        input_tokens=100,
        output_tokens=50,
        latency_ms=42.0,
        cost=0.00001,
    )


URGENT_PAYLOAD = {
    "priority": "high",
    "category": "action",
    "suggested_action": "reply",
    "draft_reply": "We are looking into this immediately.",
}

LOW_PAYLOAD = {
    "priority": "low",
    "category": "info",
    "suggested_action": "archive",
    "draft_reply": None,
}

HIGH_EMAIL = {
    "id": "t-001",
    "sender": "ceo@bigclient.com",
    "subject": "URGENT: Everything is broken",
    "body": "Production is completely down. All users affected. Fix immediately.",
}

LOW_EMAIL = {
    "id": "t-002",
    "sender": "news@digest.com",
    "subject": "Weekly roundup",
    "body": "Here are this week's top stories. Enjoy your reading!",
}


# ---------------------------------------------------------------------------
# Fixtures — patch LLM + memory calls so tests are fully offline
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_llm_high():
    with patch("agent.skills.triage.llm.chat", new_callable=AsyncMock) as m_llm, \
         patch("agent.skills.triage.retrieve_context", return_value=[]), \
         patch("agent.skills.triage.remember", return_value=MagicMock()):
        m_llm.return_value = _make_response(URGENT_PAYLOAD)
        yield m_llm


@pytest.fixture
def mock_llm_low():
    with patch("agent.skills.triage.llm.chat", new_callable=AsyncMock) as m_llm, \
         patch("agent.skills.triage.retrieve_context", return_value=[]), \
         patch("agent.skills.triage.remember", return_value=MagicMock()):
        m_llm.return_value = _make_response(LOW_PAYLOAD)
        yield m_llm


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("mock_llm_high")
def test_high_priority_email():
    result = EmailTriageSkill().run(HIGH_EMAIL)
    assert result["priority"] == Priority.high


@pytest.mark.usefixtures("mock_llm_low")
def test_low_priority_newsletter():
    result = EmailTriageSkill().run(LOW_EMAIL)
    assert result["priority"] == Priority.low


@pytest.mark.usefixtures("mock_llm_high")
def test_required_fields_present():
    result = EmailTriageSkill().run(HIGH_EMAIL)
    for f in ["priority", "category", "suggested_action"]:
        assert f in result, f"Missing field: {f}"


@pytest.mark.usefixtures("mock_llm_high")
def test_draft_reply_present_for_high():
    result = EmailTriageSkill().run(HIGH_EMAIL)
    assert result["draft_reply"] is not None


@pytest.mark.usefixtures("mock_llm_low")
def test_draft_reply_none_for_newsletter():
    result = EmailTriageSkill().run(LOW_EMAIL)
    assert result["draft_reply"] is None


def test_retrieve_context_called_with_sender():
    """Skill must query memory using the sender's address."""
    with patch("agent.skills.triage.llm.chat", new_callable=AsyncMock) as m_llm, \
         patch("agent.skills.triage.retrieve_context", return_value=[]) as m_rc, \
         patch("agent.skills.triage.remember", return_value=MagicMock()):
        m_llm.return_value = _make_response(URGENT_PAYLOAD)
        EmailTriageSkill().run(HIGH_EMAIL)
        m_rc.assert_called_once_with(HIGH_EMAIL["sender"], limit=3)


def test_remember_called_after_triage():
    """Skill must persist a memory record for each triaged email."""
    with patch("agent.skills.triage.llm.chat", new_callable=AsyncMock) as m_llm, \
         patch("agent.skills.triage.retrieve_context", return_value=[]), \
         patch("agent.skills.triage.remember", return_value=MagicMock()) as m_rem:
        m_llm.return_value = _make_response(URGENT_PAYLOAD)
        EmailTriageSkill().run(HIGH_EMAIL)
        m_rem.assert_called_once()
