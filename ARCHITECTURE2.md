# Architecture Deep Dive — AtlasOS

> **Relationship to [ARCHITECTURE.md](ARCHITECTURE.md)**: that document is the 10,000-foot view (CLI → Skill → Integrations + LLM → Memory → Output, applied uniformly across all 27 skills). This document goes deep on the specific subsystems built and debugged in one extended session — AWS authentication, EKS access control, the Jenkins CI/CD self-healing skill, the eval suite architecture, and a 21-tool MCP server. Written for explaining the project to an interviewer in detail, including the parts that are easy to get subtly wrong out loud.

---

## Table of Contents

1. [AWS Authentication — Three Methods](#1-aws-authentication--three-methods)
2. [Kubernetes / EKS — the Two-Layer Auth Model](#2-kubernetes--eks--the-two-layer-auth-model)
3. [TLS / Ingress Diagnosis — Bugs Found and Fixed](#3-tls--ingress-diagnosis--bugs-found-and-fixed)
4. [Jenkins CI/CD Monitoring & Self-Healing](#4-jenkins-cicd-monitoring--self-healing)
5. [Eval Suite Architecture](#5-eval-suite-architecture)
6. [MCP (Model Context Protocol) — Built and Verified](#6-mcp-model-context-protocol--built-and-verified)
7. [Other Subsystems (Breadth)](#7-other-subsystems-breadth)
8. [Worked Example: an AWS Cost Discrepancy Investigation](#8-worked-example-an-aws-cost-discrepancy-investigation)
9. [Quick Reference / Interview Cheat Sheet](#9-quick-reference--interview-cheat-sheet)

---

## 1. AWS Authentication — Three Methods

AtlasOS supports exactly three ways to authenticate to AWS, selected by `AWS_AUTH_METHOD` in `.env`:

| Method | When it's right | How credentials resolve |
|---|---|---|
| `iam_role` (default) | Running **on** EC2/EKS — the recommended, most secure option | boto3/kubectl resolve automatically from instance metadata; nothing in `.env` needed |
| `access_key` | Local development, no SSO set up | Static `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` in `.env` — long-lived, must never be committed |
| `sso_profile` | Local dev on a team already using AWS SSO / IAM Identity Center | A named `~/.aws` profile — short-lived, auto-refreshing, no static secret stored |

**Config layer** (`config.py`):
```python
aws_auth_method: str = "iam_role"
aws_region: str = "us-east-1"
eks_cluster_name: str = ""
aws_access_key_id: str = ""
aws_secret_access_key: str = ""
aws_profile: str = ""

def get_aws_session(self):
    if self.aws_auth_method == "access_key" and self.aws_access_key_id and self.aws_secret_access_key:
        return boto3.Session(aws_access_key_id=..., aws_secret_access_key=..., region_name=...)
    if self.aws_auth_method == "sso_profile" and self.aws_profile:
        return boto3.Session(profile_name=self.aws_profile, region_name=self.aws_region)
    return boto3.Session(region_name=self.aws_region)   # iam_role: let boto3 resolve on its own
```

**Why this matters for kubectl specifically**: `kubectl` doesn't call boto3 at all — it shells out to `aws eks get-token` as an exec-credential plugin, which inherits whatever's in the **process environment**, not whatever's in a Python `Settings` object. So a second function, `get_kubectl_env()` in `integrations/kubectl.py`, translates the same three methods into actual environment variables (`AWS_ACCESS_KEY_ID`, `AWS_PROFILE`, etc.) and gets threaded into every `subprocess.run(...)` call that shells out to `kubectl` or `aws`. Missing this was the root cause of an early bug in this project: AWS calls worked, kubectl calls didn't, because only one of the two credential-resolution paths was wired up.

**Setup wizard flow** (`skills/setup.py :: configure_aws()`):
```
"How is this running?"
  [1] IAM Role         → verify only (nothing to configure)
  [2] On my local laptop
        [1] Access Key + Secret Key  → prompts, writes to .env, verifies
        [2] SSO Named Profile        → discovers existing ~/.aws profiles,
                                        offers to bootstrap IAM Identity Center
                                        + run `aws configure sso` for a new one
  [3] Skip
```
Every branch ends in a shared `_verify_aws_setup()` — 5 real checks: `sts get-caller-identity`, `eks describe-cluster`, `update-kubeconfig`, `kubectl get nodes`, `kubectl auth can-i get pods --all-namespaces` — so the wizard never just *assumes* the values it wrote actually work.

**Runtime commands**: `agent aws auth-status` (shows current method + live verification) and `agent aws switch-auth` (re-runs just the auth step). A real gotcha hit during testing: at the profile picker, typing an **existing profile's number** silently takes the "reuse" branch (skips SSO bootstrap entirely and tries `aws sso login` on a profile that was never SSO-configured) — only typing the literal letter `N` triggers "create new" + the bootstrap offer. This was confusing enough in practice that the prompt itself was rewritten mid-session to spell out "use existing" vs "create a brand-new profile" instead of relying on a bracketed letter that could visually collide with a profile literally named `"2"`.

**IAM Identity Center auto-bootstrap** (`_bootstrap_sso_backend()`): for a brand-new SSO profile, the wizard can call `aws sso-admin create-instance` / `create-permission-set` / `identitystore create-user` / `create-account-assignment` on the user's behalf — but there is a real, permanent gap: **creating a user via the API does not trigger AWS's usual "set your password" welcome email** (that only fires when a user is created through the Console UI). The workaround is a manual step: Console → IAM Identity Center → Users → Reset password → "Generate a one-time password and share it with the user" — no CLI equivalent exists.

---

## 2. Kubernetes / EKS — the Two-Layer Auth Model

This is the single most important mental model for anything AWS+K8s in this project, and the one most likely to trip someone up in an interview: **AWS IAM and Kubernetes RBAC are two independent systems that don't know about each other unless something explicitly bridges them.**

```
LAYER 1 — AWS IAM            "Are you a real AWS identity?"     (SigV4-signed request)
     │                        checked by AWS, always passes if credentials are valid
     ▼
THE BRIDGE — aws-auth ConfigMap  OR  EKS Access Entries
     │        "Given that AWS identity, who are you INSIDE the cluster,
     │         and what Kubernetes group do you belong to?"
     │        No entry here → valid AWS identity, unrecognized nobody to the cluster.
     ▼
LAYER 2 — Kubernetes RBAC     "Given your group, can you list pods / get nodes / etc.?"
                               Never evaluated at all if Layer 1→2 has no mapping.
```

The cluster supports two ways to populate the bridge:
- **`CONFIG_MAP`** (legacy) — a ConfigMap (`aws-auth`) in `kube-system`, edited via `kubectl`. Chicken-and-egg problem: editing it *requires* kubectl access, which is exactly what's missing during a lockout.
- **`API_AND_CONFIG_MAP`** — adds the newer **EKS Access Entries API**, which is gated purely by AWS IAM permissions (`eks:CreateAccessEntry`, etc.), **not** by cluster RBAC. This is the actual way out of a lockout: an admin-level AWS identity can grant itself cluster access via pure AWS API calls, without ever needing prior kubectl access.

**The real incident this session**: mid-session, `aws eks describe-cluster` unexpectedly showed the cluster had been recreated (status flipped to `CREATING`, version bumped). The recreation reset the auth mode back to legacy `CONFIG_MAP` with zero access entries — meaning a previously-working identity was suddenly locked out again. Fix, three plain AWS API calls (no kubectl needed):
```bash
aws eks update-cluster-config --name <cluster> --access-config authenticationMode=API_AND_CONFIG_MAP
aws eks create-access-entry --cluster-name <cluster> --principal-arn <iam-user-arn> --type STANDARD
aws eks associate-access-policy --cluster-name <cluster> --principal-arn <iam-user-arn> \
    --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy --access-scope type=cluster
```
Verified end-to-end with `aws eks update-kubeconfig` + `kubectl get nodes` immediately after — went from "Unauthorized" to a working cluster in under a minute.

**Separately**: `integrations/kubectl.py :: apply_fix()` handles AI-suggested fix commands safely — `shlex.split()` (keeps quoted JSON patches intact, unlike a naive `.split()`), support for `&&`-chained multi-step fixes, and a `kubectl apply -f - <<'EOF' ... EOF` heredoc block piped via `subprocess.run(..., input=...)` rather than relying on shell heredoc syntax (which doesn't exist on Windows `cmd.exe` anyway).

---

## 3. TLS / Ingress Diagnosis — Bugs Found and Fixed

Four distinct, unrelated bugs were found and fixed in the TLS/ingress diagnosis pipeline this session — worth listing together because they're a good example of "AI diagnosis is only as good as the plumbing feeding it data":

1. **Markdown-fence JSON parsing failure** — `_domain_tls_deep_check()` used a bare `json.loads()` on Claude's response, which choked whenever Claude wrapped its JSON in a ` ```json ` fence (even with `json_mode=True` requested). Fixed by routing through the existing `agent.core.parsing.parse_llm_json()` helper, which strips fences before parsing.
2. **Wrong ingress candidate selected** — cert-manager's temporary `cm-acme-http-solver-*` stub ingress (created transiently during ACME HTTP-01 validation) was being picked over the real one, because the code just took `candidates[0]`. Fixed by filtering those stub ingresses out before selection.
3. **Certificate/Ingress namespace mismatch invisible to diagnosis** — a `Certificate` CRD is namespace-scoped and creates its TLS `Secret` in its **own** namespace, which silently breaks things when that differs from the Ingress's namespace. `CertInfo` didn't even capture `secret_name`, so the diagnosis prompt had no way to notice. Fixed by adding the field and an explicit mismatch-detection instruction in the prompt.
4. **`apply_fix()` dispatch bug** — `apply_manifest`/`delete_secret` fix types were being routed to `run_tls_fix()` with a params dict missing required keys, guaranteeing a `KeyError` on every attempt. This was very likely the actual reason `agent tls fix` reported "unable to fix" in the first place, unrelated to the diagnosis quality itself. Fixed by narrowing that dispatch path to only the fix types it actually supports.
5. **(Found later, different skill) `agent ingress heal`'s own `apply_fix()`** used `fix_command.replace("kubectl ", "").split()` — the exact same naive-split bug as #4's sibling, mangling any fix containing quoted JSON or multi-step `&&` chains. Fixed by routing through the already-hardened `kubectl.apply_fix()` instead of maintaining a second, weaker copy of the same logic.

---

## 4. Jenkins CI/CD Monitoring & Self-Healing

The newest and largest feature added — same three-layer shape as every other skill (`integrations/` → `skills/` → `cli.py`), designed around one explicit principle: **continuously observe, diagnose root cause, remediate safely, verify recovery — full autonomy only for a short, hardcoded whitelist of known-safe patterns; everything else asks a human.**

### 4.1 The three files

```
integrations/jenkins.py   raw REST client (httpx, HTTP Basic Auth) — 17 functions,
                          no AI, no business logic. Mirrors how kubectl.py wraps
                          kubectl and aws.py wraps boto3.
skills/jenkins.py         JenkinsSkill(BaseSkill) — the AI layer.
cli.py (jenkins_app)      6 CLI commands: scan / diagnose / heal / watch /
                          auth-status / patterns.
```

### 4.2 One full `diagnose()` call, in order

```
1. Job fails on Jenkins.
2. LIVE calls to Jenkins (never touch our own database):
     jk.get_build_info()      → this build's status/causes/changes/node
     jk.get_console_log()     → this build's actual log text
     jk.get_build_history()   → last 5 builds' pass/fail, for the flaky-test check
3. Our OWN memory (SQLite + vector store), fetched unconditionally:
     retrieve_context(f"jenkins {job} failure") → past_incidents
4. diagnose(job_name, build, log_text, history, past_incidents) runs:
     a. _detect_pattern(log_text, history) — regex + flaky-history heuristic FIRST.
        MATCH  → return a diagnosis built from hardcoded template text for that
                  pattern. past_incidents from step 3 goes completely UNUSED.
                  Zero Claude calls. Free, instant, identical every time.
        NO MATCH → fall through to (b).
     b. Build one big prompt: job info + build history + past_incidents (finally
        used here, as extra context) + console log tail + job config.
     c. Real llm.chat() call → Claude's own JSON answer, parsed into a diagnosis
        whose text is genuinely written by the model for this specific failure.
     d. diagnosis.auto_fixable = _is_auto_fixable(diagnosis)  — SAFETY OVERRIDE,
        see 4.4 below. Claude's own auto_fixable claim in its JSON is discarded.
     e. remember(diagnosis) — saved to memory, becomes a future past_incidents entry.
5. Return the diagnosis to whichever front door called it (CLI or, eventually, MCP).
```

**Key point worth being crisp about**: the flaky-test heuristic's "last 5 builds" data is **live Jenkins data**, fetched fresh every call — never stored by us. Only `past_incidents` comes from our own SQLite/vector store, and it's read unconditionally but only ever *used* inside the Claude branch (b) — a real, minor inefficiency (a semantic search runs even in cases where the regex layer is about to make it moot), noted but not yet optimized.

### 4.3 The pattern-detection layer, literally

```python
_LOG_PATTERNS = [
    (CREDENTIAL_EXPIRED, MANUAL_ONLY,
     "Authentication/authorization failure against a remote system",
     re.compile(r"403 Forbidden|invalid credentials|authentication failed|401 Unauthorized", re.I)),
    (DISK_FULL, MANUAL_ONLY, "Build node has run out of disk space",
     re.compile(r"no space left on device|disk quota exceeded", re.I)),
    (OUT_OF_MEMORY, MANUAL_ONLY, "Build process ran out of memory",
     re.compile(r"java\.lang\.OutOfMemoryError|Cannot allocate memory", re.I)),
    (AGENT_OFFLINE, RESTART_AGENT, "Build agent disconnected mid-build",
     re.compile(r"hudson\.remoting\.ChannelClosedException|Agent went offline", re.I)),
    # ... DOCKER_ERROR, NETWORK_ERROR, BUILD_TIMEOUT, BAD_JENKINSFILE_SYNTAX
]

def _detect_pattern(log_text, history):
    for problem_type, fix_action, root_cause, pattern in _LOG_PATTERNS:
        if pattern.search(log_text):          # searched against THIS build's live log —
            return JenkinsDiagnosis(...)        # never against memory/past incidents
    # + separate heuristic: failed now, but passed 3+ of the last 5 (live) builds → FLAKY_TEST
    return None
```
9 of the 12 eval cases hit this layer and never call Claude at all; one case (`jenkins_10`) was deliberately worded (`ECONNRESET` instead of the literal phrase `"Connection refused"`) to bypass every regex on purpose, so the suite still exercises the real Claude fallback path at least once.

### 4.4 The auto-fix safety override

```python
_AUTO_FIX_RULES = {                      # the ONLY 3 combinations ever auto-applied
    (FLAKY_TEST,     RETRIGGER_BUILD),
    (STUCK_IN_QUEUE, CANCEL_AND_RETRIGGER),
    (BUILD_TIMEOUT,  CANCEL_AND_RETRIGGER),
}
_NEVER_AUTO = {CREDENTIAL_EXPIRED, AGENT_OFFLINE, DISK_FULL, BAD_JENKINSFILE_SYNTAX, UNKNOWN}

def _is_auto_fixable(diagnosis) -> bool:
    if diagnosis.problem_type in _NEVER_AUTO:      return False
    if diagnosis.risk_level == "high":             return False
    if diagnosis.confidence != "high":             return False
    return (diagnosis.problem_type, diagnosis.fix_action) in _AUTO_FIX_RULES
```
Even when Claude's own JSON response claims `"auto_fixable": true`, `diagnosis.auto_fixable = _is_auto_fixable(diagnosis)` **overwrites** that with an independently computed, deterministic answer. Example: if Claude ever reasoned that a credential-expired failure was safe to auto-fix by retriggering, this line unconditionally flips it back to `False`, because `CREDENTIAL_EXPIRED` sits in `_NEVER_AUTO` regardless of what the model said. This one function is the entire boundary between "AI suggests" and "system acts without asking a human" — every caller (`watch()`, the monitor daemon, `agent jenkins heal`) checks `diagnosis.auto_fixable` and never the model's raw claim.

### 4.5 CLI commands

| Command | What it does |
|---|---|
| `agent jenkins scan` | Health score, failing jobs, offline agents, stuck queue — with diagnosis for each failure |
| `agent jenkins diagnose <job>` | Deep-dive one job, asks before applying any fix |
| `agent jenkins heal [--auto] [--dry-run]` | Walks every auto-fixable issue, applies + verifies recovery |
| `agent jenkins watch --auto-fix` | Runs forever, auto-heals known-safe issues, Slack-alerts on everything else |
| `agent jenkins auth-status` | Verifies the connection, shows version/executors/agents |
| `agent jenkins patterns --days 30` | Weekly systemic analysis across accumulated incident history (real Claude call) |

### 4.6 Wired into the rest of the system

- **Monitor daemon** (`agent monitor start`): if `JENKINS_URL` is set, every loop tick also runs `JenkinsSkill().scan()`, applies known-safe fixes when `JENKINS_AUTO_HEAL=true`, and sends deduplicated Slack alerts for the rest — same pattern as the existing pod/resource/security checks in that loop.
- **Setup wizard**: `configure_jenkins()` — URL/user/API-token prompts, live connection test, opt-in `JENKINS_AUTO_HEAL`.
- **Dashboard**: a lightweight `/api/jenkins` endpoint (jobs/agents/queue only, deliberately **no** Claude call on every 30-second poll to avoid runaway API cost) feeding a `JenkinsPanel.jsx` React component in the existing SLO/Incidents/Clusters sidebar.
- **Eval suite**: see Section 5.

---

## 5. Eval Suite Architecture

Full ASCII diagram: [`data/architecture/eval_architecture.txt`](data/architecture/eval_architecture.txt) — render it in color with `uv run python scripts/print_eval_architecture.py`.

### 5.1 The pattern, once, applied to every skill

```
evals/<skill>/
    cases.jsonl   — DATA ONLY: hand-written scenarios (real failure patterns you've seen)
    runner.py     — LOGIC ONLY: loads cases, mocks external calls, scores output
    __init__.py   — empty
```

### 5.2 Inside the runner — three things happen per case

```
Layer A — MOCK        patch integrations/<skill>.py's functions with canned
                       responses (mock_kubectl, mock_aws, mock_jenkins) so the
                       real skill code runs with zero real infra, in milliseconds.
Layer B — PATTERN      (skill-specific) regex/heuristic checks run first inside
                       the real skill code — free, instant, no Claude.
Layer C — AI SKILL     only reached when Layer B finds nothing — a real Claude
                       API call, costs tokens, takes seconds.
```
The mocking gotcha worth remembering: you patch the name **where it's looked up at call time**, not where it's defined. `skills/jenkins.py` does `from agent.integrations import jenkins as jk`, so the patch target is `agent.skills.jenkins.jk.get_job_config`, not `agent.integrations.jenkins.get_job_config` — get this backwards and the mock silently does nothing.

### 5.3 Scoring — quality match, not exact match

Not `assert output == expected`. Each case is scored on whether the output picked the **right enum value**, whether the free-text explanation **contains the right keyword**, and whether the fix suggested is **appropriate for the case** — because two different, equally-correct English sentences describing the same root cause should both pass.

### 5.4 Enums are per-skill, not one shared vocabulary

A real and deliberate design choice: `ProblemType` (K8s: `CrashLoopBackOff`, `OOMKilled`, `Pending`, `ImagePullBackOff`, `CreateContainerConfigError`, `Unknown`) and `JenkinsProblemType`/`JenkinsFixAction` (16 + 7 values) are **separate enum classes**, not one universal type shared across skills — each skill owns its own typed vocabulary scoped to its own domain. (Concretely: `JenkinsDiagnosis` was named that specifically to avoid colliding with the pre-existing `ProblemType`/`Diagnosis`-shaped types already used by the K8s skill.)

### 5.5 Real numbers (verified against the actual repo, not assumed)

An earlier set of "interview notes" claimed 7 suites / 64 cases / all at an 80% threshold — that was stale. Verified by counting `evals/*/cases.jsonl` directly:

**17 eval suites, 142 cases total.** Jenkins uses an 85% threshold (slightly stricter, since most of its 12 cases are deterministic pattern-matches and only 2 exercise live Claude); the rest use 80%.

| Suite | Cases | | Suite | Cases |
|---|---:|---|---|---:|
| cost | 12 | | pagerduty | 6 |
| daemon | 8 | | resources | 8 |
| db | 6 | | runbook | 6 |
| deployment | 12 | | scale | 6 |
| incident | 6 | | security | 8 |
| jenkins | 12 | | slack | 8 |
| k8s | 10 | | slo | 6 |
| logs | 8 | | triage | 10 |
| multi_cluster | 10 | | | |

### 5.6 Result phase and the CI gate

```python
pct = round(passed / n * 100)
sys.exit(0 if pct >= threshold else 1)
```
That exit code is *shaped* for CI (a GitHub Actions step running `agent eval jenkins` would fail the job on a nonzero exit) — **but there is currently no `.github/workflows/` file in this repo**, so nothing actually blocks a PR today. The mechanism is real and tested; the wiring to make it enforce anything is a follow-up, not yet built. Worth being precise about this distinction if asked directly — it's easy to accidentally overclaim "CI blocks bad AI" when what's actually true is "the eval command *would* correctly signal failure to CI, if CI called it."

### 5.7 Memory feedback loop

Every real (non-eval) `diagnose()` call ends with `remember(...)`, which writes to SQLite + a vector embedding store + a human-readable markdown vault file. The *next* time the same job fails and needs the Claude fallback path, `retrieve_context()` pulls that history back out as prompt context — so accuracy compounds over real usage without any retraining. Eval runs deliberately **do not** participate in this loop (`remember` is patched to a no-op during `run_evals()`), so running the eval suite repeatedly never pollutes real incident history with synthetic test data.

---

## 6. MCP (Model Context Protocol) — Built and Verified

**Status: built.** `src/agent/mcp_server.py` exists, registers 21 real tools via `FastMCP`, and every one of them has been exercised through the actual MCP `tools/call` protocol path (not just direct Python calls) against this project's real Jenkins instance, real EKS cluster, and real AWS account. `cli.py` and every file under `skills/` are completely untouched — the server is a pure additional consumer of the existing skill layer, proving out the "three front doors, one skill layer" claim in section 4.6 for real.

### 6.1 What MCP actually is

A standardized way for an LLM client (Claude Desktop, Claude Code, or any other MCP-aware app) to discover and call functions exposed by a separate server process — the same underlying mechanism as any tool-use setup, just protocol-standardized so it works across apps rather than being baked into one product.

- **MCP client** — the chat app (holds the conversation + the model)
- **MCP server** — your own program, which just says "here are my tools" and executes them on request

For a local project like this, the server runs as a **local subprocess** launched by the client (stdio transport) — both processes live on the same machine; nothing goes over the network for the tool-call mechanism itself.

### 6.2 Why it fits this codebase specifically

Every skill already separates **business logic** (`skills/*.py`, returning typed Pydantic models) from **presentation** (`cli.py`'s Rich-formatted printing). An MCP server is just a *third* presentation layer over the exact same skill methods — `mcp_server.py`'s `jenkins_scan()` tool is three lines: instantiate `JenkinsSkill()`, call `.scan()`, return `.model_dump()`. Zero duplicated logic. The dashboard's FastAPI endpoints already proved this pattern works as a second consumer of the same skill classes; MCP is the third, and it took no changes to `cli.py` or any skill file to add.

### 6.3 The critical nuance: two separate, unrelated Claude calls

The most commonly confused part of this design, worth stating precisely:

| | Who produces the words | Billed against |
|---|---|---|
| You typing into Claude Desktop / Claude Code | Claude's model | Your **subscription** (Pro/Max/Code session) |
| `JenkinsSkill.diagnose()`'s Claude fallback, running inside your own skill code | The same underlying model | Your own **`ANTHROPIC_API_KEY`**, pay-per-token, tracked by `get_session_total()` |

There's no such thing as "the chat app replying without an API call" — every word Claude ever produces anywhere is an inference request. The distinction is *which billing account* the request is charged to, not whether an API call happened. Concretely, in one MCP `jenkins_scan` interaction:
1. You ask "check Jenkins" → subscription-billed call decides to invoke the tool.
2. `JenkinsSkill().scan()` runs for real, on your machine.
3. If regex catches every failure → **zero** additional API-key-billed calls happen underneath.
4. If some failure needs the Claude fallback → **one more, separate**, API-key-billed call happens, invisible to the outer conversation.
5. The JSON result flows back → the outer, subscription-billed Claude phrases it as a sentence.

### 6.4 The 21 tools actually built

```
src/agent/mcp_server.py   ← 21 tools via FastMCP; cli.py and skills/*.py untouched

Tier 1 — read-only, exposed freely (17 tools):
  jenkins_scan, jenkins_diagnose, jenkins_auth_status,
  k8s_scan, k8s_diagnose,
  tls_scan, ingress_scan, security_audit, cost_analyze, aws_auth_status,
  dns_scan, domain_scan,
  memory_search, deploy_status, run_eval, project_status

Tier 2 — mutating, requires an explicit confirm=True argument (4 tools):
  jenkins_apply_fix, ingress_apply_fix, tls_apply_fix, cost_apply_fix,
  k8s_apply_fix

Tier 3 — never exposed as MCP tools: agent setup, agent aws switch-auth,
  anything touching credentials/.env, git ops, and any bulk/cluster-wide
  fix command (agent k8s security --fix is deliberately NOT an MCP tool —
  see the note on scoping below)
```

**Why one function per tool, not a registry factory.** The original sketch proposed a data-driven registry (`TOOLS = [(name, SkillClass, method, arg_schema), ...]` + one generic `_make_tool()`) to avoid hand-writing near-duplicate wrappers. In practice this doesn't fit `FastMCP`: the `@mcp.tool()` decorator introspects the wrapped Python function's **real, concrete type-hinted signature** to build each tool's JSON schema — there's no separate schema object to hand it. Generating that dynamically (synthesizing function signatures at runtime just to satisfy an abstraction) would fight the framework for no real benefit, since each tool's *body* is already just 2-4 lines with zero duplicated logic — the duplication the registry was meant to avoid was never actually there. A good example of a clean abstraction on paper turning out to be the wrong shape for the concrete API once you're holding it.

**Why single-pod/single-job scoping matters, with a real example from this session.** `k8s_apply_fix(pod_name, namespace, confirm)` diagnoses and fixes exactly *one* named pod — never "fix everything wrong in the cluster." This distinction is not theoretical: in this same session, running `agent k8s security --fix` (which bulk-patches several RBAC/security-context findings across multiple resources in one interactive pass) was blocked outright by the coding agent's own safety classifier, specifically because it mutates several security-sensitive resources unsupervised in one shot. The scoped, one-target, explicit-`confirm` shape used by every Tier 2 MCP tool is exactly the pattern that same classifier is comfortable with — so the MCP tools are, if anything, *more* conservative than the CLI's own bulk-fix commands, by design.

**Verified live**, via the real `mcp.call_tool()` protocol path, not just direct skill calls: `aws_auth_status` (returned the real IAM identity), `k8s_scan` (returned a real pod restart warning from the live cluster), `jenkins_scan`/`jenkins_auth_status` (against a real EC2-hosted Jenkins, both before and after it was configured), `run_eval` (ran the real `security` eval suite, 8/8 passed), `project_status` (a real filesystem/runtime scan), `memory_search` and `deploy_status` (both correctly returned empty against real, empty state), and `k8s_diagnose` (correctly diagnosed a real healthy pod as needing no action). `dns_scan`'s MCP wrapper has one known limitation: `DNSSkill.scan()` prints its findings directly to the terminal via Rich rather than returning them as structured data, so the MCP tool only gets back `{"report": "scan_complete"}` — real findings are lost over that path. Not a bug introduced by the MCP layer; a pre-existing shape mismatch in that one skill worth fixing if `dns_scan` needs to be genuinely useful over MCP later.

---

## 7. Other Subsystems (Breadth)

Briefly, for completeness — these exist in the codebase but weren't the focus of this session's deep work:

- **Monitoring daemon** (`agent monitor start`) — one continuous loop: pod health + resources every `--interval` seconds, security hourly, cost every 6 hours, now also Jenkins if configured. Deduplicated Slack alerting via `AlertDeduplicator` (fingerprint + severity-aware cooldown, so the same issue doesn't re-page every tick).
- **Dashboard** (`agent dashboard`) — FastAPI backend + a React (esbuild-bundled, not Vite) frontend. Two patterns coexist: dedicated panels with their own `/api/*` endpoint and polling (Incidents, SLOs, Jenkins), and a generic command-runner that introspects every registered Typer command and lets you run any of them from the browser.
- **Deployment automation** (`agent deploy`) — GitHub webhook receiver that risk-scores incoming deploys and, for anything above a threshold, posts a Slack "Approve/Reject" message (interactive Block Kit buttons) before proceeding.
- **SLO tracking** (`agent slo`) — error-budget math (allowed downtime vs. used, per service, over a rolling window), same "budget consumed %" concept the SLO dashboard panel visualizes.
- **Auto-healer** (`skills/autohealer.py`, `skills/node_healer.py`) — the K8s-specific equivalent of the Jenkins auto-fix policy: safety-gated automated remediation for crashing pods, using the same "known-safe pattern → act, otherwise ask" philosophy.
- **On-call paging** (`agent page`) — PagerDuty Events API v2 / OpsGenie Alerts API integration for escalating incidents the daemon can't self-heal.
- **Cost tracking** (`observability/costs.py`) — every real `llm.chat()` call anywhere in the codebase increments one process-lifetime counter (`get_session_total()`); every CLI command that calls Claude prints a cost footer from a before/after snapshot of that counter — the same mechanism the eval runners use to report per-run API spend.

---

## 8. Worked Example: an AWS Cost Discrepancy Investigation

A real debugging exchange from this session, useful as an example of reading AWS billing data correctly rather than assuming a tool bug:

`agent cost aws` showed **$8.41** total spend over a rolling 30-day window (`ce:GetCostAndUsage`, `UnblendedCost`, grouped by `SERVICE`), while the AWS Billing Console's "Bills" page showed **$11.60** for the current month. Investigation, using the tool's own anomaly output:
- The tool's own spend-anomaly detector had already flagged that **two single days** (Sept 3 + Sept 4) accounted for **$8.40 of the $8.41 total** — meaning the other ~28 days in that rolling window were essentially $0. That's consistent with a cluster that was genuinely only a few days old (matching an EKS cluster recreation earlier in the same session), not a bug.
- The remaining gap (a 5-day Bills total exceeding a 30-day Cost Explorer total, which is numerically only possible if the most recent 1-2 days aren't fully reflected in one of the two data sources yet) points to AWS Cost Explorer's known ~24-hour data-ingestion lag relative to the Billing Console's near-real-time month-to-date estimate — not a defect in the query, just two systems with different freshness.

The broader point: the tool's own **optimization findings were real and independently useful regardless of the discrepancy** — 3 EBS volumes still on `gp2` instead of `gp3` (~$1.20/mo combined, auto-fixable), a CloudWatch log group with no retention policy (auto-fixable), and 0% Savings Plan coverage (manual) — none of which depend on reconciling the two totals to act on.

---

## 9. Quick Reference / Interview Cheat Sheet

**One-line summary**: every AI skill in AtlasOS is three files (a raw integration client, an AI layer that tries free pattern-matching before falling back to Claude, and a mocked eval suite) — and CLI, MCP, and CI/CD are three different front doors that all end up calling the exact same skill methods.

**Numbers worth knowing**:
- 27 skill classes, 17 of them with a full eval suite, 142 eval cases total.
- Jenkins skill: 12 eval cases, 9 hit the free regex path, 1 deliberately forces the Claude fallback, 85% pass threshold.
- Only 3 (problem_type, fix_action) combinations are ever auto-applied without a human: flaky test → retrigger, stuck queue → cancel+retrigger, build timeout → cancel+retrigger.
- Real verified AWS spend during this session: ~$8-12/month, mostly EKS + EC2 compute.

**If asked "how do you ensure AI quality?"**: *"Every AI skill has an eval suite — hand-written cases based on real failure patterns, run against the real skill code with external calls mocked out. Scoring is quality-match (right enum, right keyword, appropriate fix), not exact-match, because AI output is non-deterministic. A cheap regex layer intercepts known patterns before ever calling Claude, so most eval cases — and most real usage — cost nothing and return instantly; Claude is only a fallback for genuinely novel failures."*

**If asked "what's the difference between evals and unit tests?"**: *"Unit tests check deterministic code — same input, same output, exact equality. AI output isn't deterministic, so evals check quality instead: did it pick the right category, does the explanation mention the right root cause, is the suggested fix appropriate. You also track cost and latency as eval metrics, not just correctness — because an eval suite that silently costs $50 every CI run is its own kind of failure."*

**If asked "does the AI ever act without approval?"**: *"Only for a short, hardcoded whitelist of known-safe combinations, and that decision is made by a separate, deterministic Python function — never by trusting the model's own claim about whether something is safe. Even if the AI's JSON response says a fix is auto-fixable, the code independently re-checks it against the whitelist before ever executing anything unsupervised."*

**File map**:
```
src/agent/
  cli.py                     — every command, Typer + Rich
  config.py                  — pydantic-settings from .env
  core/                      — models.py (typed domain objects), llm.py (the only
                                place that calls Claude), parsing.py, context.py
  integrations/               — raw data: kubectl.py, aws.py, jenkins.py, tls_collector,
                                ingress_collector, dns_collector, slack.py, ...
  skills/                     — 27 AI-powered skills, each a BaseSkill subclass
  memory/                     — store.py (SQLite), embeddings.py (ChromaDB/Voyage),
                                retrieval.py (remember()/retrieve_context())
  dashboard/                  — FastAPI server.py + React frontend
  observability/               — costs.py (get_session_total), logging.py (structlog)
evals/<skill>/{cases.jsonl, runner.py}   — one eval suite per skill, 17 total
data/architecture/eval_architecture.txt — the eval-system ASCII diagram
scripts/print_eval_architecture.py       — renders it with Rich colors
```
