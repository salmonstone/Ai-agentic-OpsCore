"""POST /webhook/github: signature verification must fail closed.

Previously, a missing GITHUB_WEBHOOK_SECRET made `_verify_signature` accept
any unsigned request ("dev mode"). That's inconsistent with the Jenkins hook,
which already refuses everything when its secret isn't set, and it means
anyone who knows the URL could POST a fake push event with no secret
configured. No network, no real mapping file — the loader is faked.
"""
import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient

from agent.integrations import webhook

SECRET = "github-hook-secret-456"
PUSH = {"repository": {"full_name": "org/repo"}, "ref": "refs/heads/main",
        "after": "deadbeef", "head_commit": {"message": "fix: thing", "author": {"name": "dev"}}}


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    # No real mapping file / kubectl call — pretend nothing maps to this push.
    fake_loader = type("L", (), {"find_mapping": staticmethod(lambda repo, branch: None)})()
    monkeypatch.setattr(webhook, "get_loader", lambda: fake_loader)


client = TestClient(webhook.app)


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post(body: dict, sig: str | None):
    import json
    raw = json.dumps(body).encode()
    headers = {"content-type": "application/json", "x-github-event": "push"}
    if sig is not None:
        headers["x-hub-signature-256"] = sig
    return client.post("/webhook/github", content=raw, headers=headers)


def test_valid_signature_is_accepted():
    body = __import__("json").dumps(PUSH).encode()
    r = client.post("/webhook/github", content=body,
                    headers={"content-type": "application/json", "x-github-event": "push",
                             "x-hub-signature-256": _sign(body)})
    assert r.status_code == 200


def test_missing_signature_is_rejected():
    assert _post(PUSH, sig=None).status_code == 401


def test_wrong_signature_is_rejected():
    assert _post(PUSH, sig="sha256=" + "0" * 64).status_code == 401


def test_signature_with_wrong_secret_is_rejected():
    body = __import__("json").dumps(PUSH).encode()
    r = client.post("/webhook/github", content=body,
                    headers={"content-type": "application/json", "x-github-event": "push",
                             "x-hub-signature-256": _sign(body, secret="not-the-secret")})
    assert r.status_code == 401


def test_no_secret_configured_rejects_everything(monkeypatch):
    """The fail-closed fix: previously this accepted any unsigned request."""
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET")
    from agent.config import settings
    monkeypatch.setattr(settings, "github_webhook_secret", "")

    body = __import__("json").dumps(PUSH).encode()
    assert _post(PUSH, sig=None).status_code == 401
    r = client.post("/webhook/github", content=body,
                    headers={"content-type": "application/json", "x-github-event": "push",
                             "x-hub-signature-256": _sign(body)})
    assert r.status_code == 401   # even a well-formed signature can't be checked without a secret


def test_non_push_events_are_ignored_after_auth():
    body = __import__("json").dumps(PUSH).encode()
    r = client.post("/webhook/github", content=body,
                    headers={"content-type": "application/json", "x-github-event": "ping",
                             "x-hub-signature-256": _sign(body)})
    assert r.status_code == 200 and r.text == "ignored"
