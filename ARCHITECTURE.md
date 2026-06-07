# Architecture — AI Agentic OS

## Overview

AI Agentic OS is a **command-driven AI operations platform**. Every `agent <command>` call follows the same path: CLI → Skill → Integrations (data) + LLM (reasoning) → Memory (history) → Rich output.

```
┌─────────────────────────────────────────────────────────────────┐
│                        USER TERMINAL                            │
│                   agent k8s full-scan --fix                     │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                        CLI  (cli.py)                            │
│  Typer app · 30+ commands · Rich panels/tables · cost footer    │
│  Sub-apps: k8s · aws · tls · dns · domain · gmail · memory      │
└──────────┬──────────────────────────────────────────────────────┘
           │  calls
           ▼
┌─────────────────────────────────────────────────────────────────┐
│                     SKILLS LAYER                                │
│  skills/full_scan.py   skills/tls.py    skills/gmail.py         │
│  skills/k8s.py         skills/aws.py    skills/domain.py        │
│  skills/ingress.py     skills/dns.py    skills/network.py       │
│  skills/triage.py      skills/setup.py  ...                     │
│                                                                 │
│  Each skill: BaseSkill ABC → execute() → dict result            │
└──────┬────────────────────────────────┬────────────────────────┘
       │ collect data                   │ reason with AI
       ▼                                ▼
┌──────────────────┐          ┌─────────────────────────────────┐
│  INTEGRATIONS    │          │          CORE / LLM             │
│                  │          │                                 │
│  kubectl.py      │          │  core/llm.py                    │
│  collectors.py   │          │  · Only place that calls Claude │
│  aws.py          │          │  · Timed + cost-tracked         │
│  tls_collector   │          │  · Rate-limit retry (3×)        │
│  ingress_coll.   │          │  · Structured logging           │
│  dns_collector   │          │                                 │
│  network_coll.   │          │  core/models.py                 │
│  node_inspector  │          │  · LLMResponse, PodInfo,        │
│  system_coll.    │          │    IngressInfo, TLSSecretInfo…  │
└──────────────────┘          └────────────────┬────────────────┘
                                               │
       ┌───────────────────────────────────────┘
       │ save incident history
       ▼
┌─────────────────────────────────────────────────────────────────┐
│                       MEMORY LAYER                              │
│                                                                 │
│  memory/retrieval.py                                            │
│  · remember() — save to SQLite + embed in ChromaDB + vault .md  │
│  · retrieve_context() — semantic search → Memory objects        │
│                                                                 │
│  memory/store.py          memory/embeddings.py                  │
│  · SQLite via sqlite-utils · ChromaDB PersistentClient          │
│  · CRUD for Memory rows   · Voyage AI voyage-3-lite (512-dim)   │
│                           · Hash fallback (no API key needed)   │
│                                                                 │
│  data/memory.db  (SQLite)    chroma_db/  (vector index)         │
│  data/vault/*.md (human-readable markdown copies)               │
└─────────────────────────────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────────────┐
│                    OBSERVABILITY                                 │
│  observability/logging.py  → structlog JSON / pretty console    │
│  observability/costs.py    → per-call + session token tracking  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Module Map

### `src/agent/cli.py` — Command surface
The single Typer application. Every `agent <x>` command lives here. Handles user I/O: Rich panels, tables, progress spinners, cost footers. Calls skills or integrations directly.

### `src/agent/skills/` — AI-powered operations

| File | What it does |
|---|---|
| `base.py` | `BaseSkill` ABC — `name`, `description`, `execute()` contract |
| `full_scan.py` | Runs all 10 collectors in parallel, sends results to Claude for analysis |
| `k8s.py` | Quick cluster health scan — pods, nodes, immediate issues |
| `tls.py` / `tls_monitor.py` | Certificate expiry checks, renewal recommendations |
| `ingress.py` | Ingress rule audit — TLS, paths, backend health |
| `dns.py` | CoreDNS health, configmap, endpoint validation |
| `network.py` | NetworkPolicy audit, connectivity checks |
| `aws.py` | EC2, EIP, load balancer, security group analysis |
| `domain.py` / `domain_live.py` | DNS resolution + live HTTP connectivity per domain |
| `gmail.py` | Gmail fetch via OAuth |
| `triage.py` | AI email priority/category classification |
| `autohealer.py` | Automated pod restart / resource patch decisions |
| `node_healer.py` | Node-level taint/drain/cordon operations |
| `job_triage.py` | Failed Job / CronJob root-cause analysis |
| `setup.py` | Interactive setup wizard — all integrations |
| `_fix_runner.py` | Shared --fix execution harness |

### `src/agent/integrations/` — Raw data collectors

| File | What it does |
|---|---|
| `kubectl.py` | Core `run_kubectl()` wrapper · typed helpers: `get_pods()`, `get_all_ingresses()`, `get_problematic_pods()` |
| `collectors.py` | 10 parallel `--no-headers` collectors for full-scan: nodes, pods, dns, network, pvcs, jobs, hpa, ingress, rbac, tls |
| `aws.py` | `boto3` wrappers: EC2, EIPs, ALB/NLB, security groups + fix actions |
| `tls_collector.py` | Reads TLS secrets from K8s, parses x509 certs, returns expiry data |
| `ingress_collector.py` | Parses ingress rules into typed `IngressInfo` objects |
| `dns_collector.py` | CoreDNS pod status, endpoint, configmap |
| `network_collector.py` | NetworkPolicy enumeration |
| `node_inspector.py` | Per-node resource usage, conditions, taints |
| `system_collector.py` | Cluster-wide resource summary |

### `src/agent/core/` — LLM client & shared types

| File | What it does |
|---|---|
| `llm.py` | **Only file that calls Claude.** Async `chat()` with retry, cost tracking, structured logging |
| `models.py` | All shared data models: `LLMResponse`, `PodInfo`, `IngressInfo`, `TLSSecretInfo`, `Memory`, `NodeInfo` |
| `parsing.py` | JSON extraction from LLM responses |
| `async_utils.py` | `asyncio.run()` safe wrapper for Windows |
| `context.py` | Request-scoped context (namespace, dry-run flag) |

### `src/agent/memory/` — Persistent incident memory

| File | What it does |
|---|---|
| `retrieval.py` | `remember(content, source, metadata)` · `retrieve_context(query, limit)` — the public API |
| `store.py` | SQLite CRUD via `sqlite-utils` — stores full Memory rows |
| `embeddings.py` | ChromaDB collection + Voyage AI `voyage-3-lite` embeddings (512-dim). Falls back to hash embeddings when no API key |

### `src/agent/observability/`

| File | What it does |
|---|---|
| `logging.py` | `get_logger()` — structlog with pretty console + JSON modes |
| `costs.py` | Per-call token/cost accumulation · `get_session_total()` |

---

## Data Flow — `agent k8s full-scan`

```
agent k8s full-scan
        │
        ▼
