"""
Living documentation for AtlasOS.

`AboutSkill` scans the ACTUAL project at runtime — every .py file under
src/agent/, the evals/ suites, pyproject.toml, .env.example, and the webhook
mappings — then reports structure, live runtime state, and (optionally) a
Claude-generated architecture explanation.

Nothing here is hardcoded: add a new file, skill, or eval suite and the next
`agent about` picks it up automatically.
"""
from __future__ import annotations

import ast
import os
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

from agent.config import settings
from agent.core.async_utils import run_sync
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill

log = get_logger(__name__)

# ── Paths — resolved from this file, never hardcoded to a machine ────────────
_AGENT_DIR = Path(__file__).resolve().parent.parent          # src/agent
_SRC_DIR = _AGENT_DIR.parent                                  # src
_PROJECT_ROOT = _SRC_DIR.parent                               # repo root
_EVALS_DIR = _PROJECT_ROOT / "evals"
_DATA_DIR = _PROJECT_ROOT / "data"

_SKIP_DIR_PARTS = {"__pycache__", "node_modules", "dist", ".venv", ".git"}

# path-segment → human category
_CATEGORY_BY_SEGMENT = {
    "core": "Core Engine",
    "memory": "Memory System",
    "skills": "Skills",
    "integrations": "Integrations",
    "observability": "Observability",
    "dashboard": "Dashboard",
    "tools": "Tool Definitions",
}

# Friendly labels for the skills table. Discovery is dynamic — this map only
# prettifies known skill classes; unknown ones still appear with a derived name.
_SKILL_DISPLAY: dict[str, dict] = {
    "K8sSkill":             {"label": "K8s Monitor",       "eval": "k8s"},
    "LogAnalysisSkill":     {"label": "Log Analysis",      "eval": "logs"},
    "SecurityAuditSkill":   {"label": "Security Audit",    "eval": "security"},
    "ResourceMonitorSkill": {"label": "Resource Monitor",  "eval": "resources"},
    "TLSSkill":             {"label": "TLS Manager",       "eval": None},
    "TLSMonitorSkill":      {"label": "TLS Monitor",       "eval": None},
    "CostAnalysisSkill":    {"label": "Cost Analysis",     "eval": "cost"},
    "DeploymentSkill":      {"label": "Deployment Mgmt",   "eval": "deployment"},
    "EmailTriageSkill":     {"label": "Email Triage",      "eval": "triage"},
    "ClusterOverviewSkill": {"label": "Cluster Overview",  "eval": None},
    "MultiClusterSkill":    {"label": "Multi-Cluster",     "eval": "multi_cluster"},
    "AwsSkill":             {"label": "AWS Ops",           "eval": None},
    "IngressSkill":         {"label": "Ingress",           "eval": None},
    "NetworkSkill":         {"label": "Network",           "eval": None},
    "DNSSkill":             {"label": "DNS",               "eval": None},
    "DomainSkill":          {"label": "Domain",            "eval": None},
    "NodeHealerSkill":      {"label": "Node Healer",       "eval": None},
    "AutoHealerSkill":      {"label": "Auto Healer",       "eval": None},
    "FullScanSkill":        {"label": "Full Scan",         "eval": None},
    "GmailSkill":           {"label": "Gmail",             "eval": None},
    "JobTriageSkill":       {"label": "Job Triage",        "eval": None},
    "InfoGatherSkill":      {"label": "Info Gather",       "eval": None},
}


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass
class FileInfo:
    path: str                       # relative to project root, posix style
    category: str
    docstring: str
    classes: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    lines: int = 0


@dataclass
class SkillSummary:
    name: str                       # class name
    label: str                      # friendly display name
    description: str
    cli_commands: list[str] = field(default_factory=list)
    integrations_used: list[str] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)
    eval_suite: str | None = None
    eval_cases: int = 0


