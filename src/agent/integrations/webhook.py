"""
GitHub webhook receiver — FastAPI app.

Endpoints:
  POST /webhook/github   receive push events
  GET  /webhook/health   liveness check
"""
from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Request, Response

from agent.integrations.mapping_loader import get_loader
from agent.observability.logging import get_logger

log = FastAPI_app = None  # set below after imports resolved

log = get_logger(__name__)

app = FastAPI(title="AtlasOS Webhook Receiver", docs_url=None, redoc_url=None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_secret() -> bytes:
    import os
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        from agent.config import settings
        secret = settings.github_webhook_secret
    return secret.encode()


def _verify_signature(body: bytes, sig_header: str) -> bool:
    secret = _get_secret()
    if not secret:
        # No secret configured — allow in dev mode (set GITHUB_WEBHOOK_SECRET to enforce)
        log.warning("webhook.signature.no_secret — accepting unsigned request")
        return True
    if not sig_header or not sig_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig_header)


def _extract_image_tag(
    commit_message: str,
    commit_sha: str,
    image_prefix: str,
) -> str:
    """Best-effort image tag extraction from commit message or SHA."""
    # Strategy 1: explicit "image:tag" or "deploy:tag" in message
    m = re.search(r"(?:image|deploy|tag)[=:\s]+([a-zA-Z0-9._/-]+:[a-zA-Z0-9._-]+)", commit_message)
    if m:
        candidate = m.group(1)
        if ":" in candidate:
            return candidate

    # Strategy 2: semver v1.2.3 in message
    m2 = re.search(r"\bv(\d+\.\d+\.\d+(?:-[a-zA-Z0-9.]+)?)\b", commit_message)
    if m2:
        tag = m2.group(1)
        return f"{image_prefix}:{tag}" if image_prefix else tag

    # Strategy 3: short SHA fallback
    short_sha = commit_sha[:8] if commit_sha else "latest"
    return f"{image_prefix}:{short_sha}" if image_prefix else short_sha


# ---------------------------------------------------------------------------
# Slack signature helper
# ---------------------------------------------------------------------------

def _verify_slack_signature(body: bytes, timestamp: str, sig_header: str) -> bool:
    """Verify Slack HMAC-SHA256 request signature + timestamp freshness (±5 min)."""
    import time as _time
    try:
        if abs(_time.time() - float(timestamp)) > 300:
            return False
    except (ValueError, TypeError):
        return False

    import os
    secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    if not secret:
        try:
            from agent.config import settings
            secret = settings.slack_signing_secret
        except Exception:
            pass
    if not secret:
        log.warning("slack.actions.no_signing_secret")
        return False

    base     = f"v0:{timestamp}:{body.decode('utf-8', errors='replace')}".encode()
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig_header or "")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/webhook/health")
async def health() -> dict:
    cfg = get_loader().load()
    return {
        "status":   "ok",
        "mappings": len(cfg.mappings),
        "time":     datetime.now(timezone.utc).isoformat(),
    }