cli.py::cmd_full_scan()
        │
        ├─► collectors.collect_all()          ← ThreadPoolExecutor, 3 workers max
        │       ├── collect_nodes()           ← kubectl get nodes --no-headers
        │       ├── collect_pods()            ← kubectl get pods -A --no-headers
        │       ├── collect_dns()             ← 3 kubectl queries (no exec)
        │       ├── collect_network()         ← kubectl get networkpolicies
        │       ├── collect_pvcs()            ← kubectl get pvc -A --no-headers
        │       ├── collect_jobs()            ← kubectl get jobs -A --no-headers
        │       ├── collect_hpa()             ← kubectl get hpa -A --no-headers
        │       ├── collect_ingress()         ← ingress_collector + tls_collector
        │       ├── collect_rbac()            ← jsonpath for cluster-admin bindings
        │       └── collect_tls()            ← x509 cert parse per secret
        │
        ├─► full_scan.FullScanSkill.execute() ← builds structured prompt
        │       └─► core/llm.chat()           ← single Claude call, json_mode=True
        │               └── returns JSON: {issues[], recommendations[], summary}
        │
        ├─► memory/retrieval.remember()       ← saves scan result to SQLite + ChromaDB
        │
        └─► cli.py renders Rich tables + panels + cost footer
```

## Data Flow — `agent memory search "<query>"`

```
query string
    │
    ▼
memory/retrieval.retrieve_context(query, limit=5)
    │
    ├─► embeddings._VoyageEmbeddingFunction(query)   ← voyage-3-lite API call
    │       └── returns 512-dim vector
    │
    ├─► ChromaDB.query(vector, n_results=5)           ← cosine similarity search
    │       └── returns [ids, documents, distances]
    │
    └─► store.get_memories(ids)                       ← SQLite lookup for full rows
            └── returns List[Memory]
```

---

## Key Design Decisions

**`--no-headers` over `-o json`**
All parallel collectors use `kubectl --no-headers` table output instead of `-o json`. On Windows, each `kubectl` subprocess spawns a full Go runtime. Running 10 in parallel with JSON output caused `[WinError 1455]` paging file exhaustion. Table output is ~20× smaller; capped at 3 concurrent workers.

**Single LLM entrypoint**
`core/llm.py` is the only file that calls Claude. This means retry logic, cost tracking, and logging are applied uniformly — no skill can accidentally make an untracked or unretried API call.

**Collectors are pure data**
Nothing in `integrations/collectors.py` calls the LLM. Collectors gather raw facts; skills compose them into prompts. This keeps collectors fast, testable, and reusable across multiple skills.

**Memory = SQLite + ChromaDB + Vault**
Every `remember()` call writes three places: SQLite (queryable structured store), ChromaDB (vector index for semantic search), and a markdown file in `data/vault/` (human-readable, git-trackable if desired). The vault is the escape hatch — if the DBs are lost, the markdown files remain.

**Voyage AI with hash fallback**
Semantic search works without any additional setup (hash fallback). Setting `VOYAGE_API_KEY` upgrades it to true semantic embeddings transparently — same interface, no code changes.

---

## Runtime Requirements

```
Python 3.11+          runtime
uv                    package manager + venv
kubectl               Kubernetes commands (optional)
AWS CLI v2            AWS commands (optional)
```

## Storage layout (gitignored, created at runtime)

```
data/
  memory.db           SQLite — all Memory rows
  vault/              Markdown copies of every memory
    *.md
chroma_db/            ChromaDB vector index
  chroma.sqlite3
  <collection-uuid>/
```
