"""
Daily summary — one Slack message each morning with the state of
everything AtlasOS watches: cluster, Jenkins, GitHub Actions, AWS spend,
fixes waiting for approval, backups, and AtlasOS itself.

Facts only: no AI calls. Each section is gathered independently, so one
unreachable system (AWS down, Jenkins offline) turns that section into
"couldn't check" instead of failing the whole message. It's sent every day
even when everything is fine, so a missing message means AtlasOS itself
isn't running. At most one is sent per calendar day.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.observability.logging import get_logger

log = get_logger(__name__)

WINDOW = timedelta(hours=24)
BACKUP_STALE_AFTER = timedelta(hours=26)    # a daily backup, plus slack for when it ran
_HEALTHY_POD_PHASES = {"Running", "Succeeded", "Completed"}
_ICON = {"ok": "✅", "warn": "⚠️", "error": "❔"}


@dataclass
class Section:
    title: str
    status: str                 # ok | warn | error
    lines: list[str] = field(default_factory=list)


def _ago(delta: timedelta) -> str:
    s = int(delta.total_seconds())
    if s < 3600:
        return f"{max(1, s // 60)}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------

def _cluster(now: datetime) -> Section:
    from agent.integrations.kubectl import get_nodes_detail, get_pods

    nodes = get_nodes_detail()
    if not nodes:
        return Section("Cluster", "error", ["Couldn't reach the cluster (no nodes returned)."])
    ready = sum(1 for n in nodes if n.status == "Ready")
    pods = get_pods("all")
    bad = []
    for p in pods:
        ok_phase = p.status in _HEALTHY_POD_PHASES
        if p.status == "Running" and "/" in p.ready:
            have, want = p.ready.split("/", 1)
            ok_phase = have == want
        if not ok_phase:
            bad.append(p)
    lines = [f"{ready}/{len(nodes)} nodes ready · {len(pods) - len(bad)}/{len(pods)} pods healthy"]
    for p in bad[:5]:
        lines.append(f"`{p.namespace}/{p.name}` — {p.status} ({p.ready} ready, {p.restarts} restarts)")
    if len(bad) > 5:
        lines.append(f"…and {len(bad) - 5} more")
    return Section("Cluster", "warn" if bad or ready < len(nodes) else "ok", lines)


def _jenkins(now: datetime) -> Section:
    from agent.integrations import jenkins as jk

    jobs = [j for j in jk.get_all_jobs() if not j.is_folder]
    failed = []
    for j in jobs:
        for b in jk.get_build_history(j.name, count=10):
            if b.status != "FAILURE" or not b.timestamp:
                continue
            when = datetime.fromtimestamp(b.timestamp / 1000, timezone.utc)
            if now - when <= WINDOW:
                failed.append((j.name, b.number, when))
    if not failed:
        return Section("Jenkins", "ok", [f"No failed builds in the last 24h ({len(jobs)} jobs)."])
    failed.sort(key=lambda f: f[2], reverse=True)
    lines = [f"{len(failed)} failed build(s) in the last 24h:"]
    lines += [f"❌ `{name}` #{num} ({_ago(now - when)})" for name, num, when in failed[:5]]
    return Section("Jenkins", "warn", lines)


def _github(now: datetime) -> Section:
    from agent.integrations import github_actions as gha

    repo = gha.resolve_repo(None)
    failed = []
    for r in gha.list_runs(repo, limit=30, failed_only=True):
        try:
            when = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if now - when <= WINDOW:
            failed.append((r, when))
    if not failed:
        return Section("GitHub Actions", "ok", [f"No failed runs in the last 24h (`{repo}`)."])
    lines = [f"{len(failed)} failed run(s) in `{repo}`:"]
    lines += [f"❌ {r['workflow']} on `{r['branch']}` ({_ago(now - when)}) — run {r['id']}"
              for r, when in failed[:5]]
    return Section("GitHub Actions", "warn", lines)


def _aws(now: datetime) -> Section:
    from agent.integrations.aws_cost import get_spend_anomalies

    data = get_spend_anomalies(days=30)
    totals = data.get("daily_totals") or []
    if not totals:
        return Section("AWS spend", "error", ["No Cost Explorer data (check AWS access)."])
    latest = totals[-1]
    mean = data.get("mean_daily") or 0.0
    pct = ((latest["amount"] - mean) / mean * 100) if mean else 0.0
    spike = any(a.get("date") == latest["date"] for a in data.get("anomaly_days") or [])
    line = f"{latest['date']}: ${latest['amount']:.2f} vs ${mean:.2f}/day avg ({pct:+.0f}%)"
    if spike:
        return Section("AWS spend", "warn", [line, "Statistical spike — `agent cost analyze` for the breakdown."])
    return Section("AWS spend", "ok", [line])


def _approvals(now: datetime) -> Section:
    from agent.core import approvals

    approvals.expire_stale()
    pending = approvals.list_actions(status="pending", limit=50)
    if not pending:
        return Section("Waiting on you", "ok", ["No fixes waiting for approval."])
    lines = [f"{len(pending)} fix(es) waiting for your Approve/Reject in Slack:"]
    lines += [f"• {a.summary[:120]} (`{a.id}`)" for a in pending[:5]]
    return Section("Waiting on you", "warn", lines)


def _backups(now: datetime) -> Section:
    from agent.core.backup import list_backups

    regular = [b for b in list_backups() if not b.pre_restore]
    if not regular:
        return Section("Backups", "warn", ["No backups found. Run `agent backup create`."])
    last = regular[0]
    age = now - last.created
    line = f"Last backup {_ago(age)} ({last.size / 1024:,.0f} KB, {len(regular)} kept)"
    if age > BACKUP_STALE_AFTER:
        return Section("Backups", "warn", [line + " — older than a day."])
    return Section("Backups", "ok", [line])


def _atlasos(now: datetime) -> Section:
    import json

    from agent.core import supervisor as sv

    pid = sv.running_supervisor_pid()
    if not pid:
        return Section("AtlasOS", "warn", ["Supervisor isn't running — MCP/ngrok aren't being watched. "
                                           "Run `start-atlasos.bat`."])
    try:
        services = json.loads(Path(sv.STATE_FILE).read_text(encoding="utf-8")).get("services", {})
    except (OSError, ValueError):
        services = {}
    parts, unhealthy = [], False
    for name in ("mcp", "ngrok"):
        s = services.get(name, {})
        ok = s.get("state") == "running" and s.get("healthy")
        unhealthy = unhealthy or not ok
        parts.append(f"{name} {'up' if ok else s.get('state', 'unknown')}")
    parts.append("daemon on" if _daemon_running() else "daemon off (started by hand)")
    return Section("AtlasOS", "warn" if unhealthy else "ok", [" · ".join(parts)])


def _daemon_running() -> bool:
    try:
        import psutil
        pid = int(Path("data/daemon.pid").read_text().strip())
        return psutil.pid_exists(pid)
    except Exception:
        return False


SECTIONS = [_cluster, _jenkins, _github, _aws, _approvals, _backups, _atlasos]
_TITLES = {_cluster: "Cluster", _jenkins: "Jenkins", _github: "GitHub Actions", _aws: "AWS spend",
           _approvals: "Waiting on you", _backups: "Backups", _atlasos: "AtlasOS"}


# ---------------------------------------------------------------------------
# build, render, send
# ---------------------------------------------------------------------------

def build_summary(now: datetime | None = None) -> list[Section]:
    now = now or datetime.now(timezone.utc)
    out = []
    for fn in SECTIONS:
        try:
            out.append(fn(now))
        except Exception as e:
            log.warning("daily_summary.section_failed", section=_TITLES[fn], error=str(e))
            out.append(Section(_TITLES[fn], "error", [f"Couldn't check: {str(e)[:150]}"]))
    return out


def headline(sections: list[Section]) -> str:
    warn = [s.title for s in sections if s.status == "warn"]
    err = [s.title for s in sections if s.status == "error"]
    if not warn and not err:
        return "All clear."
    bits = []
    if warn:
        bits.append(f"Needs attention: {', '.join(warn)}")
    if err:
        bits.append(f"Couldn't check: {', '.join(err)}")
    return " · ".join(bits)


def render_text(sections: list[Section], now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    out = [f"AtlasOS daily summary — {now.astimezone():%a %d %b %Y}", headline(sections), ""]
    for s in sections:
        out.append(f"{_ICON[s.status]} {s.title}")
        out += [f"    {line}" for line in s.lines]
    return "\n".join(out)


def render_blocks(sections: list[Section], now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    blocks = [
        {"type": "header", "text": {"type": "plain_text",
                                    "text": f"☀️ AtlasOS daily summary — {now.astimezone():%a %d %b}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{headline(sections)}*"}},
        {"type": "divider"},
    ]
    for s in sections:
        body = "\n".join(s.lines)[:2900]          # Slack's section text limit is 3000
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                                                   "text": f"{_ICON[s.status]} *{s.title}*\n{body}"}})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                   "text": "Sent daily by AtlasOS · preview anytime: `agent summary show`"}]})
    return blocks


def send(now: datetime | None = None, force: bool = False) -> dict:
    """Build and post today's summary. At most once per calendar day unless force."""
    from agent.integrations import daemon_db
    from agent.integrations.slack import is_configured, send_alert_blocks

    now = now or datetime.now(timezone.utc)
    key = f"daily_summary/{now.astimezone():%Y-%m-%d}"
    if not force and daemon_db.check_cooldown(key, cooldown_minutes=24 * 60):
        return {"sent": False, "reason": "already sent today"}
    if not is_configured():
        return {"sent": False, "reason": "Slack isn't configured (SLACK_WEBHOOK_URL)"}
    sections = build_summary(now)
    if not send_alert_blocks(render_blocks(sections, now)):
        return {"sent": False, "reason": "Slack rejected the message"}
    daemon_db.set_cooldown(key)
    log.info("daily_summary.sent", headline=headline(sections))
    return {"sent": True, "headline": headline(sections)}
