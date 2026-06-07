"""
Gmail integration skill.

Fetches unread emails from a Gmail inbox using the Gmail API (OAuth 2.0)
and converts them into the agent's Email model so they can be passed
directly to EmailTriageSkill.

Setup (one-time):
    1. Go to console.cloud.google.com
    2. Create a project → Enable Gmail API
    3. Credentials → Create OAuth 2.0 Client ID (Desktop app)
    4. Download JSON → save as  data/gmail_credentials.json
    5. Run:  uv run agent gmail auth
    6. Browser opens → sign in → token saved to data/gmail_token.json
    7. Run:  uv run agent gmail triage
"""
from __future__ import annotations

import base64
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

from agent.core.models import Email
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill

log = get_logger(__name__)

SCOPES             = ["https://www.googleapis.com/auth/gmail.modify"]
CREDENTIALS_FILE   = Path("data/gmail_credentials.json")
TOKEN_FILE         = Path("data/gmail_token.json")


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def get_gmail_service():
    """
    Return an authenticated Gmail API service object.

    On the first call it opens a browser for OAuth consent and saves the
    token to TOKEN_FILE.  On subsequent calls it reuses (or refreshes) the
    saved token without any user interaction.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds: Credentials | None = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            log.info("gmail.auth.refreshing_token")
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"\n\nGmail credentials file not found: {CREDENTIALS_FILE}\n\n"
                    "To set up Gmail integration:\n"
                    "  1. Go to console.cloud.google.com\n"
                    "  2. Create a project and enable the Gmail API\n"
                    "  3. Credentials → Create OAuth 2.0 Client ID (Desktop app)\n"
                    "  4. Download JSON → save as  data/gmail_credentials.json\n"
                    "  5. Run:  uv run agent gmail auth\n"
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0)
            log.info("gmail.auth.completed")

        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
        log.info("gmail.auth.token_saved", path=str(TOKEN_FILE))

    return build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------------------
# MIME parsing helpers
# ---------------------------------------------------------------------------

def _extract_body(payload: dict) -> str:
    """
    Recursively extract the plain-text body from a Gmail message payload.
    Falls back to HTML part if no plain-text part is found.
    """
    mime = payload.get("mimeType", "")

    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace").strip()

    if mime.startswith("multipart"):
        plain = ""
        html  = ""
        for part in payload.get("parts", []):
            part_mime = part.get("mimeType", "")
            if part_mime == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    plain = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace").strip()
            elif part_mime == "text/html":
                data = part.get("body", {}).get("data", "")
                if data:
                    html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace").strip()
            elif part_mime.startswith("multipart"):
                nested = _extract_body(part)
                if nested:
                    plain = nested
        return plain or html

    return ""


def _parse_sender(raw: str) -> str:
    """'Display Name <email@example.com>' → 'email@example.com'"""
    if "<" in raw:
        return raw.split("<")[1].rstrip(">").strip()
    return raw.strip()


def _parse_date(raw: str) -> datetime:
    try:
        return parsedate_to_datetime(raw).replace(tzinfo=None)
    except Exception:
        return datetime.utcnow()


def _headers_dict(headers: list[dict]) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in headers}


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------

class GmailSkill(BaseSkill):
    """
    Fetch unread emails from Gmail and return them as Email model dicts
    ready for EmailTriageSkill.
    """

    @property
    def name(self) -> str:
        return "gmail-fetch"

    @property
    def description(self) -> str:
        return "Fetch unread emails from Gmail inbox via the Gmail API."

    def execute(self, input_data: dict) -> dict:
        """
        Args (via input_data):
            limit     int   Max emails to fetch (default 10)
            query     str   Gmail search query (default 'is:unread in:inbox')
            mark_read bool  Mark fetched emails as read (default False)

        Returns:
            { "emails": [Email.model_dump(), ...], "count": int }
        """
        limit     = input_data.get("limit", 10)
        query     = input_data.get("query", "is:unread in:inbox")
        mark_read = input_data.get("mark_read", False)

        service = get_gmail_service()

        # 1 — list matching message IDs
        resp = service.users().messages().list(
            userId="me",
            q=query,
            maxResults=limit,
        ).execute()

        messages = resp.get("messages", [])
        log.info("gmail.fetch.listed", count=len(messages), query=query)

        emails: list[dict] = []

        for msg_stub in messages:
            msg_id = msg_stub["id"]

            # 2 — fetch full message
            full = service.users().messages().get(
                userId="me",
                id=msg_id,
                format="full",
            ).execute()

            hdrs    = _headers_dict(full.get("payload", {}).get("headers", []))
            body    = _extract_body(full.get("payload", {}))
            sender  = _parse_sender(hdrs.get("from", "unknown@unknown.com"))
            subject = hdrs.get("subject", "(no subject)")
            ts      = _parse_date(hdrs.get("date", ""))

            email = Email(
                id=msg_id,
                sender=sender,
                subject=subject,
                body=body or "(no body)",
                timestamp=ts,
            )
            emails.append(email.model_dump())
            log.debug("gmail.fetch.parsed", id=msg_id[:12], sender=sender)

            # 3 — optionally mark as read
            if mark_read:
                service.users().messages().modify(
                    userId="me",
                    id=msg_id,
                    body={"removeLabelIds": ["UNREAD"]},
                ).execute()
                log.debug("gmail.marked_read", id=msg_id[:12])

        log.info("gmail.fetch.done", fetched=len(emails), mark_read=mark_read)
        return {"emails": emails, "count": len(emails)}
