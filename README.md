# AI Agentic OS

An AI-powered command-line operations platform. Manage Kubernetes clusters, AWS infrastructure, Gmail, and more — all from one unified CLI with natural language understanding and persistent memory.

→ **[Architecture & Design Decisions](ARCHITECTURE.md)**

> **New:** Run **`agent about`** for full, always-current architecture documentation — it scans the live project (every file, skill, eval suite, and runtime state) and explains how it all connects. Add `--no-claude` for an instant offline overview or `--json` for a machine-readable snapshot.

## Quick Install

```bash
curl -fsSL https://raw.githubusercontent.com/YOUR_USERNAME/Ai-agentic-Os/main/install.sh | bash
```

This installs dependencies, runs `uv sync`, and launches the interactive setup wizard.

## Manual Install

```bash
# Clone the repo
git clone https://github.com/YOUR_USERNAME/Ai-agentic-Os.git
cd Ai-agentic-Os

# Install uv (if not installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install dependencies
uv sync
uv pip install -e .

# Configure integrations
agent setup
```

## Run in Docker

The image bundles Python, all dependencies, and pinned `kubectl` / `aws` / `helm`.
It contains no credentials: `~/.aws`, `~/.kube/config` and `.env` are mounted
read-only when the container starts.

```bash
docker compose up -d --build                       # MCP server on 127.0.0.1:8000
docker compose logs -f                             # follow output
docker compose run --rm atlasos agent k8s nodes    # any CLI command, one-off
docker compose down                                # stop
```

`.env` must contain `MCP_AUTH_TOKEN`; the server refuses to start without it.
`data/` and `chroma_db/` are shared with a local run, so use one or the other,
since both bind port 8000. Adding a cluster (`agent k8s add-cluster`) writes
kubeconfig, so do that on the host; the container picks it up on restart.

## Keep It Running

`agent supervise` runs ngrok and the remote MCP server, restarts either one if
it crashes or stops responding (backoff 2s up to 5 min), and hands the MCP
server ngrok's current hostname so tunnelled requests never 421.

```bash
start-atlasos.bat                  # start (also runs at sign-in and unlock via Task Scheduler)
agent supervise status             # state, health, restarts, public URL
agent supervise logs mcp           # or: ngrok, daemon
agent supervise stop               # stop everything cleanly
```

The autonomous healing daemon applies fixes on its own, so it is opt-in:
`agent supervise run --services ngrok,mcp,daemon`. In Docker, the compose
`restart: unless-stopped` policy does the same job for the container.

## Backups

All state (memory and audit trail, incidents, deploys, SLOs, vault, semantic
memory) lives in `data/` and `chroma_db/`. Back it up with:

```bash
agent backup create          # verified snapshot → backups/atlasos-<UTC time>.tar.gz
agent backup list            # newest first
agent backup verify <name>   # checksums + database integrity, restores nothing
agent backup restore <name>  # asks first; saves the current state as a pre-restore backup
```

Databases are snapshotted with SQLite's online backup API, so it's safe while
the agent is running. Secrets (`.env`, Gmail tokens) are never included.
`create` keeps the newest backup of each of the last 7 days plus 4 older
weeks. `backup.bat` has the Task Scheduler command for a daily run.
Backups are local-only for now: copy `backups/` somewhere off this disk.

## Fix Approvals via Slack

The six `*_apply_fix` tools (Jenkins, Kubernetes, ingress, TLS, AWS, cost)
normally need someone at the CLI to type `y`. `agent approvals` proposes one
to Slack instead — a real Approve/Reject message you (or anyone in that
channel) can act on from a phone:

```bash
agent approvals test                    # sends a message that touches nothing real — prove it works first
agent approvals propose cost_apply_fix --summary "EBS gp2->gp3 vol-0abc" -p fix_id=vol-0abc -p days=30
agent approvals list                    # pending / approved / rejected / applied / failed
```

