#!/usr/bin/env bash
# AI Agentic OS — one-line installer
# Usage: curl -fsSL https://raw.githubusercontent.com/YOUR_USERNAME/Ai-agentic-Os/main/install.sh | bash
#
# What it does:
#   1. Checks Python 3.11+
#   2. Installs uv (package manager) if missing
#   3. Warns about optional deps: kubectl, AWS CLI
#   4. Runs: uv sync + uv pip install -e .
#   5. Launches: agent setup (interactive wizard)

set -e

RED='\033[0;31m'
GRN='\033[0;32m'
YEL='\033[1;33m'
CYN='\033[0;36m'
BLD='\033[1m'
RST='\033[0m'

ok()   { echo -e "${GRN}✓${RST}  $*"; }
warn() { echo -e "${YEL}⚠${RST}  $*"; }
err()  { echo -e "${RED}✗${RST}  $*"; }
info() { echo -e "${CYN}→${RST}  $*"; }
sep()  { echo -e "${CYN}────────────────────────────────────────────${RST}"; }

echo
echo -e "${BLD}${CYN}  AI Agentic OS — Installer${RST}"
sep
echo

# ── 1. Python 3.11+ ──────────────────────────────────────────────────────────

PYTHON=""
for cmd in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$cmd" &>/dev/null; then
        version=$("$cmd" -c "import sys; print(sys.version_info[:2])" 2>/dev/null)
        # Extract major.minor
        major=$("$cmd" -c "import sys; print(sys.version_info.major)" 2>/dev/null)
        minor=$("$cmd" -c "import sys; print(sys.version_info.minor)" 2>/dev/null)
        if [ "$major" -eq 3 ] && [ "$minor" -ge 11 ] 2>/dev/null; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    err "Python 3.11 or newer is required."
    echo "   Install from: https://www.python.org/downloads/"
    echo
    exit 1
fi

PY_VER=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
ok "Python $PY_VER found ($PYTHON)"

# ── 2. uv ────────────────────────────────────────────────────────────────────

if ! command -v uv &>/dev/null; then
    info "Installing uv (fast Python package manager)…"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # Source the shell profile to pick up uv in PATH
    export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
    if ! command -v uv &>/dev/null; then
        err "uv installation failed. Install manually: https://docs.astral.sh/uv/"
        exit 1
    fi
fi

UV_VER=$(uv --version 2>&1 | head -1)
ok "uv ready — $UV_VER"

# ── 3. Optional dependencies ─────────────────────────────────────────────────

echo
sep
info "Checking optional dependencies…"
echo

if command -v kubectl &>/dev/null; then
    ok "kubectl found — Kubernetes features available"
else
    warn "kubectl not found — Kubernetes commands will be unavailable"
    echo "   Install: https://kubernetes.io/docs/tasks/tools/"
fi

if command -v aws &>/dev/null; then
    ok "AWS CLI found — AWS features available"
else
    warn "AWS CLI not found — AWS commands will be unavailable"
    echo "   Install: https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html"
fi

if command -v git &>/dev/null; then
    ok "git found"
else
    warn "git not found — some features may not work"
fi

# ── 4. Install dependencies ───────────────────────────────────────────────────

echo
sep
info "Installing Python dependencies with uv…"
echo

uv sync

info "Installing AI Agentic OS in editable mode…"
uv pip install -e .

echo
ok "Installation complete"

# ── 5. Create data directory ──────────────────────────────────────────────────

mkdir -p data/vault
ok "data/ directory ready"

# ── 5b. Bootstrap .env from example if not present ───────────────────────────

if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    cp .env.example .env
    ok ".env created from .env.example — add your ANTHROPIC_API_KEY before running"
elif [ ! -f ".env" ]; then
    warn ".env not found — run 'agent setup' or copy .env.example to .env"
fi

# ── 5. Verify agent CLI ───────────────────────────────────────────────────────

if ! command -v agent &>/dev/null; then
    # Try uv run fallback
    if uv run agent --help &>/dev/null 2>&1; then
        ok "CLI available via: uv run agent"
        AGENT_CMD="uv run agent"
    else
        warn "Could not verify 'agent' CLI. Try: source ~/.bashrc or restart terminal"
        AGENT_CMD="agent"
    fi
else
    ok "CLI available: agent"
    AGENT_CMD="agent"
fi

# ── 6. Run setup wizard ───────────────────────────────────────────────────────

echo
sep
echo
echo -e "${BLD}Ready to configure. Launching setup wizard…${RST}"
echo -e "${CYN}(Press Ctrl+C at any time to skip and run later with: agent setup)${RST}"
echo

if [ "$AGENT_CMD" = "uv run agent" ]; then
    uv run agent setup
else
    agent setup
fi

echo
sep
echo -e "${GRN}${BLD}  All done!${RST}"
echo -e "  Run ${CYN}agent --help${RST} to explore available commands."
echo
