# AtlasOS — instructions for any Claude Code session working in this repo

## Never bypass the CLI/MCP layer for real operations

This project's core architecture principle is: **CLI and MCP both call the
exact same shared skill layer** (`src/agent/skills/*.py`) — see
[ARCHITECTURE2.md](ARCHITECTURE2.md) section 6 and the module docstring in
`src/agent/mcp_server.py`. That's not a style preference; it's the only place
safety checks (`core/safety.py`), the audit trail (`memory.remember()`), and
confirm-gates for mutating actions actually run.

**If you need Jenkins/K8s/AWS/etc. data or actions, use one of these two
paths — nothing else:**

```bash
agent jenkins jobs           # CLI
agent jenkins scan
agent k8s scan
# ... or the equivalent MCP tool (jenkins_list_jobs, jenkins_scan, k8s_scan, ...)
```

**Do NOT, under any circumstance:**
- Read `.env` or any credentials file directly to extract a token/key/URL so
  you can construct your own `curl`/API call, "just this once," even for a
  read-only operation. This has happened before in this repo (a session hit
  friction with an MCP tool, searched for `JENKINS_URL`/`JENKINS_USER`/
  `JENKINS_TOKEN` in the environment, and hand-rolled its own authenticated
  request when the proper tool seemed slow or annoying).
- Import an internal module from `src/agent/integrations/*.py` or
  `src/agent/skills/*.py` directly in a scratch script to "call the same
  function the MCP server uses" as a workaround. Even when the specific
  function is read-only and the data returned would be identical, this
  defeats the audit trail and sets a precedent that will eventually apply to
  something that mutates real infrastructure.
- Assume a tool being slow, erroring, or unclear is a reason to route around
  it. If an `agent` command or MCP tool is slow/broken, **say so to the user
  and/or fix the actual tool** (see `src/agent/cli.py` / `src/agent/mcp_server.py`
  / `src/agent/skills/*.py`) — do not improvise an equivalent path that skips
  the shared layer.

If you're genuinely unsure whether something needs a new CLI command / MCP
tool versus being out of scope, ask the user rather than assembling your own
bypass.

## Everything else

See [ARCHITECTURE.md](ARCHITECTURE.md) for the system-wide design and
[ARCHITECTURE2.md](ARCHITECTURE2.md) for a deep dive on AWS auth, EKS RBAC,
the Jenkins self-healing skill, the eval suite design, and the MCP server —
including the exact tiering rules for which operations are read-only vs.
require `confirm=True`.