Tapping Approve re-runs the exact same tool ChatGPT or the CLI would call,
with `confirm=True` — nothing is cached from when it was proposed, so it
always acts on the current state, not a stale diagnosis. A proposal expires
(45 min by default) rather than staying clickable indefinitely.

The autonomous daemon uses this too, but only for its riskiest step: a
crash-looping pod always gets an immediate, automatic rolling restart (that
part never waits — it's bounded and reversible), but if the restart alone
doesn't recover it, the AI-guessed fix that comes next (patching the
container's command, or another kubectl command) is proposed to Slack
instead of applied on its own. OOM memory bumps and CPU-based autoscaling
stay fully automatic — they're mechanical, capped adjustments, not an AI
guess acting on a live deployment.

Requires a Slack App with **Interactivity & Shortcuts** turned on, Request
URL set to `https://<your-ngrok-host>/slack/actions` — served from the same
port and tunnel as the MCP server, authenticated by Slack's own request
signature rather than the MCP token. `SLACK_WEBHOOK_URL` and
`SLACK_SIGNING_SECRET` must be set (`agent secrets status` shows where).

## AWS Spend Alerts

Every night at 1:03 AM the daemon checks whether the most recent day AWS has
billing data for is a statistical outlier against your last 30 days (more
than 1.5 standard deviations above the mean) — not just a busy day, a
genuine spike — and Slacks you the amount and the top contributing services
if so. Each day can only trigger this once, however many times the check
runs.

```bash
agent daemon check-cost-anomalies          # run the check right now instead of waiting for 1:03 AM
agent daemon check-cost-anomalies --force  # ...and alert again even if today already did
```

Read-only against Cost Explorer — safe to run any time, and a no-op if
nothing looks unusual.

## RDS Capacity Forecast

Everything else answers "is this healthy right now" — this answers "at this
rate, when does it run out." Each night, alongside the cost checks, the
daemon fits a plain trend line to every RDS instance's free storage over the
last 14 days of CloudWatch data and, if it's shrinking fast enough to hit
zero within 14 days, Slacks you the instance, the trend, and days left. It
only ever extrapolates a *shrinking* trend — flat or growing free space is
never reported as a countdown — and needs a handful of days of history
before it will guess at all.

```bash
agent db capacity-forecast              # every RDS instance's trend, not just critical ones
agent daemon check-rds-capacity         # run the nightly Slack check right now
agent daemon check-rds-capacity --force # ...and alert again even if this week already did
```

A critical instance alerts at most once a week, not every night, since a
storage trend moves slowly and a nightly repeat would just be noise.

## What You Can Configure

The `agent setup` wizard walks you through each integration:

| Integration | What it does | Required |
|---|---|---|
| **Anthropic Claude** | Powers all AI analysis and reasoning | Yes (or OpenAI/Groq) |
| **OpenAI / GPT** | Alternative AI provider | No |
| **Groq** | Fast, free-tier AI inference | No |
| **Voyage AI** | Semantic memory search (free, 200M tokens/month) | Recommended |
| **AWS** | EC2, EKS, load balancers, security groups, EIPs | For AWS commands |
| **Kubernetes** | Pod health, ingress, TLS, RBAC, storage, HPA | For K8s commands |
| **Jenkins** | CI/CD monitoring and self-healing (flaky builds, offline agents) | For Jenkins commands |
| **Gmail** | Read and triage your inbox with AI | For Gmail commands |
| **Custom APIs** | GitHub, Slack, Jira, or any OpenAI-compatible endpoint | Optional |

## Commands

### Kubernetes

```bash
agent k8s scan              # Quick health check — pods, nodes, issues
agent k8s full-scan         # Deep scan across 10 areas with AI analysis
agent k8s full-scan --areas nodes,pods,ingress,tls   # Specific areas
agent k8s full-scan --fix   # Auto-remediate issues found
```

### AWS

```bash
agent aws list              # Show EC2, EIPs, load balancers, security groups
agent aws list --fix        # Fix flagged issues (open SGs, unattached EIPs)
agent aws auth-status       # Show current AWS auth method and verify it works
agent aws switch-auth       # Switch between IAM Role / Access Key / SSO Profile
```

AtlasOS supports three AWS authentication methods — IAM Role (recommended for
EC2/EKS), Access Keys (local dev), and SSO Profile. `agent setup` walks you
through picking one. See [deployment/local/README.md](deployment/local/README.md)
for local setups and [deployment/hosted/README.md](deployment/hosted/README.md)
for running on EC2/EKS with an IAM role.

### Jenkins CI/CD

```bash
agent jenkins scan               # Jobs, agents, queue — AI diagnosis of every failure
agent jenkins diagnose <job>      # Deep dive on one job's failure, offer to apply the fix
agent jenkins heal                # Apply every safe, known fix (flaky tests, stuck builds)
agent jenkins watch --auto-fix    # Autonomous monitoring loop — heals known-safe issues, alerts on the rest
agent jenkins auth-status         # Verify the Jenkins connection
agent jenkins patterns            # Weekly recurring-failure analysis across incident history
```

Known-safe patterns (flaky tests, stuck queue items, hung builds) are detected by
fast regex matching — no AI call — and can be auto-fixed. Anything else (credential
expiry, disk full, syntax errors, offline agents) always requires human approval.
See [deployment/local/README.md](deployment/local/README.md) for setup.

### TLS / Ingress

```bash
agent tls check             # Check all ingress TLS certificates
agent tls fix               # Renew or flag expiring certs
agent domain live           # Live connectivity check on all ingress domains
```

### Gmail

```bash
agent gmail list            # List recent emails with AI summaries
agent gmail triage          # AI-powered inbox triage with labels
```

### DNS / Network

```bash
agent dns check             # Validate CoreDNS health and config
agent network check         # Check network policies and connectivity
```

### Memory

```bash
agent memory search "TLS cert expired"    # Semantic search across incident history
agent memory list                         # Show recent memories
```

### Evaluations

```bash
agent eval run              # Run all AI skill evaluations
```

### Setup

```bash
agent setup                 # Re-run the setup wizard at any time
```

### About / Documentation

```bash
agent about                 # Full architecture explanation from a live scan
agent about --no-claude     # Instant overview, no API call
agent about --section skills   # Just one section (skills, commands, status, …)
agent about --json > scan.json # Machine-readable project snapshot
agent commands              # List every command with descriptions
```

## Requirements

| Tool | Minimum Version | Purpose |
|---|---|---|
| Python | 3.11+ | Runtime |
| uv | any | Package manager |
| kubectl | any | Kubernetes commands |
| AWS CLI v2 | any | AWS commands |
| git | any | Version tracking |

## Configuration

All settings are stored in `.env` in the project root. `agent setup` manages this file automatically.

```env
ANTHROPIC_API_KEY=sk-ant-...
VOYAGE_API_KEY=pa-...          # Semantic memory search
AWS_AUTH_METHOD=iam_role        # iam_role (default) | access_key | sso_profile
AWS_REGION=ap-south-1
EKS_CLUSTER_NAME=my-cluster
LLM_MODEL=claude-haiku-4-5-20251001
LLM_EXPENSIVE_MODEL=claude-opus-4-8
```

See [Configuration → AWS](deployment/local/README.md) for the other two auth methods.

## Architecture

See **[ARCHITECTURE.md](ARCHITECTURE.md)** for the full system diagram, module map, data flow walkthroughs, and key design decisions.

```
src/agent/
├── cli.py              # All commands (Typer + Rich)
├── config.py           # Pydantic settings from .env
├── core/               # LLM client — single entrypoint for Claude
├── integrations/       # Raw data: kubectl, AWS boto3, TLS, DNS, network
├── skills/             # AI-powered operations (23 skills — see `agent about`)
├── memory/             # SQLite + ChromaDB + Vault markdown
└── observability/      # Structured logging + cost tracking
```

## License

MIT