@dataclass
class ProjectStructure:
    files: list[FileInfo] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    integrations: list[str] = field(default_factory=list)
    eval_suites: list[dict] = field(default_factory=list)   # {name, cases}
    dependencies: list[str] = field(default_factory=list)
    config_vars: list[str] = field(default_factory=list)
    webhook_mappings: list[dict] = field(default_factory=list)
    version: str = "0.0.0"
    total_files: int = 0
    total_lines: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RuntimeState:
    kubectl_connected: bool = False
    cluster_name: str = "—"
    aws_configured: bool = False
    aws_profile: str = "—"
    slack_configured: bool = False
    anthropic_configured: bool = False
    gmail_configured: bool = False
    daemon_running: bool = False
    webhook_running: bool = False
    webhook_port: int = 0
    memory_records: int = 0
    embeddings_count: int = 0
    pending_deploys: int = 0
    total_deploys: int = 0
    last_activity: str = "—"

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _category_for(rel_path: Path) -> str:
    parts = rel_path.parts
    # parts like ("agent", "skills", "k8s.py") when relative to src
    if len(parts) >= 2 and parts[0] == "agent":
        seg = parts[1]
        if seg.endswith(".py"):
            return "Interface"          # cli.py, config.py, __init__.py at agent root
        return _CATEGORY_BY_SEGMENT.get(seg, "Other")
    return "Other"