@app.post("/webhook/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event:      str | None = Header(default=None),
) -> Response:
    body = await request.body()

    # Verify HMAC
    if not _verify_signature(body, x_hub_signature_256 or ""):
        log.warning(
            "webhook.signature.invalid",
            event=x_github_event,
            sig=x_hub_signature_256,
        )
        raise HTTPException(status_code=401, detail="Invalid signature")

    # Only handle push events
    if x_github_event != "push":
        return Response(content="ignored", status_code=200)

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Return 200 immediately — GitHub must not wait for deploy processing.
    # Push the work onto the durable event queue; the daemon's queue_worker
    # will pick it up, run the risk analysis, and send the Slack approval.
    try:
        repo   = payload.get("repository", {}).get("full_name", "")
        ref    = payload.get("ref", "")
        branch = ref.replace("refs/heads/", "")
        sha    = payload.get("after", "")
        head   = payload.get("head_commit") or {}
        msg    = head.get("message", "")
        author = head.get("author", {}).get("name", "unknown")
        files: list[str] = (
            head.get("added", []) + head.get("modified", []) + head.get("removed", [])
        )

        # Quick mapping check — drop events with no mapping before queuing
        loader  = get_loader()
        mapping = loader.find_mapping(repo, branch)
        if mapping is None:
            log.info("webhook.no_mapping", repo=repo, branch=branch)
            return Response(content="no_mapping", status_code=200)

        new_image = _extract_image_tag(msg, sha, mapping.image_prefix)

        from agent.integrations.event_queue import enqueue
        event_id = enqueue(
            "deploy.push",
            {
                "repo":      repo,
                "branch":    branch,
                "sha":       sha,
                "message":   msg,
                "author":    author,
                "files":     files[:20],
                "new_image": new_image,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            priority=2,   # high — deploys are time-sensitive
        )
        log.info("webhook.enqueued", repo=repo, branch=branch,
                 sha=sha[:8], event_id=event_id[:8])
    except Exception as exc:
        log.warning("webhook.enqueue_failed", error=str(exc))

    return Response(content="accepted", status_code=200)


# ---------------------------------------------------------------------------
# Jenkins build-failure hook — diagnose automatically, post to Slack, and
# offer the fix as an Approve/Reject proposal when it can be automated.
#
# Jenkins side (a Jenkinsfile `post { failure { ... } }` step, or the
# Notification plugin) POSTs here with the shared JENKINS_WEBHOOK_SECRET in
# an X-AtlasOS-Token header or a ?token= query param. Fails closed: with no
# secret configured, every request is rejected.
# ---------------------------------------------------------------------------

def _jenkins_secret() -> str:
    import os
    return os.environ.get("JENKINS_WEBHOOK_SECRET", "").strip()


def _verify_jenkins_token(presented: str) -> bool:
    secret = _jenkins_secret()
    if not secret or not presented:
        return False
    return hmac.compare_digest(presented.encode(), secret.encode())


def parse_jenkins_event(payload: dict) -> tuple[str, int, str] | None:
    """(job, build_number, status) from either payload shape we accept:

    - simple:  {"job": "api/main", "build": 42, "status": "FAILURE"}
    - Jenkins Notification plugin:
               {"name": "api", "build": {"number": 42, "phase": "COMPLETED", "status": "FAILURE"}}

    Returns None for anything else, including the plugin's STARTED phase
    (no result yet).
    """
    if not isinstance(payload, dict):
        return None
    job = payload.get("job") or payload.get("name")
    build = payload.get("build")
    status = payload.get("status")
    if isinstance(build, dict):
        if str(build.get("phase", "")).upper() == "STARTED":
            return None
        status = build.get("status", status)
        build = build.get("number")
    try:
        number = int(build)
    except (TypeError, ValueError):
        return None
    if not job or not isinstance(job, str) or not status:
        return None
    return job.strip(), number, str(status).strip().upper()


@app.post("/webhook/jenkins")
async def jenkins_webhook(
    request: Request,
    x_atlasos_token: str | None = Header(default=None),
) -> Response:
    token = x_atlasos_token or request.query_params.get("token", "")
    if not _verify_jenkins_token(token):
        log.warning("jenkins_webhook.unauthorized")
        raise HTTPException(status_code=401, detail="Invalid or missing token")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event = parse_jenkins_event(payload)
    if event is None:
        return Response(content="ignored: not a build result", status_code=200)
    job, number, status = event
    if status != "FAILURE":
        return Response(content=f"ignored: status {status}", status_code=200)

    # Each job+build is handled once. Jenkins can report the same build more
    # than once (the Notification plugin sends COMPLETED then FINALIZED), and
    # a retried POST must not produce a second Slack message. Checked and
    # marked with no `await` in between, so two near-simultaneous requests on
    # this event loop can't both pass.
    if not _claim_jenkins_build(job, number):
        return Response(content="duplicate: already handled", status_code=200)

    # Diagnosis takes a while (log fetch + AI); Jenkins must not wait on it.
    import asyncio
    asyncio.create_task(_handle_jenkins_failure(job, number))
    log.info("jenkins_webhook.accepted", job=job, build=number)
    return Response(content="accepted", status_code=202)


def _claim_jenkins_build(job: str, number: int) -> bool:
    """True the first time a job+build is seen (within 7 days), else False."""
    from agent.integrations import daemon_db

    key = f"jenkins_failure/{job}#{number}"
    try:
        if daemon_db.check_cooldown(key, cooldown_minutes=7 * 24 * 60):
            return False
        daemon_db.set_cooldown(key)
    except Exception as exc:
        log.warning("jenkins_webhook.dedupe_failed", error=str(exc))
    return True


_NON_FIX_ACTIONS = frozenset({"MANUAL_ONLY", "NO_ACTION_NEEDED"})


async def _handle_jenkins_failure(job: str, number: int) -> dict:
    """Diagnose one failed build and tell Slack. Never raises."""
    import asyncio

    from agent.integrations.slack import send_alert_generic

    try:
        from agent.mcp_server import jenkins_diagnose
        diag = await jenkins_diagnose(job, number)
    except Exception as exc:
        log.warning("jenkins_webhook.diagnose_failed", job=job, build=number, error=str(exc))
        send_alert_generic(
            title="Jenkins build failed",
            message=f"❌ `{job}` #{number} failed, but AtlasOS couldn't diagnose it: {str(exc)[:200]}\n"
                    f"Try: agent jenkins diagnose {job} --build {number}",
            severity="warning",
            fields={"Job": job, "Build": str(number)},
        )
        return {"handled": True, "diagnosed": False}

    root = (diag.get("root_cause") or "unknown").strip()
    raw_action = diag.get("fix_action", "MANUAL_ONLY")
    action = str(getattr(raw_action, "value", raw_action))   # enum -> "RETRIGGER" etc.
    try:
        from agent.memory.retrieval import remember
        remember(f"Jenkins {job} #{number} failed: {root} (suggested: {action})",
                 source="jenkins", metadata={"job": job, "build": number, "fix_action": action})
    except Exception:
        pass

    # Only an action AtlasOS can actually perform gets an Approve button.
    # MANUAL_ONLY needs a human; NO_ACTION_NEEDED (e.g. a build that was
    # meant to fail) needs nothing — offering "Approve" for it is noise.
    if action not in _NON_FIX_ACTIONS:
        from agent.core import fix_registry
        summary = f"Jenkins {job} #{number} failed: {root} — suggested fix: {action}"
        try:
            r = await asyncio.to_thread(
                fix_registry.propose, "jenkins_apply_fix",
                {"job_name": job, "build_number": number}, summary,
                "Jenkins failure hook", True,
            )
            return {"handled": True, "diagnosed": True, "proposed": True, "approval_id": r["id"]}
        except fix_registry.ProposalError as exc:
            log.warning("jenkins_webhook.propose_failed", error=str(exc))
            # fall through to a plain alert so the diagnosis isn't lost

    send_alert_generic(
        title="Jenkins build failed",
        message=(f"❌ `{job}` #{number} failed.\n*Cause:* {root}\n"
                 f"{diag.get('explanation', '')}\n"
                 + {"MANUAL_ONLY": "*Needs a manual fix.*",
                    "NO_ACTION_NEEDED": "*No action needed* — this failure is expected."}.get(
                        action, f"*Suggested fix:* {action}")
                 + (f"\n*Prevention:* {diag['prevention']}" if diag.get("prevention") else "")),
        severity="warning",
        fields={"Job": job, "Build": str(number), "Confidence": str(diag.get("confidence", ""))},
    )
    return {"handled": True, "diagnosed": True, "proposed": False}


# ---------------------------------------------------------------------------
# Slack Interactive Components — button approve / reject
# ---------------------------------------------------------------------------

@app.post("/slack/actions")
async def slack_actions(
    request: Request,
    x_slack_signature:        str | None = Header(default=None),
    x_slack_request_timestamp: str | None = Header(default=None),
) -> Response:
    """
    Receives Slack button clicks (approve/reject deploy).

    Set this URL in your Slack App → Interactivity & Shortcuts → Request URL:
      https://<your-server>/slack/actions
    """
    body = await request.body()

    if not _verify_slack_signature(body, x_slack_request_timestamp or "", x_slack_signature or ""):
        log.warning("slack.actions.invalid_signature")
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    # Slack sends URL-encoded: payload=<JSON>
    import urllib.parse
    try:
        form   = urllib.parse.parse_qs(body.decode())
        raw    = form.get("payload", ["{}"])[0]
        payload = __import__("json").loads(raw)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid payload: {exc}")

    # Acknowledge immediately — Slack requires < 3s response
    import asyncio
    asyncio.create_task(_handle_slack_action(payload))
    return Response(
        content='{"text": "Processing…"}',
        media_type="application/json",
        status_code=200,
    )


async def _handle_slack_action(payload: dict) -> None:
    """Process Slack button action in the background."""
    import asyncio

    actions      = payload.get("actions", [])
    response_url = payload.get("response_url", "")
    user         = payload.get("user", {}).get("name", "someone")

    if not actions:
        return

    action    = actions[0]
    action_id = action.get("action_id", "")
    deploy_id = action.get("value", "")

    log.info("slack.action.received", action_id=action_id, deploy_id=deploy_id, user=user)

    from agent.integrations.slack import update_deploy_message

    if action_id == "approve_deploy":
        # Immediately update Slack message to show "Deploying…"
        update_deploy_message(
            response_url, "DEPLOYING",
            deploy_id,
            f"Approved by *{user}* — deploy started, watching for up to 2 minutes…",
        )

        loop = asyncio.get_event_loop()
        try:
            from agent.skills.deployment import DeploymentSkill
            skill = DeploymentSkill()
            report = await loop.run_in_executor(
                None,
                lambda: skill.execute_approve(deploy_id),
            )
            if report is None:
                update_deploy_message(
                    response_url, "FAILED", deploy_id,
                    "Deploy was blocked by health gate or not found.",
                )
            else:
                update_deploy_message(
                    response_url, report.status, report.deployment,
                    f"{report.claude_summary}\n"
                    f"Pods: {report.pods_healthy} healthy | Duration: {report.duration_seconds}s",
                )
        except Exception as exc:
            log.warning("slack.action.approve_failed", error=str(exc))
            update_deploy_message(
                response_url, "FAILED", deploy_id,
                f"Deploy error: {str(exc)[:120]}",
            )

    elif action_id == "reject_deploy":
        loop = asyncio.get_event_loop()
        try:
            from agent.skills.deployment import DeploymentSkill
            skill = DeploymentSkill()
            ok = await loop.run_in_executor(
                None,
                lambda: skill.execute_reject(deploy_id),
            )
            update_deploy_message(
                response_url, "REJECTED", deploy_id,
                f"Rejected by *{user}*.",
            )
        except Exception as exc:
            log.warning("slack.action.reject_failed", error=str(exc))

    elif action_id in ("approve_action", "reject_action"):
        await _handle_fix_action(action_id, action.get("value", ""), user, response_url)

    else:
        log.warning("slack.action.unknown", action_id=action_id)


async def _handle_fix_action(action_id: str, action_pk: str, user: str, response_url: str) -> None:
    """Dispatch an approve/reject click on one of the generic *_apply_fix
    proposals (agent.core.approvals + fix_registry) — separate from the
    deploy-specific flow above, which this does not touch."""
    from agent.core import approvals
    from agent.integrations.slack import update_action_message
    from agent.memory.retrieval import remember

    pending = approvals.get(action_pk)
    if pending is None:
        update_action_message(response_url, "FAILED", "(unknown)", "No such pending action — it may be old.")
        return

    if pending.status != "pending":
        update_action_message(response_url, pending.status.upper(), pending.summary,
                              f"Already {pending.status} — no action taken.")
        return

    if approvals.is_expired(pending):
        approvals.decide(pending.id, "expired", user)
        update_action_message(response_url, "EXPIRED", pending.summary,
                              "This proposal expired before anyone approved it. Re-run the scan to propose it again.")
        remember(f"Approval {pending.id} ({pending.kind}) expired before a decision",
                 source="approvals", metadata={"kind": pending.kind, "id": pending.id})
        return

    if action_id == "reject_action":
        approvals.decide(pending.id, "rejected", user)
        update_action_message(response_url, "REJECTED", pending.summary, f"Rejected by *{user}*.")
        remember(f"Approval {pending.id} ({pending.kind}) rejected by {user}: {pending.summary}",
                 source="approvals", metadata={"kind": pending.kind, "id": pending.id, "user": user})
        return

    # approve_action
    approvals.decide(pending.id, "approved", user)
    update_action_message(response_url, "APPROVED", pending.summary, f"Approved by *{user}* — applying…")

    from agent.core.fix_registry import execute
    try:
        result = await execute(pending.kind, pending.params, confirm=True)
    except Exception as exc:
        log.warning("approvals.execute_failed", id=pending.id, kind=pending.kind, error=str(exc))
        result = {"applied": False, "message": f"error: {exc}"}

    final = "APPLIED" if result.get("applied") else "FAILED"
    approvals.decide(pending.id, final.lower(), user, result=result)
    update_action_message(response_url, final, pending.summary,
                          result.get("message") or f"Result: {json_dumps_short(result)}")
    remember(f"Approval {pending.id} ({pending.kind}) approved by {user} -> {final}: {pending.summary}",
             source="approvals", metadata={"kind": pending.kind, "id": pending.id, "user": user, "result": final})


def json_dumps_short(d: dict, limit: int = 300) -> str:
    import json as _json
    s = _json.dumps({k: v for k, v in d.items() if k != "diagnosis"})
    return s if len(s) <= limit else s[:limit] + "…"
