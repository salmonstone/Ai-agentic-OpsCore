"""
System-level data collectors for the agentic OS context snapshot.

Rules:
  - No Claude calls — data only
  - Every function returns {"summary": str, "data": dict, "ok": bool}
  - Never raises — errors go into the result dict
  - All collectors run in parallel via collect_all_system()
"""
from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from datetime import datetime, timezone
from pathlib import Path

from agent.observability.logging import get_logger

log = get_logger(__name__)

_TIMEOUT = 10  # seconds per collector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(summary: str, data: dict) -> dict:
    return {"ok": True, "summary": summary, "data": data}


def _err(area: str, error: str) -> dict:
    return {"ok": False, "summary": f"{area} collection failed: {error[:80]}", "data": {}}


def _run(cmd: list[str], timeout: int = 5) -> tuple[bool, str]:
    """Run a shell command, return (ok, output/error)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        return False, result.stderr.strip() or result.stdout.strip()
    except FileNotFoundError:
        return False, "not found"
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------

def collect_system() -> dict:
    """OS, CPU, memory, disk."""
    try:
        import psutil

        cpu_count    = psutil.cpu_count(logical=True)
        cpu_phys     = psutil.cpu_count(logical=False)
        cpu_pct      = psutil.cpu_percent(interval=0.5)
        mem          = psutil.virtual_memory()
        disk         = psutil.disk_usage("/") if platform.system() != "Windows" else psutil.disk_usage("C:\\")

        mem_total_gb = round(mem.total / 1024**3, 1)
        mem_used_gb  = round(mem.used  / 1024**3, 1)
        mem_pct      = mem.percent
        disk_total   = round(disk.total / 1024**3, 1)
        disk_free    = round(disk.free  / 1024**3, 1)
        disk_pct     = disk.percent

        summary = (
            f"OS: {platform.system()} {platform.release()} | "
            f"Python {platform.python_version()} | "
            f"CPU: {cpu_pct:.0f}% ({cpu_phys}p/{cpu_count}t) | "
            f"RAM: {mem_used_gb}/{mem_total_gb}GB ({mem_pct:.0f}%) | "
            f"Disk: {disk_free}/{disk_total}GB free ({disk_pct:.0f}% used)"
        )

        data = {
            "os":            platform.system(),
            "os_release":    platform.release(),
            "os_version":    platform.version()[:80],
            "hostname":      socket.gethostname(),
            "python":        platform.python_version(),
            "arch":          platform.machine(),
            "cpu_logical":   cpu_count,
            "cpu_physical":  cpu_phys,
            "cpu_pct":       cpu_pct,
            "mem_total_gb":  mem_total_gb,
            "mem_used_gb":   mem_used_gb,
            "mem_pct":       mem_pct,
            "disk_total_gb": disk_total,
            "disk_free_gb":  disk_free,
            "disk_pct":      disk_pct,
        }
        return _ok(summary, data)

    except ImportError:
        # psutil not installed — fall back to platform only
        summary = (
            f"OS: {platform.system()} {platform.release()} | "
            f"Python {platform.python_version()} | "
            f"host: {socket.gethostname()} | (install psutil for CPU/RAM data)"
        )
        return _ok(summary, {
            "os":         platform.system(),
            "os_release": platform.release(),
            "hostname":   socket.gethostname(),
            "python":     platform.python_version(),
            "arch":       platform.machine(),
        })
    except Exception as exc:
        return _err("system", str(exc))


def collect_tools() -> dict:
    """Which CLI tools are installed and their versions."""
    TOOLS = {
        "kubectl":   ["kubectl", "version", "--client", "--short"],
        "helm":      ["helm", "version", "--short"],
        "aws":       ["aws", "--version"],
        "docker":    ["docker", "--version"],
        "git":       ["git", "--version"],
        "terraform": ["terraform", "--version"],
        "node":      ["node", "--version"],
        "npm":       ["npm", "--version"],
        "python":    ["python", "--version"],
        "pip":       ["pip", "--version"],
        "minikube":  ["minikube", "version", "--short"],
        "kind":      ["kind", "--version"],
        "skaffold":  ["skaffold", "version"],
        "gcloud":    ["gcloud", "--version"],
        "az":        ["az", "--version"],
    }

    present  = {}
    missing  = []

    for tool, cmd in TOOLS.items():
        if shutil.which(cmd[0]) is None:
            missing.append(tool)
            continue
        ok, out = _run(cmd)
        version = out.splitlines()[0][:60] if out else "?"
        present[tool] = version

    summary = (
        f"{len(present)} tool(s) available: {', '.join(present)} | "
        f"{len(missing)} missing: {', '.join(missing) or 'none'}"
    )
    return _ok(summary, {"present": present, "missing": missing})


def collect_k8s_context() -> dict:
    """Current kubectl context, cluster, and namespace — no API calls."""
    ok_ctx, current_ctx = _run(["kubectl", "config", "current-context"])
    if not ok_ctx:
        return _ok("No active kubectl context.", {"available": False})

    ok_ns, namespace = _run(["kubectl", "config", "view", "--minify",
                              "-o", "jsonpath={.contexts[0].context.namespace}"])
    namespace = namespace or "default"

    ok_cs, cluster = _run(["kubectl", "config", "view", "--minify",
                            "-o", "jsonpath={.clusters[0].cluster.server}"])

    # List all contexts
    ok_list, ctx_raw = _run(["kubectl", "config", "get-contexts", "--no-headers"])
    ctx_lines = ctx_raw.strip().splitlines() if ok_list else []
    ctx_names = []
    for line in ctx_lines:
        parts = line.split()
        if parts:
            name = parts[1] if parts[0] == "*" else parts[0]
            ctx_names.append(name)

    summary = (
        f"Context: {current_ctx} | "
        f"Namespace: {namespace} | "
        f"Server: {cluster[:60] if ok_cs else '?'} | "
        f"{len(ctx_names)} context(s) configured"
    )
    return _ok(summary, {
        "available":       True,
        "current_context": current_ctx,
        "namespace":       namespace,
        "server":          cluster if ok_cs else "",
        "all_contexts":    ctx_names,
    })


def collect_aws_context() -> dict:
    """AWS CLI profile, region, and caller identity (if configured)."""
    if shutil.which("aws") is None:
        return _ok("AWS CLI not installed.", {"available": False})

    profile = os.environ.get("AWS_PROFILE") or os.environ.get("AWS_DEFAULT_PROFILE") or "default"
    region  = os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION") or ""

    if not region:
        ok_r, region_out = _run(["aws", "configure", "get", "region"])
        region = region_out.strip() if ok_r else "?"

    # Try to get caller identity — slow if not configured
    ok_id, identity = _run(["aws", "sts", "get-caller-identity", "--output", "text",
                              "--query", "Account"], timeout=6)
    account = identity.strip() if ok_id else "not authenticated"

    summary = f"AWS profile: {profile} | region: {region} | account: {account}"
    return _ok(summary, {
        "available": True,
        "profile":   profile,
        "region":    region,
        "account":   account,
        "authenticated": ok_id,
    })


def collect_gmail_context() -> dict:
    """Check whether Gmail credentials and token files are present."""
    data_dir    = Path("data")
    creds_path  = data_dir / "gmail_credentials.json"
    token_path  = data_dir / "gmail_token.json"

    creds_ok = creds_path.exists()
    token_ok = token_path.exists()

    if not creds_ok and not token_ok:
        summary = "Gmail: not configured (no credentials or token)"
    elif creds_ok and not token_ok:
        summary = "Gmail: credentials present, not yet authenticated (run: agent gmail auth)"
    elif creds_ok and token_ok:
        summary = "Gmail: fully configured and authenticated"
    else:
        summary = "Gmail: token present but credentials missing (unusual)"

    return _ok(summary, {
        "credentials_present": creds_ok,
        "token_present":       token_ok,
        "ready":               creds_ok and token_ok,
    })


def collect_env_context() -> dict:
    """Check which key env vars are set (names only — no values for secrets)."""
    WATCHED = {
        "ANTHROPIC_API_KEY":    "Claude API key",
        "AWS_ACCESS_KEY_ID":    "AWS access key",
        "AWS_SECRET_ACCESS_KEY":"AWS secret",
        "AWS_PROFILE":          "AWS profile name",
        "AWS_DEFAULT_REGION":   "AWS region",
        "AWS_REGION":           "AWS region (alt)",
        "KUBECONFIG":           "Custom kubeconfig path",
        "GOOGLE_CLOUD_PROJECT": "GCP project",
        "GITHUB_TOKEN":         "GitHub token",
        "DATABASE_URL":         "DB connection URL",
        "REDIS_URL":            "Redis URL",
        "OPENAI_API_KEY":       "OpenAI API key",
    }

    set_vars    = {}
    missing     = []

    for var, desc in WATCHED.items():
        val = os.environ.get(var)
        if val:
            # Mask secrets — only show that they're set
            if "KEY" in var or "SECRET" in var or "TOKEN" in var or "URL" in var:
                set_vars[var] = f"[set] ({desc})"
            else:
                set_vars[var] = f"{val[:40]} ({desc})"
        else:
            missing.append(var)

    summary = f"{len(set_vars)} env var(s) set | {len(missing)} missing"
    return _ok(summary, {"set": set_vars, "missing": missing})


def collect_git_context() -> dict:
    """Find git repos in the current and parent directories."""
    repos = []

    # Check current dir and up to 2 parents
    search_roots = [Path.cwd()]
    p = Path.cwd().parent
    for _ in range(2):
        search_roots.append(p)
        p = p.parent

    seen = set()
    for root in search_roots:
        git_dir = root / ".git"
        if git_dir.exists() and str(root) not in seen:
            seen.add(str(root))
            ok_branch, branch = _run(["git", "-C", str(root), "branch", "--show-current"])
            ok_hash,   commit = _run(["git", "-C", str(root), "log", "-1", "--format=%h %s"])
            ok_status, status = _run(["git", "-C", str(root), "status", "--porcelain"])
            dirty = bool(status.strip()) if ok_status else False
            repos.append({
                "path":   str(root),
                "branch": branch if ok_branch else "?",
                "commit": commit[:60] if ok_hash else "?",
                "dirty":  dirty,
            })

    summary = f"{len(repos)} git repo(s) in scope"
    if repos:
        parts = []
        for r in repos:
            d = " (dirty)" if r["dirty"] else ""
            parts.append(f"{Path(r['path']).name}@{r['branch']}{d}")
        summary += ": " + ", ".join(parts)

    return _ok(summary, {"repos": repos})


def collect_local_network() -> dict:
    """Local hostname and IP addresses."""
    try:
        hostname = socket.gethostname()
        ips = []
        try:
            info = socket.getaddrinfo(hostname, None)
            seen = set()
            for item in info:
                ip = item[4][0]
                if ip not in seen and not ip.startswith("::"):
                    seen.add(ip)
                    ips.append(ip)
        except Exception:
            pass

        # Also try to get the main outbound IP
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            outbound_ip = s.getsockname()[0]
            s.close()
            if outbound_ip not in ips:
                ips.insert(0, outbound_ip)
        except Exception:
            pass

        summary = f"Host: {hostname} | IPs: {', '.join(ips[:4]) or 'none detected'}"
        return _ok(summary, {"hostname": hostname, "ips": ips[:6]})
    except Exception as exc:
        return _err("network", str(exc))


def collect_docker_context() -> dict:
    """Docker daemon status and running containers."""
    if shutil.which("docker") is None:
        return _ok("Docker not installed.", {"available": False})

    ok_info, info = _run(["docker", "info", "--format", "{{.ServerVersion}}"])
    if not ok_info:
        return _ok("Docker installed but daemon not running.", {
            "available": True, "daemon_running": False,
        })

    ok_ps, ps_raw = _run(["docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"])
    containers = []
    if ok_ps and ps_raw:
        for line in ps_raw.strip().splitlines():
            parts = line.split("\t")
            containers.append({
                "name":   parts[0] if len(parts) > 0 else "?",
                "status": parts[1] if len(parts) > 1 else "?",
                "image":  parts[2] if len(parts) > 2 else "?",
            })

    summary = (
        f"Docker {info} running | "
        f"{len(containers)} container(s) active"
    )
    return _ok(summary, {
        "available":       True,
        "daemon_running":  True,
        "server_version":  info,
        "containers":      containers,
    })


def collect_python_packages() -> dict:
    """Key installed Python packages and their versions."""
    KEY_PACKAGES = [
        "anthropic", "pydantic", "typer", "rich", "chromadb",
        "boto3", "google-api-python-client", "psutil",
        "kubernetes", "httpx", "fastapi", "uvicorn",
    ]
    installed = {}
    missing   = []

    try:
        import importlib.metadata as meta
        for pkg in KEY_PACKAGES:
            try:
                version = meta.version(pkg)
                installed[pkg] = version
            except meta.PackageNotFoundError:
                missing.append(pkg)
    except Exception as exc:
        return _err("python_packages", str(exc))

    summary = (
        f"{len(installed)}/{len(KEY_PACKAGES)} key packages installed | "
        f"missing: {', '.join(missing) or 'none'}"
    )
    return _ok(summary, {"installed": installed, "missing": missing})


# ---------------------------------------------------------------------------
# Parallel orchestrator
# ---------------------------------------------------------------------------

def collect_tls_context() -> dict:
    """Quick TLS summary — cert-manager health, expired/expiring certs, TLS secret count."""
    if shutil.which("kubectl") is None:
        return _ok("kubectl not installed — TLS check skipped.", {"available": False})

    # cert-manager pods
    cm_ok = False
    cm_ns = None
    for ns in ("cert-manager", "kube-system", "default"):
        ok, out = _run(["kubectl", "get", "pods", "-n", ns, "-l", "app=cert-manager", "--no-headers"])
        if ok and out.strip():
            lines = out.strip().splitlines()
            cm_ok = all("Running" in l for l in lines)
            cm_ns = ns
            break

    # cert-manager Certificates
    total_certs = 0
    not_ready   = 0
    ok_certs, certs_out = _run(["kubectl", "get", "certificates", "-A", "--no-headers"])
    if ok_certs and certs_out.strip():
        for line in certs_out.strip().splitlines():
            total_certs += 1
            if "True" not in line:
                not_ready += 1

    # TLS secrets
    ok_sec, sec_out = _run(["kubectl", "get", "secrets", "-A",
                             "--field-selector=type=kubernetes.io/tls", "--no-headers"])
    tls_secrets = len(sec_out.strip().splitlines()) if ok_sec and sec_out.strip() else 0

    # ACME challenges in flight
    ok_ch, ch_out = _run(["kubectl", "get", "challenges", "-A", "--no-headers"])
    challenges = len(ch_out.strip().splitlines()) if ok_ch and ch_out.strip() else 0

    parts = []
    if cm_ns:
        parts.append(f"cert-manager: {'OK' if cm_ok else 'DEGRADED'} ({cm_ns})")
    else:
        parts.append("cert-manager: NOT INSTALLED")
    parts.append(f"{total_certs} Certificate(s)"
                 + (f", {not_ready} not ready" if not_ready else ""))
    parts.append(f"{tls_secrets} TLS secret(s)")
    if challenges:
        parts.append(f"{challenges} ACME challenge(s) active")

    return _ok(" | ".join(parts), {
        "available":     True,
        "cm_ok":         cm_ok,
        "cm_ns":         cm_ns,
        "total_certs":   total_certs,
        "not_ready":     not_ready,
        "tls_secrets":   tls_secrets,
        "challenges":    challenges,
    })


def collect_ingress_context() -> dict:
    """Quick nginx ingress summary — controller status, external IPs, ingress count."""
    if shutil.which("kubectl") is None:
        return _ok("kubectl not installed — ingress check skipped.", {"available": False})

    # Controller pods
    ctrl_ok    = False
    ctrl_ns    = None
    ctrl_pods  = 0
    for ns in ("ingress-nginx", "kube-system", "default"):
        for sel in ("app.kubernetes.io/name=ingress-nginx", "app=ingress-nginx", "app=nginx-ingress"):
            ok, out = _run(["kubectl", "get", "pods", "-n", ns, "-l", sel, "--no-headers"])
            if ok and out.strip():
                lines      = out.strip().splitlines()
                ctrl_pods  = len(lines)
                ctrl_ns    = ns
                ctrl_ok    = all("Running" in l for l in lines)
                break
        if ctrl_pods:
            break

    # LoadBalancer services — external IPs
    ok_svc, svc_raw = _run(["kubectl", "get", "services", "-A",
                             "-o", "custom-columns="
                             "NS:.metadata.namespace,"
                             "NAME:.metadata.name,"
                             "TYPE:.spec.type,"
                             "EXTERNAL:.status.loadBalancer.ingress[0].ip,"
                             "HOST:.status.loadBalancer.ingress[0].hostname",
                             "--no-headers"])
    lb_pending  = []
    lb_assigned = []
    if ok_svc:
        for line in svc_raw.strip().splitlines():
            parts = line.split()
            if len(parts) < 3 or parts[2] != "LoadBalancer":
                continue
            ip   = parts[3] if len(parts) > 3 else "<none>"
            host = parts[4] if len(parts) > 4 else "<none>"
            addr = ip if ip not in ("<none>", "") else host
            addr = addr if addr not in ("<none>", "") else ""
            key  = f"{parts[0]}/{parts[1]}"
            if addr:
                lb_assigned.append(f"{key} → {addr}")
            else:
                lb_pending.append(key)

    # Ingress resources
    ok_ing, ing_raw = _run(["kubectl", "get", "ingress", "-A", "--no-headers"])
    ing_count   = len(ing_raw.strip().splitlines()) if ok_ing and ing_raw.strip() else 0
    no_address  = 0
    if ok_ing and ing_raw.strip():
        for line in ing_raw.strip().splitlines():
            parts = line.split()
            # column 4 is ADDRESS in kubectl get ingress output
            if len(parts) < 5 or not parts[4] or parts[4] == "<none>":
                no_address += 1

    # Build summary
    if not ctrl_pods:
        ctrl_label = "NOT INSTALLED"
    elif ctrl_ok:
        ctrl_label = f"OK ({ctrl_pods} pod(s) in {ctrl_ns})"
    else:
        ctrl_label = f"DEGRADED ({ctrl_pods} pod(s) in {ctrl_ns})"

    parts = [f"nginx controller: {ctrl_label}"]
    if lb_assigned:
        parts.append(f"external IP: {lb_assigned[0].split(' → ')[1]}")
    elif lb_pending:
        parts.append(f"external IP: PENDING ({', '.join(lb_pending[:2])})")
    else:
        parts.append("no LoadBalancer services")
    parts.append(f"{ing_count} Ingress resource(s)" +
                 (f", {no_address} without address" if no_address else ""))

    summary = " | ".join(parts)
    return _ok(summary, {
        "available":     True,
        "controller_ok": ctrl_ok,
        "controller_ns": ctrl_ns,
        "controller_pods": ctrl_pods,
        "lb_assigned":   lb_assigned,
        "lb_pending":    lb_pending,
        "ingress_count": ing_count,
        "no_address":    no_address,
    })


def collect_dns_context() -> dict:
    """Quick DNS summary — CoreDNS pod health and kube-dns service."""
    if shutil.which("kubectl") is None:
        return _ok("kubectl not installed — DNS check skipped.", {"available": False})

    # CoreDNS pods
    ok_pods, pods_out = _run(["kubectl", "get", "pods", "-n", "kube-system",
                               "-l", "k8s-app=kube-dns", "--no-headers"])
    total_pods = 0
    running    = 0
    if ok_pods and pods_out.strip():
        for line in pods_out.strip().splitlines():
            total_pods += 1
            if "Running" in line:
                running += 1

    # kube-dns service
    ok_svc, svc_out = _run(["kubectl", "get", "service", "kube-dns",
                             "-n", "kube-system", "--no-headers"])
    svc_ok = ok_svc and bool(svc_out.strip())

    parts = []
    if total_pods:
        parts.append(f"CoreDNS: {running}/{total_pods} pods running")
    else:
        parts.append("CoreDNS pods: NOT FOUND")
    parts.append(f"kube-dns service: {'OK' if svc_ok else 'MISSING'}")

    return _ok(" | ".join(parts), {
        "available":    True,
        "coredns_pods": total_pods,
        "running":      running,
        "svc_ok":       svc_ok,
    })


def collect_network_context() -> dict:
    """Quick network summary — CNI detection, kube-proxy, node readiness."""
    if shutil.which("kubectl") is None:
        return _ok("kubectl not installed — network check skipped.", {"available": False})

    # Detect CNI
    cni_detected = None
    cni_map = {
        "calico":  ("kube-system", "k8s-app=calico-node"),
        "flannel": ("kube-flannel", "app=flannel"),
        "cilium":  ("kube-system", "k8s-app=cilium"),
        "weave":   ("kube-system", "name=weave-net"),
    }
    for cni_name, (ns, sel) in cni_map.items():
        ok, out = _run(["kubectl", "get", "pods", "-n", ns, "-l", sel, "--no-headers"])
        if ok and out.strip():
            cni_detected = cni_name
            break

    # kube-proxy
    ok_kp, kp_out = _run(["kubectl", "get", "daemonset", "kube-proxy",
                           "-n", "kube-system", "--no-headers"])
    kp_ok = ok_kp and bool(kp_out.strip())

    # Nodes ready
    ok_nodes, nodes_out = _run(["kubectl", "get", "nodes", "--no-headers"])
    nodes_total = 0
    nodes_ready = 0
    if ok_nodes and nodes_out.strip():
        for line in nodes_out.strip().splitlines():
            nodes_total += 1
            if " Ready" in line and "NotReady" not in line:
                nodes_ready += 1

    parts = [
        f"CNI: {cni_detected or 'unknown'}",
        f"kube-proxy: {'OK' if kp_ok else 'NOT FOUND'}",
        f"nodes: {nodes_ready}/{nodes_total} Ready",
    ]
    return _ok(" | ".join(parts), {
        "available":     True,
        "cni":           cni_detected,
        "kube_proxy_ok": kp_ok,
        "nodes_ready":   nodes_ready,
        "nodes_total":   nodes_total,
    })


SYSTEM_COLLECTORS = {
    "system":           collect_system,
    "tools":            collect_tools,
    "k8s_context":      collect_k8s_context,
    "ingress_context":  collect_ingress_context,
    "tls_context":      collect_tls_context,
    "dns_context":      collect_dns_context,
    "network_context":  collect_network_context,
    "aws_context":      collect_aws_context,
    "gmail_context":    collect_gmail_context,
    "env":              collect_env_context,
    "git":              collect_git_context,
    "local_network":    collect_local_network,
    "docker":           collect_docker_context,
    "python_packages":  collect_python_packages,
}


def collect_all_system(areas: list[str] | None = None) -> tuple[dict[str, dict], float]:
    """
    Run all system collectors in parallel.
    Returns ({area: result}, elapsed_ms).
    """
    import time
    selected = {k: v for k, v in SYSTEM_COLLECTORS.items() if areas is None or k in areas}
    results: dict[str, dict] = {}

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(selected)) as pool:
        futures = {pool.submit(fn): area for area, fn in selected.items()}
        for future in as_completed(futures, timeout=_TIMEOUT + 5):
            area = futures[future]
            try:
                results[area] = future.result(timeout=_TIMEOUT)
            except TimeoutError:
                results[area] = _err(area, "timed out")
            except Exception as exc:
                results[area] = _err(area, str(exc))

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    log.info("system_collectors.done", areas=list(results), elapsed_ms=elapsed_ms)
    return results, elapsed_ms