def _parse_python(path: Path) -> tuple[str, list[str], list[str], int]:
    """Return (docstring, top-level classes, top-level functions, line-count).

    Uses ast for correctness; falls back to line scanning on syntax errors.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return "", [], [], 0

    line_count = text.count("\n") + 1 if text else 0

    try:
        tree = ast.parse(text)
        doc = (ast.get_docstring(tree) or "").strip()
        classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
        funcs = [
            n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        return doc, classes, funcs, line_count
    except SyntaxError:
        # Fallback: crude line scan (matches the "lines starting with" spec)
        classes, funcs = [], []
        for line in text.splitlines():
            if line.startswith("class "):
                classes.append(line[6:].split("(")[0].split(":")[0].strip())
            elif line.startswith("def "):
                funcs.append(line[4:].split("(")[0].strip())
        return "", classes, funcs, line_count


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _sqlite_count(db_path: Path, table: str) -> int:
    if not db_path.exists():
        return 0
    try:
        con = sqlite3.connect(str(db_path))
        try:
            row = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            return int(row[0]) if row else 0
        finally:
            con.close()
    except Exception:
        return 0


def _time_ago(iso_str: str) -> str:
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        secs = int(delta.total_seconds())
        if secs < 0:
            return "just now"
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60} min ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except Exception:
        return iso_str[:19].replace("T", " ")


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------

class AboutSkill(BaseSkill):
    @property
    def name(self) -> str:
        return "about"

    @property
    def description(self) -> str:
        return "Scan the live project and explain its full architecture, every file, and how it all connects."

    def execute(self, input_data: dict) -> dict:
        structure = self.scan_project_structure()
        state = self.scan_runtime_state()
        result = {
            "structure": structure.to_dict(),
            "runtime": state.to_dict(),
            "skills": [asdict(s) for s in self.get_skills_summary(structure)],
        }
        if input_data.get("with_explanation"):
            result["explanation"] = self.generate_architecture_explanation(structure, state)
        return result

    # ------------------------------------------------------------------
    # 1. Filesystem scan
    # ------------------------------------------------------------------

    def scan_project_structure(self) -> ProjectStructure:
        files: list[FileInfo] = []
        skills: list[str] = []
        integrations: list[str] = []
        total_lines = 0

        for py in sorted(_AGENT_DIR.rglob("*.py")):
            if any(part in _SKIP_DIR_PARTS for part in py.parts):
                continue
            rel_to_src = py.relative_to(_SRC_DIR)
            rel_to_root = py.relative_to(_PROJECT_ROOT)
            category = _category_for(rel_to_src)
            doc, classes, funcs, lines = _parse_python(py)
            total_lines += lines
            files.append(FileInfo(
                path=rel_to_root.as_posix(),
                category=category,
                docstring=doc,
                classes=classes,
                functions=funcs,
                lines=lines,
            ))
            if category == "Skills":
                skills.extend(
                    c for c in classes
                    if c.endswith("Skill") and c != "BaseSkill"
                )
            if category == "Integrations" and py.stem != "__init__":
                integrations.append(py.stem)

        eval_suites = self._scan_eval_suites()
        version, dependencies = self._scan_pyproject()
        config_vars = self._scan_env_example()
        webhook_mappings = self._scan_webhook_mappings()

        return ProjectStructure(
            files=files,
            skills=sorted(set(skills)),
            integrations=sorted(set(integrations)),
            eval_suites=eval_suites,
            dependencies=dependencies,
            config_vars=config_vars,
            webhook_mappings=webhook_mappings,
            version=version,
            total_files=len(files),
            total_lines=total_lines,
        )

    def _scan_eval_suites(self) -> list[dict]:
        suites: list[dict] = []
        if not _EVALS_DIR.is_dir():
            return suites
        for d in sorted(_EVALS_DIR.iterdir()):
            if not d.is_dir() or d.name in _SKIP_DIR_PARTS:
                continue
            has_runner = (d / "runner.py").exists()
            cases_file = d / "cases.jsonl"
            if not has_runner and not cases_file.exists():
                continue
            cases = 0
            if cases_file.exists():
                try:
                    cases = sum(
                        1 for line in cases_file.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    )
                except Exception:
                    cases = 0
            suites.append({"name": d.name, "cases": cases})
        return suites

    def _scan_pyproject(self) -> tuple[str, list[str]]:
        path = _PROJECT_ROOT / "pyproject.toml"
        if not path.exists():
            return "0.0.0", []
        try:
            import tomllib
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            proj = data.get("project", {})
            version = proj.get("version", "0.0.0")
            deps = proj.get("dependencies", []) or []
            # strip version specifiers → bare package names
            names = []
            for d in deps:
                name = d.split(">=")[0].split("==")[0].split("<")[0].split("[")[0].strip()
                if name:
                    names.append(name)
            return version, names
        except Exception:
            return "0.0.0", []

    def _scan_env_example(self) -> list[str]:
        path = _PROJECT_ROOT / ".env.example"
        if not path.exists():
            return []
        variables: list[str] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key = line.split("=", 1)[0].strip()
                if key and key.replace("_", "").isalnum():
                    variables.append(key)
        except Exception:
            return []
        # de-dupe, keep order
        seen, out = set(), []
        for v in variables:
            if v not in seen:
                seen.add(v)
                out.append(v)
        return out

    def _scan_webhook_mappings(self) -> list[dict]:
        path = _DATA_DIR / "webhook_mappings.yaml"
        if not path.exists():
            return []
        try:
            import yaml
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            mappings = data.get("mappings", []) or []
            return [m for m in mappings if isinstance(m, dict)]
        except Exception:
            return []

    # ------------------------------------------------------------------
    # 2. Runtime state
    # ------------------------------------------------------------------

    def scan_runtime_state(self) -> RuntimeState:
        st = RuntimeState()

        # ── kubectl / cluster ──────────────────────────────────────────
        try:
            from agent.integrations import kubectl
            if kubectl.is_cluster_available():
                st.kubectl_connected = True
                try:
                    st.cluster_name = kubectl._get_cluster_name() or "—"
                except Exception:
                    st.cluster_name = "—"
        except Exception:
            pass

        # ── cloud / integration config ─────────────────────────────────
        st.aws_profile = os.environ.get("AWS_PROFILE", "") or "—"
        st.aws_configured = bool(os.environ.get("AWS_PROFILE"))
        st.slack_configured = bool(getattr(settings, "slack_webhook_url", ""))
        st.anthropic_configured = bool(getattr(settings, "anthropic_api_key", ""))
        st.gmail_configured = (_DATA_DIR / "gmail_token.json").exists()

        # ── daemon ─────────────────────────────────────────────────────
        st.daemon_running = self._pid_alive(_DATA_DIR / "daemon.pid")

        # ── webhook listener (foreground uvicorn → detect by port) ──────
        st.webhook_port = int(getattr(settings, "webhook_port", 0) or 0)
        st.webhook_running = self._port_listening(st.webhook_port)

        # ── memory ─────────────────────────────────────────────────────
        db_path = Path(getattr(settings, "db_path", "data/memory.db"))
        if not db_path.is_absolute():
            db_path = _PROJECT_ROOT / db_path
        st.memory_records = _sqlite_count(db_path, "memories")
        st.last_activity = self._last_activity(db_path)
        st.embeddings_count = self._embeddings_count()

        # ── deploys ────────────────────────────────────────────────────
        try:
            from agent.integrations import deploy_db
            st.pending_deploys = len(deploy_db.list_pending())
        except Exception:
            st.pending_deploys = 0
        st.total_deploys = _sqlite_count(_DATA_DIR / "deploy.db", "reports")

        return st

    def _pid_alive(self, pid_file: Path) -> bool:
        if not pid_file.exists():
            return False
        try:
            pid = int(pid_file.read_text().strip())
        except Exception:
            return False
        try:
            import psutil
            return psutil.pid_exists(pid)
        except ImportError:
            # POSIX fallback: signal 0 probes existence without killing
            try:
                os.kill(pid, 0)
                return True
            except (OSError, ProcessLookupError):
                return False
            except Exception:
                return False

    def _port_listening(self, port: int) -> bool:
        if not port:
            return False
        try:
            import psutil
            for conn in psutil.net_connections(kind="inet"):
                if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port:
                    return True
            return False
        except Exception:
            # Fallback: try to bind; if it fails, something holds the port
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind(("127.0.0.1", port))
                return False   # bind succeeded → nothing listening
            except OSError:
                return True    # in use → assume listener
            finally:
                s.close()

    def _last_activity(self, db_path: Path) -> str:
        if not db_path.exists():
            return "—"
        try:
            con = sqlite3.connect(str(db_path))
            try:
                row = con.execute(
                    "SELECT created_at FROM memories ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
                return _time_ago(row[0]) if row and row[0] else "—"
            finally:
                con.close()
        except Exception:
            return "—"

    def _embeddings_count(self) -> int:
        try:
            from agent.memory import embeddings
            col = embeddings._get_collection()
            return col.count() if col is not None else 0
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # 3. Skills summary
    # ------------------------------------------------------------------

    def get_skills_summary(self, structure: ProjectStructure | None = None) -> list[SkillSummary]:
        structure = structure or self.scan_project_structure()
        eval_cases = {s["name"]: s["cases"] for s in structure.eval_suites}

        summaries: list[SkillSummary] = []
        for f in structure.files:
            if f.category != "Skills":
                continue
            skill_classes = [c for c in f.classes if c.endswith("Skill") and c != "BaseSkill"]
            if not skill_classes:
                continue
            integrations_used = self._integrations_in_file(f.path)
            class_methods = self._class_methods(f.path)
            for cls in skill_classes:
                disp = _SKILL_DISPLAY.get(cls, {})
                label = disp.get("label") or _derive_label(cls)
                eval_name = disp.get("eval")
                summaries.append(SkillSummary(
                    name=cls,
                    label=label,
                    description=_first_line(f.docstring) or f"{label} skill.",
                    cli_commands=[],   # populated by the CLI layer via introspection
                    integrations_used=integrations_used,
                    methods=class_methods.get(cls, []),
                    eval_suite=eval_name,
                    eval_cases=eval_cases.get(eval_name or "", 0),
                ))
        summaries.sort(key=lambda s: s.label.lower())
        return summaries

    def _integrations_in_file(self, rel_path: str) -> list[str]:
        path = _PROJECT_ROOT / rel_path
        used: set[str] = set()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return []
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("from agent.integrations."):
                mod = s.split("from agent.integrations.", 1)[1].split(" ", 1)[0].split(".")[0]
                if mod:
                    used.add(mod)
            elif s == "import boto3" or s.startswith("import boto3 ") or s.startswith("from boto3"):
                used.add("boto3")
        return sorted(used)

    def _class_methods(self, rel_path: str) -> dict[str, list[str]]:
        """Map each class in a file to its public method names (via ast)."""
        path = _PROJECT_ROOT / rel_path
        result: dict[str, list[str]] = {}
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            return result
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                methods = [
                    m.name for m in node.body
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and not m.name.startswith("_")
                ]
                result[node.name] = methods
        return result

    # ------------------------------------------------------------------
    # 4. Claude architecture explanation
    # ------------------------------------------------------------------

    def generate_architecture_explanation(
        self, structure: ProjectStructure, state: RuntimeState
    ) -> str:
        from agent.core import llm

        scan_text = self._render_scan_for_llm(structure, state)
        system = _ARCHITECT_SYSTEM_PROMPT
        try:
            resp = run_sync(llm.chat(
                messages=[{"role": "user", "content": scan_text}],
                system=system,
                max_tokens=4096,
            ))
            return resp.content
        except Exception as exc:
            log.warning("about.explanation_failed", error=str(exc)[:300])
            return (
                f"[Claude analysis unavailable: {exc}]\n\n"
                "Run with a valid ANTHROPIC_API_KEY to generate the full "
                "architecture explanation, or use --no-claude to skip it."
            )

    def _render_scan_for_llm(self, structure: ProjectStructure, state: RuntimeState) -> str:
        lines: list[str] = []
        lines.append(f"# ATLASOS PROJECT SCAN (version {structure.version})")
        lines.append(
            f"\nScale: {structure.total_files} Python files, "
            f"{structure.total_lines:,} lines, "
            f"{len(structure.skills)} skills, "
            f"{len(structure.integrations)} integrations, "
            f"{len(structure.eval_suites)} eval suites."
        )

        # Files grouped by category
        by_cat: dict[str, list[FileInfo]] = {}
        for f in structure.files:
            by_cat.setdefault(f.category, []).append(f)
        lines.append("\n## FILES BY LAYER")
        for cat in sorted(by_cat):
            group = by_cat[cat]
            cat_lines = sum(x.lines for x in group)
            lines.append(f"\n### {cat} ({len(group)} files, {cat_lines:,} lines)")
            for f in group:
                summary = _first_line(f.docstring)
                cls = ", ".join(f.classes[:6])
                lines.append(
                    f"- {f.path} ({f.lines} lines)"
                    + (f" — {summary}" if summary else "")
                    + (f" [classes: {cls}]" if cls else "")
                )

        lines.append("\n## SKILLS")
        lines.append(", ".join(structure.skills) or "none")
        lines.append("\n## INTEGRATIONS")
        lines.append(", ".join(structure.integrations) or "none")
        lines.append("\n## EVAL SUITES")
        lines.append(
            ", ".join(f"{s['name']} ({s['cases']} cases)" for s in structure.eval_suites)
            or "none"
        )
        lines.append("\n## DEPENDENCIES")
        lines.append(", ".join(structure.dependencies) or "none")
        lines.append(f"\n## CONFIG VARIABLES ({len(structure.config_vars)})")
        lines.append(", ".join(structure.config_vars) or "none")

        lines.append("\n## LIVE RUNTIME STATE")
        lines.append(f"- kubectl connected: {state.kubectl_connected} (cluster: {state.cluster_name})")
        lines.append(f"- AWS configured: {state.aws_configured} (profile: {state.aws_profile})")
        lines.append(f"- Slack: {state.slack_configured}, Gmail: {state.gmail_configured}, "
                     f"Anthropic key: {state.anthropic_configured}")
        lines.append(f"- daemon running: {state.daemon_running}, webhook listening: {state.webhook_running}")
        lines.append(f"- memory records: {state.memory_records}, embeddings: {state.embeddings_count}")
        lines.append(f"- deploys: {state.total_deploys} total, {state.pending_deploys} pending")
        lines.append(f"- last activity: {state.last_activity}")

        return "\n".join(lines)


def _derive_label(class_name: str) -> str:
    """Turn 'ResourceMonitorSkill' → 'Resource Monitor'."""
    name = class_name[:-5] if class_name.endswith("Skill") else class_name
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0 and not name[i - 1].isupper():
            out.append(" ")
        out.append(ch)
    return "".join(out).strip() or class_name


# ---------------------------------------------------------------------------
# Claude system prompt — rewritten for the REAL execution model + gap report
# ---------------------------------------------------------------------------

_ARCHITECT_SYSTEM_PROMPT = """You are a senior software architect explaining a \
production AI platform (AtlasOS) to another senior engineer.

