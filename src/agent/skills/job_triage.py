"""
Job & Interview Email Triage Skill.

Fetches the latest inbox emails, filters for job-related ones, and returns
a structured summary of up to 5 relevant emails with category, priority,
and deadline detection.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime

from agent.core import context, llm
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill
from agent.skills.gmail import GmailSkill

log = get_logger(__name__)

# Gmail pre-filter — narrows the fetch before LLM sees anything
_GMAIL_QUERY = (
    "in:inbox ("
    "interview OR recruiter OR hiring OR assessment OR "
    '"coding test" OR "online assessment" OR offer OR internship OR '
    "shortlisted OR selection OR screening OR "
    '"technical interview" OR "HR interview" OR '
    '"job opportunity" OR "application status" OR "application update" OR '
    "OA OR round OR shortlist"
    ")"
)

SYSTEM = """You are a Job & Interview Email Triage Agent.

You will receive a list of emails in JSON format. Your task:

1. Keep ONLY emails related to:
   - Job applications / application status updates
   - Interview invitations or scheduling
   - Recruiter or hiring manager outreach
   - Assessment / coding test / online assessment invitations
   - Offer letters
   - Internship opportunities
   - Rejection emails
   - Shortlisting / selection notifications

2. DISCARD:
   - Promotions, newsletters, marketing
   - Social / GitHub notifications
   - Shopping, bank alerts
   - Any email not directly about a job opportunity

3. From the relevant emails, return the latest 5 (sorted newest first).

4. For each relevant email return a JSON object with:
   - id: email id
   - received_time: ISO timestamp string
   - sender: sender email
   - subject: subject line
   - company: company name if detectable, else null
   - category: one of "Interview" | "Assessment" | "Recruiter Outreach" | "Application Update" | "Offer" | "Rejection" | "Other"
   - priority: one of "Critical" | "High" | "Medium"
   - summary: exactly 2 sentences summarizing the email
   - action_required: if the email contains an interview date, assessment deadline, or offer deadline — a string describing the deadline in IST. Otherwise null.

Priority rules:
- Critical = interview scheduled, offer letter, assessment deadline within 24h
- High     = interview invite (no date yet), shortlisted, recruiter outreach, assessment invite
- Medium   = application update, status confirmation, rejection

5. Respond with ONLY valid JSON — no markdown, no explanation:
{
  "relevant": [
    { ...email object... },
    ...
  ],
  "total_scanned": <number of emails you received>,
  "found": <number of relevant emails returned>
}

If no job-related emails exist, return:
{ "relevant": [], "total_scanned": <n>, "found": 0 }"""


def _parse_json(content: str) -> dict:
    text = content.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```\s*$', '', text).strip()
    return json.loads(text)


class JobTriageSkill(BaseSkill):
    """
    Fetch latest inbox emails, filter for job-related ones, and return
    a structured summary of up to 5 with category, priority, and deadlines.
    """

    @property
    def name(self) -> str:
        return "job-triage"

    @property
    def description(self) -> str:
        return "Surface job/interview emails from Gmail inbox, ignore everything else."

    def execute(self, input_data: dict) -> dict:
        """
        Args (via input_data):
            fetch_limit  int  How many emails to pull from Gmail before filtering (default 30)
            result_limit int  Max job emails to return (default 5)

        Returns:
            { "relevant": [...], "total_scanned": N, "found": M }
        """
        fetch_limit  = input_data.get("fetch_limit", 30)
        result_limit = input_data.get("result_limit", 5)

        # 1 — pull emails from Gmail using keyword pre-filter
        gmail_result = GmailSkill().run({
            "limit": fetch_limit,
            "query": _GMAIL_QUERY,
            "mark_read": False,
        })
        emails = gmail_result["emails"]

        if not emails:
            log.info("job_triage.no_emails_fetched")
            return {"relevant": [], "total_scanned": 0, "found": 0}

        log.info("job_triage.fetched", count=len(emails))

        # 2 — build a compact representation for the LLM (avoid huge bodies)
        compact = []
        for e in emails:
            body_preview = e.get("body", "")[:500].strip()
            compact.append({
                "id":            e["id"],
                "received_time": e["timestamp"] if isinstance(e["timestamp"], str) else str(e["timestamp"]),
                "sender":        e["sender"],
                "subject":       e["subject"],
                "body_preview":  body_preview,
            })

        user_text = (
            f"Here are {len(compact)} inbox emails to triage.\n"
            f"Return up to {result_limit} job-related ones, newest first.\n\n"
            + json.dumps(compact, indent=2)
        )

        # 3 — LLM call
        llm_response = asyncio.run(
            llm.chat(
                messages=[context.user_message(user_text)],
                system=SYSTEM,
                json_mode=True,
            )
        )

        result = _parse_json(llm_response.content)
        result["relevant"] = result.get("relevant", [])[:result_limit]
        result["found"]    = len(result["relevant"])

        log.info("job_triage.done",
                 scanned=result.get("total_scanned", len(emails)),
                 found=result["found"])

        return result
