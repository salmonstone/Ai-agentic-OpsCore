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