You have been given the ACTUAL scanned structure of the project — every file, \
its docstring and classes, the eval suites, dependencies, config, and current \
runtime state. Use the REAL file and class names from the scan. Be specific and \
precise. No fluff, no invented components.

IMPORTANT — the real execution model (do not describe anything else):
AtlasOS does NOT use a Claude tool-use / function-calling loop, an orchestrator \
module, or MCP. The actual pattern is single-shot per skill:
  1. cli.py routes the command straight to a skill class in src/agent/skills/.
  2. The skill gathers live data in Python (integrations/kubectl.py via the
     `kubectl` binary, boto3 for AWS, Gmail API, etc.).
  3. It retrieves relevant past memory (memory/retrieval.py → ChromaDB).
  4. It builds ONE prompt and makes ONE Claude call through core/llm.py
     (the only module that talks to Claude), often with json_mode.
  5. It parses the response, saves a memory (memory/store.py + embeddings.py),
     and renders output.
Claude reasons over a summary the Python code assembled; it does not call tools
back into the code.

Produce a comprehensive architecture explanation with these sections:

1. PROJECT PHILOSOPHY — the problem it solves; the observe → diagnose → heal →
   optimize loop.
2. LAYERED ARCHITECTURE — interface (CLI + dashboard), skill layer, execution
   (single Claude call via llm.py), memory (SQLite + ChromaDB), integrations,
   observability. Explain why each layer exists using real module names.
