# AI Agentic OS

An AI-powered command-line operations platform. Manage Kubernetes clusters, AWS infrastructure, Gmail, and more — all from one unified CLI with natural language understanding and persistent memory.

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
```

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
AWS_PROFILE=my-profile          # AWS named profile
AWS_DEFAULT_REGION=ap-south-1
LLM_MODEL=claude-haiku-4-5-20251001
LLM_EXPENSIVE_MODEL=claude-opus-4-8
```

## Architecture

```
src/agent/
├── cli.py              # Typer CLI — all commands
├── config.py           # Pydantic settings from .env
├── core/               # LLM client (Anthropic + fallbacks)
├── integrations/
│   ├── aws.py          # EC2, EIP, LB, SG collectors + fixers
│   ├── kubectl.py      # Kubernetes typed API wrappers
│   └── collectors.py   # Parallel cluster data collection
├── memory/
│   ├── store.py        # SQLite memory store
│   ├── embeddings.py   # ChromaDB + Voyage AI embeddings
│   └── retrieval.py    # remember() / recall() API
├── skills/
│   ├── full_scan.py    # k8s full-scan AI analysis
│   ├── gmail.py        # Gmail skill
│   └── setup.py        # Setup wizard
└── observability/
    └── logging.py      # Structured logging
```

## License

MIT