3. DATA FLOW — walk, file by file in order, what happens when: (a) a user runs
   `agent k8s scan`; (b) a GitHub webhook arrives (integrations/webhook.py);
   (c) the monitor daemon (core/daemon.py) detects an OOMKill.
4. THE MEMORY SYSTEM — how SQLite (store.py, source of truth) and ChromaDB
   (embeddings.py, vectors) work together; how retrieval.py injects RAG context;
   why this compounds over time.
5. THE EXECUTION MODEL — exactly how a skill makes its single Claude call through
   core/llm.py: timing, cost tracking, JSON mode, rate-limit retry. Name the
   files. Do NOT describe a tool-use loop — it does not exist here.
6. EVERY SKILL EXPLAINED — for each skill in the scan: problem solved, which
   integrations it uses, what it writes to memory, and an example invocation.
7. THE INTEGRATION LAYER — how kubectl, boto3/AWS, Slack, Gmail, and the webhook
   listener are wired; how auth/config flows from .env via config.py. Note that
   there is no MCP layer.
8. THE DEPLOYMENT PIPELINE — GitHub push → webhook.py → pending deploy (deploy_db)
   → approval gate → rollout → auto-rollback. Files at each step.
9. SECURITY MODEL — approval gates, production-namespace confirmation, webhook
   HMAC verification, what the agent will and will not do autonomously.
10. OBSERVABILITY DESIGN — structlog logging, per-call cost tracking (costs.py),
    audit trail in SQLite, what the dashboard surfaces live.
11. WHY THESE TECHNOLOGY CHOICES — real reasons: SQLite over Postgres, raw
    Anthropic SDK over LangChain, Typer over argparse, FastAPI over Flask,
    ChromaDB over Pinecone.
12. WHAT MAKES THIS PRODUCTION-GRADE — eval suites (give the real total case
    count), alert dedup, auto-rollback, false-positive filtering, human-in-the-
    loop gates, cost tracking, audit logging.
13. DESIGN GAPS / ROADMAP — briefly and honestly: the codebase currently uses
    single-shot Claude calls, so a true agentic tool-use loop, a dedicated
    orchestrator module, and an MCP integration are NOT yet implemented. Note
    these as natural next steps, distinguishing intended design from what the
    scan actually shows.

Format with clear markdown headings and subsections. Write for a senior engineer
in an architecture review."""
