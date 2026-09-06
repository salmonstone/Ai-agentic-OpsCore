"""
Shared data shapes for the entire agent system.

Rule: no raw dicts cross module boundaries — always a typed model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# LLM layer
# ---------------------------------------------------------------------------

@dataclass
class LLMResponse:
    """Everything returned by one Claude API call."""
    content: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    cost: float


# ---------------------------------------------------------------------------
# Email domain
# ---------------------------------------------------------------------------

class Email(BaseModel):
    id: str
    sender: str
    subject: str
    body: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class Priority(str, Enum):
    high   = "high"
    medium = "medium"
    low    = "low"


class Category(str, Enum):
    action  = "action"
    meeting = "meeting"
    info    = "info"
    spam    = "spam"


class TriageResult(BaseModel):
    email_id:         str
    priority:         Priority
    category:         Category
    suggested_action: str
    draft_reply:      str | None = None


# ---------------------------------------------------------------------------
# Kubernetes domain
# ---------------------------------------------------------------------------

class ProblemType(str, Enum):
    CRASH_LOOP   = "CrashLoopBackOff"
    OOM_KILLED   = "OOMKilled"
    PENDING      = "Pending"
    IMAGE_PULL   = "ImagePullBackOff"
    CONFIG_ERROR = "CreateContainerConfigError"
    UNKNOWN      = "Unknown"


class KubectlResult(BaseModel):
    command:     list[str]
    output:      str
    error:       str
    success:     bool
    duration_ms: float


class PodInfo(BaseModel):
    name:       str
    namespace:  str
    status:     str
    ready:      str
    restarts:   int
    age:        str
    node:       str
    ip:         str              = ""
    containers: list[str]       = Field(default_factory=list)
    images:     list[str]       = Field(default_factory=list)
    conditions: list[str]       = Field(default_factory=list)


class NodeInfo(BaseModel):
    name:               str
    status:             str        # Ready / NotReady
    roles:              list[str]  = Field(default_factory=list)
    age:                str
    kubelet_version:    str
    instance_type:      str
    os_image:           str
    capacity_cpu:       str
    capacity_memory:    str
    allocatable_cpu:    str
    allocatable_memory: str
    conditions:         list[str]  = Field(default_factory=list)


class NodeMetrics(BaseModel):
    name:           str
    cpu_cores:      str        # e.g. "250m"
    cpu_percent:    float      # 0-100
    memory_bytes:   str        # e.g. "1800Mi"
    memory_percent: float      # 0-100


class PvcInfo(BaseModel):
    name:          str
    namespace:     str
    status:        str         # Bound / Pending / Lost
    capacity:      str         # e.g. "10Gi"
    storage_class: str
    volume_name:   str
    access_modes:  list[str]   = Field(default_factory=list)


class NodeIssue(BaseModel):
    severity:    str           # "critical" | "warning" | "info"
    issue_type:  str           # "MemoryPressure" | "DiskPressure" | "PvcFull" | "HighMemoryPod" | etc.
    resource:    str           # node name, pod name, pvc name
    namespace:   str           = ""
    description: str
    fix:         str           # human-readable fix description
    fix_command: str | None    = None   # exact kubectl command to apply


class NodeHealthReport(BaseModel):
    generated_at:  str
    node_metrics:  list[NodeMetrics]  = Field(default_factory=list)
    pvcs:          list[PvcInfo]      = Field(default_factory=list)
    top_pods:      list[dict]         = Field(default_factory=list)
    issues:        list[NodeIssue]    = Field(default_factory=list)
    analysis:      str | None         = None


class ScanIssue(BaseModel):
    severity:    str            # "critical" | "warning" | "info"
    area:        str            # "nodes" | "dns" | "network" | "pvcs" | "jobs" | "hpa" | "ingress" | "rbac"
    resource:    str
    namespace:   str            = ""
    description: str
    fix:         str
    fix_command: str | None     = None
    deep_dive:   bool           = False   # True = needs full log/YAML analysis


class FullScanReport(BaseModel):
    generated_at:       str
    collection_ms:      float
    areas_scanned:      list[str]  = Field(default_factory=list)
    areas_failed:       list[str]  = Field(default_factory=list)
    issues:             list[ScanIssue] = Field(default_factory=list)
    analysis:           str | None = None


class NamespaceInfo(BaseModel):
    name:           str
    pods:           list[PodInfo] = Field(default_factory=list)
    total_pods:     int
    healthy_pods:   int
    unhealthy_pods: int


class ClusterOverview(BaseModel):
    cluster_name:      str
    total_namespaces:  int
    namespaces:        list[NamespaceInfo] = Field(default_factory=list)
    total_pods:        int
    healthy_pods:      int
    unhealthy_pods:    int
    total_nodes:       int
    healthy_nodes:     int
    generated_at:      str
    analysis:          str | None = None


class PodDiagnosis(BaseModel):
    pod:           str
    namespace:     str
    problem_type:  ProblemType
    root_cause:    str
    suggested_fix: str
    fix_command:   str | None = None
    confidence:    str        # "high" | "medium" | "low"
    explanation:   str


# ---------------------------------------------------------------------------
# AWS domain
# ---------------------------------------------------------------------------

class AwsResourceType(str, Enum):
    EC2       = "EC2"
    RDS       = "RDS"
    ALB       = "ALB"
    EKS_NODE  = "EKSNode"


class AwsProblemType(str, Enum):
    EC2_STOPPED          = "Ec2Stopped"
    EC2_STATUS_FAILED    = "Ec2StatusFailed"
    EC2_CPU_CREDIT       = "Ec2CpuCreditExhausted"
    RDS_STOPPED          = "RdsStopped"
    RDS_STORAGE_FULL     = "RdsStorageFull"
    RDS_CONN_MAX         = "RdsConnectionsAtMax"
    ALB_UNHEALTHY        = "AlbUnhealthyTargets"
    ALB_NO_TARGETS       = "AlbNoTargets"
    UNKNOWN              = "Unknown"


class AwsResource(BaseModel):
    id:           str
    name:         str
    resource_type: AwsResourceType
    status:       str
    region:       str
    metadata:     dict[str, Any] = Field(default_factory=dict)


class AwsDiagnosis(BaseModel):
    resource_id:   str
    resource_name: str
    resource_type: AwsResourceType
    region:        str
    problem_type:  AwsProblemType
    root_cause:    str
    suggested_fix: str
    fix_command:   str | None = None
    confidence:    str
    explanation:   str


# ---------------------------------------------------------------------------
# TLS / Certificate domain
# ---------------------------------------------------------------------------

class TLSProblemType(str, Enum):
    CERT_EXPIRED          = "CertExpired"
    CERT_EXPIRING_SOON    = "CertExpiringSoon"
    CM_NOT_INSTALLED      = "CertManagerNotInstalled"
    CM_NOT_RUNNING        = "CertManagerNotRunning"
    CERTIFICATE_NOT_READY = "CertificateNotReady"
    ISSUER_NOT_READY      = "IssuerNotReady"
    ACME_CHALLENGE_FAIL   = "AcmeChallengeFailing"
    SECRET_MISSING        = "SecretMissing"
    SECRET_INVALID        = "SecretInvalid"
    HOSTNAME_MISMATCH     = "HostnameMismatch"
    RATE_LIMIT_HIT        = "RateLimitHit"
    SELF_SIGNED           = "SelfSigned"
    WRONG_ISSUER_REF      = "WrongIssuerRef"
    NO_TLS_CONFIGURED     = "NoTLSConfigured"
    WRONG_DOMAIN          = "WrongDomain"
    INGRESS_MISCONFIGURED = "IngressMisconfigured"
    HEALTHY               = "Healthy"
    UNKNOWN               = "Unknown"


class TLSIssue(BaseModel):
    severity:     str
    problem_type: TLSProblemType
    resource:     str
    namespace:    str            = ""
    description:  str
    fix:          str
    fix_command:  str | None     = None
    deep_dive:    bool           = False


class TLSDiagnosis(BaseModel):
    name:          str
    namespace:     str
    problem_type:  TLSProblemType
    root_cause:    str
    suggested_fix: str
    fix_command:   str | None    = None
    confidence:    str
    explanation:   str


class TLSScanReport(BaseModel):
    generated_at:    str
    collection_ms:   float
    issues:          list[TLSIssue] = Field(default_factory=list)
    analysis:        str | None     = None
    cm_installed:    bool           = False
    cm_healthy:      bool           = False
    total_certs:     int            = 0
    expired_certs:   int            = 0
    expiring_certs:  int            = 0


# ---------------------------------------------------------------------------
# Ingress / Nginx domain
# ---------------------------------------------------------------------------

class IngressProblemType(str, Enum):
    EXTERNAL_IP_PENDING   = "ExternalIPPending"
    CONTROLLER_NOT_RUNNING = "ControllerNotRunning"
    CONTROLLER_NOT_INSTALLED = "ControllerNotInstalled"
    NO_ADDRESS            = "NoAddress"
    BACKEND_DOWN          = "BackendDown"
    SERVICE_NOT_FOUND     = "ServiceNotFound"
    TLS_SECRET_MISSING    = "TLSSecretMissing"
    CERT_MANAGER_ERROR    = "CertManagerError"
    WRONG_INGRESS_CLASS   = "WrongIngressClass"
    NO_INGRESS_CLASS      = "NoIngressClass"
    CONFIG_MAP_ERROR      = "ConfigMapError"
    HEALTHY               = "Healthy"
    UNKNOWN               = "Unknown"


class IngressIssue(BaseModel):
    severity:     str                    # "critical" | "warning" | "info"
    problem_type: IngressProblemType
    resource:     str
    namespace:    str                    = ""
    description:  str
    fix:          str
    fix_command:  str | None             = None
    deep_dive:    bool                   = False


class IngressDiagnosis(BaseModel):
    name:          str
    namespace:     str
    problem_type:  IngressProblemType
    root_cause:    str
    suggested_fix: str
    fix_command:   str | None            = None
    confidence:    str
    explanation:   str


class IngressScanReport(BaseModel):
    generated_at:    str
    collection_ms:   float
    issues:          list[IngressIssue]  = Field(default_factory=list)
    analysis:        str | None          = None
    controller_ok:   bool                = False
    external_ip:     str | None          = None


# ---------------------------------------------------------------------------
# Ingress / TLS discovery models
# ---------------------------------------------------------------------------

class IngressInfo(BaseModel):
    name:            str
    namespace:       str
    domain:          str
    tls_enabled:     bool
    tls_secret:      str       = ""
    backend_service: str       = ""
    address:         str       = ""
    age:             str       = ""


class TLSSecretInfo(BaseModel):
    name:               str
    namespace:          str
    domain:             str    = ""
    issuer:             str    = ""
    expiry_date:        str    = ""
    days_until_expiry:  int    = -1
    is_expired:         bool   = False
    is_expiring_soon:   bool   = False
    cert_type:          str    = "unknown"


class CertInfo(BaseModel):
    name:        str
    namespace:   str
    domain:      str    = ""
    ready:       bool   = False
    status:      str    = ""
    message:     str    = ""
    expiry:      str    = ""
    issuer:      str    = ""
    secret_name: str    = ""


class TLSMonitorDiagnosis(BaseModel):
    domain:         str
    ingress:        IngressInfo
    cert_info:      TLSSecretInfo | None = None
    problem_type:   str
    root_cause:     str
    suggested_fix:  str
    fix_command:    str | None   = None
    fix_type:       str          = "none"
    confidence:     str
    explanation:    str
    claude_analysis: str         = ""


# ---------------------------------------------------------------------------
# DNS domain
# ---------------------------------------------------------------------------

class DNSProblemType(str, Enum):
    COREDNS_NOT_RUNNING    = "CoreDNSNotRunning"
    COREDNS_NOT_INSTALLED  = "CoreDNSNotInstalled"
    COREDNS_CONFIG_INVALID = "CoreDNSConfigInvalid"
    DNS_RESOLUTION_FAIL    = "DNSResolutionFail"
    EXTERNAL_DNS_FAIL      = "ExternalDNSFail"
    NDOTS_MISCONFIGURED    = "NdotsMisconfigured"
    KUBE_DNS_SVC_MISSING   = "KubeDNSSvcMissing"
    NO_ENDPOINTS           = "NoEndpoints"
    HEALTHY                = "Healthy"
    UNKNOWN                = "Unknown"


class DNSIssue(BaseModel):
    severity:     str
    problem_type: DNSProblemType
    resource:     str
    namespace:    str            = ""
    description:  str
    fix:          str
    fix_command:  str | None     = None
    deep_dive:    bool           = False


class DNSDiagnosis(BaseModel):
    name:          str
    namespace:     str
    problem_type:  DNSProblemType
    root_cause:    str
    suggested_fix: str
    fix_command:   str | None    = None
    confidence:    str
    explanation:   str


class DNSScanReport(BaseModel):
    generated_at:       str
    collection_ms:      float
    issues:             list[DNSIssue] = Field(default_factory=list)
    analysis:           str | None     = None
    coredns_healthy:    bool           = False
    resolution_ok:      bool           = False
    external_dns_found: bool           = False


# ---------------------------------------------------------------------------
# Network domain
# ---------------------------------------------------------------------------

class NetworkProblemType(str, Enum):
    CNI_NOT_RUNNING        = "CniNotRunning"
    CNI_NOT_DETECTED       = "CniNotDetected"
    KUBE_PROXY_DOWN        = "KubeProxyDown"
    SERVICE_NO_ENDPOINTS   = "ServiceNoEndpoints"
    POD_CONNECTIVITY_FAIL  = "PodConnectivityFail"
    NETWORK_POLICY_BLOCKING = "NetworkPolicyBlocking"
    NODE_NETWORK_UNAVAIL   = "NodeNetworkUnavailable"
    NODE_PRESSURE          = "NodePressure"
    HEALTHY                = "Healthy"
    UNKNOWN                = "Unknown"


class NetworkIssue(BaseModel):
    severity:     str
    problem_type: NetworkProblemType
    resource:     str
    namespace:    str              = ""
    description:  str
    fix:          str
    fix_command:  str | None       = None
    deep_dive:    bool             = False


class NetworkDiagnosis(BaseModel):
    name:          str
    namespace:     str
    problem_type:  NetworkProblemType
    root_cause:    str
    suggested_fix: str
    fix_command:   str | None      = None
    confidence:    str
    explanation:   str


class NetworkScanReport(BaseModel):
    generated_at:   str
    collection_ms:  float
    issues:         list[NetworkIssue] = Field(default_factory=list)
    analysis:       str | None         = None
    cni_detected:   str | None         = None
    cni_healthy:    bool               = False
    nodes_ready:    int                = 0
    nodes_total:    int                = 0


# ---------------------------------------------------------------------------
# Memory layer
# ---------------------------------------------------------------------------

class Memory(BaseModel):
    id:         str
    content:    str
    source:     str
    metadata:   dict[str, Any] = Field(default_factory=dict)
    created_at: datetime       = Field(default_factory=datetime.utcnow)
    embedding:  list[float] | None = None


# ---------------------------------------------------------------------------
# Resource Monitor domain
# ---------------------------------------------------------------------------

class NodeResourceMetrics(BaseModel):
    name:           str
    cpu_usage:      str        # e.g. "450m"
    cpu_percent:    int        # 0-100
    memory_usage:   str        # e.g. "2Gi"
    memory_percent: int        # 0-100
    status:         str        # "healthy" | "warning" | "critical"
    pressure:       bool       # True if any metric is at critical level


class PodResourceMetrics(BaseModel):
    name:           str
    namespace:      str
    cpu_usage:      str
    cpu_percent:    int        # 0 if no cpu limit set
    memory_usage:   str
    memory_percent: int        # 0 if no memory limit set
    cpu_limit:      str        # "none" if not set
    memory_limit:   str        # "none" if not set
    at_risk:        bool
    risk_type:      str        # "cpu" | "memory" | "both" | "no_limits" | "none"


class ResourceLimits(BaseModel):
    cpu_limit:      str
    memory_limit:   str
    cpu_request:    str
    memory_request: str
    has_limits:     bool
    has_requests:   bool


class HPAInfo(BaseModel):
    name:             str
    namespace:        str
    target:           str
    min_replicas:     int
    max_replicas:     int
    current_replicas: int
    cpu_target:       int
    cpu_current:      int


class ResourceAlert(BaseModel):
    pod_or_node:    str
    namespace:      str        = ""
    alert_type:     str        # "CPU_HIGH" | "MEM_HIGH" | "NO_LIMITS" | "OOM_RISK"
    severity:       str        # "critical" | "warning" | "info"
    current_usage:  str
    limit:          str
    percent_used:   int
    recommendation: str
    fix_command:    str | None = None
    auto_fix:       bool       = False


class ResourceReport(BaseModel):
    nodes:               list[NodeResourceMetrics]  = Field(default_factory=list)
    pods:                list[PodResourceMetrics]   = Field(default_factory=list)
    alerts:              list[ResourceAlert]        = Field(default_factory=list)
    critical_count:      int                        = 0
    warning_count:       int                        = 0
    healthy_count:       int                        = 0
    pods_without_limits: list[str]                  = Field(default_factory=list)
    claude_analysis:     str                        = ""
    generated_at:        str                        = ""


# ---------------------------------------------------------------------------
# Security audit
# ---------------------------------------------------------------------------

class SecurityFinding(BaseModel):
    id:                str
    severity:          str            # critical / high / medium / low / info
    category:          str            # privilege / secret / rbac / network / runtime / exposure
    title:             str
    description:       str
    affected_resource: str
    namespace:         str            = ""
    evidence:          str            = ""
    recommendation:    str            = ""
    fix_command:       str | None     = None
    cve_reference:     str | None     = None


class SecurityReport(BaseModel):
    cluster_name:    str                        = ""
    scan_time:       str                        = ""
    total_findings:  int                        = 0
    critical:          int                        = 0
    high:              int                        = 0
    medium:            int                        = 0
    low:               int                        = 0
    info:              int                        = 0
    system_components: int                        = 0
    findings:          list[SecurityFinding]      = Field(default_factory=list)
    security_score:  int                        = 100
    production_ready: bool                      = False
    claude_summary:  str                        = ""


class SecurityDriftReport(BaseModel):
    """What changed since the last security audit of this cluster."""
    cluster_name:     str                    = ""
    scan_time:        str                    = ""
    has_baseline:     bool                   = False   # False = no prior scan to compare against
    new_findings:     list[SecurityFinding]  = Field(default_factory=list)
    resolved_count:   int                    = 0        # findings that existed before, gone now
    unchanged_count:  int                    = 0        # findings present in both scans
    total_active:     int                    = 0        # total real findings right now


class TimelineEvent(BaseModel):
    """One entry in a correlated incident's causal chain."""
    timestamp:  str = ""
    source:     str = ""   # "deploy" | "daemon_action" | "memory:<skill-source>" | "incident"
    event_type: str = ""   # e.g. "deploy", "error_rate_spike", "pod_restart", "latency_increase"
    detail:     str = ""


class CorrelatedIncident(BaseModel):
    """
    Result of scanning every existing signal source (deploys, daemon actions,
    memory/RAG across every skill, open incidents) within a time window and
    asking Claude whether they represent one causally-linked incident.
    """
    window_minutes:       int                    = 30
    has_signal:           bool                   = False   # False = nothing at all in the window
    is_incident:          bool                   = False   # True = Claude judged this a real, linked incident
    confidence:           str                    = "low"   # high / medium / low
    title:                str                    = ""
    root_cause:           str                    = ""
    contributing_factors: list[str]              = Field(default_factory=list)
    primary_service:      str                    = ""
    namespace:            str                    = ""
    severity:             str                    = "warning"
    timeline:             list[TimelineEvent]    = Field(default_factory=list)
    incident_id:          str | None             = None    # set only if an incident was opened/reused
    signal_counts:        dict                   = Field(default_factory=dict)
    summary:              str                    = ""


# ---------------------------------------------------------------------------
# Multi-cluster management
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Cost analysis
# ---------------------------------------------------------------------------

class PodCost(BaseModel):
    pod:                str
    namespace:          str
    deployment:         str
    cpu_request:        float = 0.0
    memory_request:     float = 0.0   # GB
    cpu_actual:         float = 0.0
    memory_actual:      float = 0.0   # GB
    node_instance_type: str   = "unknown"
    est_monthly_cost:   float = 0.0
    waste_percent:      int   = 0
    waste_label:        str   = "unknown"  # OK / over-provisioned / under-provisioned


class DeploymentCost(BaseModel):
    deployment:        str
    namespace:         str
    pod_count:         int   = 0
    total_cpu_request: float = 0.0
    total_cpu_actual:  float = 0.0
    est_monthly_cost:  float = 0.0
    waste_percent:     int   = 0
    waste_label:       str   = "unknown"


class NamespaceCost(BaseModel):
    namespace:        str
    pod_count:        int   = 0
    est_monthly_cost: float = 0.0


class CostReport(BaseModel):
    cluster_name:             str                    = ""
    total_nodes:              int                    = 0
    total_monthly_node_cost:  float                  = 0.0
    total_requested_cost:     float                  = 0.0
    total_waste_cost:         float                  = 0.0
    waste_percent:            int                    = 0
    namespaces:               list[NamespaceCost]    = Field(default_factory=list)
    deployments:              list[DeploymentCost]   = Field(default_factory=list)
    top_wasteful_pods:        list[PodCost]          = Field(default_factory=list)
    claude_analysis:          str                    = ""
    generated_at:             str                    = ""


class AWSCostData(BaseModel):
    period:                   str            = ""
    total_spend:              float          = 0.0
    by_service:               dict           = Field(default_factory=dict)
    daily_average:            float          = 0.0
    today_estimate:           float          = 0.0
    ec2_spend:                float          = 0.0
    k8s_utilization_percent:  int            = 0
    idle_overhead:            float          = 0.0


# ---------------------------------------------------------------------------
# Multi-cluster management
# ---------------------------------------------------------------------------

class ClusterContext(BaseModel):
    name:           str
    cluster:        str   = ""
    user:           str   = ""
    namespace:      str   = "default"
    is_current:     bool  = False
    cloud_provider: str   = "unknown"   # aws / gcp / azure / local
    region:         str   = ""
    environment:    str   = "unknown"   # dev / staging / prod / testing
    health:         str   = "unknown"   # healthy / unreachable / unknown
    node_count:     int   = 0
    last_used:      str   = ""


# ---------------------------------------------------------------------------
# Pod Log Analysis domain
# ---------------------------------------------------------------------------

class PodLogs(BaseModel):
    pod:              str
    namespace:        str
    containers:       list[str]        = []
    current_logs:     dict[str, str]   = Field(default_factory=dict)   # container_name -> log text
    previous_logs:    dict[str, str]   = Field(default_factory=dict)   # container_name -> log text (crash logs)
    has_previous:     bool             = False
    log_lines_count:  int              = 0
    fetch_errors:     list[str]        = []


class ContainerState(BaseModel):
    name:          str
    ready:         bool       = False
    restart_count: int        = 0
    state:         str        = "unknown"    # running / waiting / terminated
    last_state:    str        = ""
    exit_code:     int | None = None
    reason:        str | None = None         # OOMKilled / Error / Completed / CrashLoopBackOff


class LogAnalysis(BaseModel):
    pod:                  str
    namespace:            str
    root_cause:           str        = ""
    error_type:           str        = "UNKNOWN"   # OOM / CONFIG / NETWORK / CRASH / PERMISSION / UNKNOWN
    confidence:           str        = "low"       # high / medium / low
    key_log_lines:        list[str]  = []
    explanation:          str        = ""
    suggested_fix:        str        = ""
    fix_command:          str | None = None
    related_to_restart:   bool       = False
    claude_full_analysis: str        = ""


# ---------------------------------------------------------------------------
# Deployment Management domain
# ---------------------------------------------------------------------------

class DeploymentInfo(BaseModel):
    name:             str
    namespace:        str
    replicas_desired: int           = 0
    replicas_ready:   int           = 0
    current_image:    str           = ""
    containers:       list[str]     = Field(default_factory=list)
    strategy:         str           = "RollingUpdate"
    max_surge:        str           = "25%"
    max_unavailable:  str           = "25%"
    revision:         int           = 0
    healthy:          bool          = False
    labels:           dict[str, str] = Field(default_factory=dict)


class Revision(BaseModel):
    revision_number: int
    image:           str            = ""
    change_cause:    str            = "<none>"
    created_at:      str            = ""


class DeploymentAction(BaseModel):
    action_type:      str           # rollback / scale / deploy / restart
    deployment:       str
    namespace:        str
    current_state:    str           = ""
    proposed_state:   str           = ""
    is_safe:          bool          = True
    risk_level:       str           = "low"       # low / medium / high
    downtime_estimate: str          = "~0 seconds"
    warnings:         list[str]     = Field(default_factory=list)
    claude_analysis:  str           = ""
    command:          str           = ""
    is_production:    bool          = False


# ---------------------------------------------------------------------------
# GitHub Webhook models
# ---------------------------------------------------------------------------

class WebhookMapping(BaseModel):
    repo:                  str
    branch:                str  = "main"
    deployment:            str
    namespace:             str
    image_prefix:          str  = ""
    auto_approve_low_risk: bool = False


class WebhookConfig(BaseModel):
    webhook_secret:        str              = ""
    auto_approve_low_risk: bool             = False
    mappings:              list[WebhookMapping] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Webhook event + pending deploy
# ---------------------------------------------------------------------------

class WebhookEvent(BaseModel):
    repo:            str
    branch:          str
    commit_sha:      str
    commit_message:  str
    author:          str
    files_changed:   list[str] = Field(default_factory=list)
    timestamp:       str       = ""


class PendingDeploy(BaseModel):
    id:           str
    repo:         str
    branch:       str
    deployment:   str
    namespace:    str
    new_image:    str
    old_image:    str           = ""
    risk_score:   int           = 0
    risk_label:   str           = "low"
    status:       str           = "pending"   # pending/approved/rejected/deployed/failed
    created_at:   str           = ""
    approved_at:  str | None    = None
    deployed_at:  str | None    = None
    commit_sha:   str           = ""
    author:       str           = ""
    commit_message: str         = ""


# ---------------------------------------------------------------------------
# Health gate
# ---------------------------------------------------------------------------

class CheckResult(BaseModel):
    name:     str
    passed:   bool
    severity: str   = "pass"   # pass / warn / block
    message:  str   = ""
    detail:   str   = ""


class HealthGateResult(BaseModel):
    passed:           bool
    blocked:          bool
    checks:           list[CheckResult]    = Field(default_factory=list)
    warnings:         list[str]            = Field(default_factory=list)
    blockers:         list[str]            = Field(default_factory=list)
    recommendation:   str                  = ""
    check_duration_ms: int                 = 0


# ---------------------------------------------------------------------------
# Risk scorer
# ---------------------------------------------------------------------------

class RiskScore(BaseModel):
    total:          int
    label:          str                             # low/medium/high/critical
    factors:        list[tuple[str, int]]           = Field(default_factory=list)
    recommendation: str                             = ""
    auto_approve:   bool                            = False


# ---------------------------------------------------------------------------
# Post-deploy watcher
# ---------------------------------------------------------------------------

class WatchSample(BaseModel):
    elapsed_s:    int
    ready_count:  int
    total_count:  int
    restart_delta: int   = 0
    error_lines:  int    = 0
    memory_mi:    int    = 0
    event:        str    = ""   # "" / "warn" / "rollback"
    message:      str    = ""


class WatchResult(BaseModel):
    success:              bool
    rollback_triggered:   bool              = False
    rollback_reason:      str | None        = None
    samples:              list[WatchSample] = Field(default_factory=list)
    final_ready_count:    int               = 0
    errors_detected:      int               = 0
    memory_delta_percent: int               = 0
    restart_count:        int               = 0
    duration_seconds:     int               = 0


# ---------------------------------------------------------------------------
# Deploy report
# ---------------------------------------------------------------------------

class DeployReport(BaseModel):
    id:                 str
    deployment:         str
    namespace:          str
    status:             str       # SUCCESS / FAILED / ROLLED_BACK
    old_image:          str       = ""
    new_image:          str       = ""
    risk_level:         str       = "low"
    duration_seconds:   int       = 0
    pods_healthy:       int       = 0
    errors_detected:    int       = 0
    rollback_triggered: bool      = False
    claude_summary:     str       = ""
    timestamp:          str       = ""


# ── AWS Cost Optimization ──────────────────────────────────────────────────

class CostFix(BaseModel):
    id: str
    category: str
    title: str
    description: str
    monthly_savings: float
    annual_savings: float = 0.0
    effort: str          # easy / medium / hard
    risk: str            # safe / low / medium / high
    fix_type: str        # aws_cli / console / automatic
    fix_command: str | None = None
    auto_fixable: bool = False
    aws_resource_id: str | None = None
    priority: int = 0
    roi_score: float = 0.0

    def model_post_init(self, __context):
        if self.annual_savings == 0.0:
            self.annual_savings = round(self.monthly_savings * 12, 2)


class IdleResources(BaseModel):
    unattached_volumes: list[dict] = []
    unused_eips: list[dict] = []
    old_snapshots: list[dict] = []
    idle_load_balancers: list[dict] = []
    stopped_instances: list[dict] = []
    total_monthly_waste: float = 0.0


class AWSCostAnalysis(BaseModel):
    period_days: int = 30
    total_spend: float = 0.0
    last_month_spend: float = 0.0
    month_change_percent: float = 0.0
    forecast_this_month: float = 0.0
    by_service: dict = {}
    idle_resources: IdleResources = Field(default_factory=IdleResources)
    ebs_opportunities: list[dict] = []
    cloudwatch_logs: dict = {}
    ecr_waste: dict = {}
    rightsizing: list[dict] = []
    reserved_vs_ondemand: dict = {}
    total_monthly_waste: float = 0.0
    savings_plan: list[CostFix] = []
    anomalies: dict = {}
    savings_plans: dict = {}
    cost_by_tag: dict = {}
    claude_analysis: str = ""
    generated_at: str = ""


# ── Terraform (IaC) scan ────────────────────────────────────────────────────

class TfResource(BaseModel):
    """One resource block found in the .tf files."""
    address: str                      # e.g. aws_instance.web
    type: str                         # aws_instance
    name: str                         # web
    file: str = ""
    line: int = 0
    provider: str = ""                # aws / google / azurerm …
    stateful: bool = False            # holds data — destroy/recreate loses it


class TfFinding(BaseModel):
    """A security / risk / cost concern about the configuration."""
    id: str
    severity: str                     # critical / high / medium / low / info
    category: str                     # security / cost / blast-radius / reliability
    title: str
    description: str
    resource: str = ""                # affected resource address
    file: str = ""
    line: int = 0
    evidence: str = ""                # the offending attribute/value
    recommendation: str = ""
    auto_fixable: bool = False        # can the .tf patcher fix it safely?
    fix_attribute: str | None = None  # attribute the patcher would add, e.g. encrypted = true


class TfCostItem(BaseModel):
    """Estimated monthly cost for a single billable resource."""
    resource: str
    type: str
    detail: str = ""                  # instance type, size, etc.
    monthly_cost: float = 0.0
    assumptions: str = ""             # what we assumed (region, hours, count)


class TfModule(BaseModel):
    """A module block — usually where the real infrastructure lives."""
    name: str
    source: str = ""
    version: str = ""
    pinned: bool = False              # version constrained / git ref present
    local: bool = False               # local path source (./modules/x)
    file: str = ""
    line: int = 0


class TerraformReport(BaseModel):
    root: str = ""
    file_count: int = 0
    resource_count: int = 0
    providers: list[str] = Field(default_factory=list)
    resources: list[TfResource] = Field(default_factory=list)
    modules: list[TfModule] = Field(default_factory=list)
    findings: list[TfFinding] = Field(default_factory=list)
    cost_items: list[TfCostItem] = Field(default_factory=list)
    estimated_monthly_cost: float = 0.0
    plan_actions: dict = Field(default_factory=dict)   # create/update/delete/replace counts
    replace_addresses: list[str] = Field(default_factory=list)
    security_score: int = 100
    remote_state: bool = False        # backend configured (vs local state)
    parse_errors: list[str] = Field(default_factory=list)
    claude_summary: str = ""
    generated_at: str = ""

    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "critical")

    @property
    def high_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "high")


# ---------------------------------------------------------------------------
# Jenkins CI/CD domain
# ---------------------------------------------------------------------------

class JenkinsProblemType(str, Enum):
    FLAKY_TEST             = "FLAKY_TEST"
    BROKEN_DEPENDENCY      = "BROKEN_DEPENDENCY"
    AGENT_OFFLINE          = "AGENT_OFFLINE"
    DISK_FULL              = "DISK_FULL"
    CREDENTIAL_EXPIRED     = "CREDENTIAL_EXPIRED"
    BAD_JENKINSFILE_SYNTAX = "BAD_JENKINSFILE_SYNTAX"
    MERGE_CONFLICT         = "MERGE_CONFLICT"
    TEST_TIMEOUT           = "TEST_TIMEOUT"
    BUILD_TIMEOUT          = "BUILD_TIMEOUT"
    NETWORK_ERROR          = "NETWORK_ERROR"
    DOCKER_ERROR           = "DOCKER_ERROR"
    OUT_OF_MEMORY          = "OUT_OF_MEMORY"
    PERMISSION_DENIED      = "PERMISSION_DENIED"
    STUCK_IN_QUEUE         = "STUCK_IN_QUEUE"
    INFRASTRUCTURE_ISSUE   = "INFRASTRUCTURE_ISSUE"
    UNKNOWN                = "UNKNOWN"


class JenkinsFixAction(str, Enum):
    RETRIGGER_BUILD      = "RETRIGGER_BUILD"
    RESTART_AGENT        = "RESTART_AGENT"
    CLEAR_WORKSPACE      = "CLEAR_WORKSPACE"
    CANCEL_AND_RETRIGGER = "CANCEL_AND_RETRIGGER"
    TOGGLE_AGENT_OFFLINE = "TOGGLE_AGENT_OFFLINE"
    MANUAL_ONLY          = "MANUAL_ONLY"
    NO_ACTION_NEEDED     = "NO_ACTION_NEEDED"


class JenkinsInfo(BaseModel):
    version:      str  = ""
    url:          str  = ""
    num_executors: int = 0
    node_count:   int  = 0
    connected:    bool = False
    error:        str  = ""


class JenkinsJob(BaseModel):
    name:                    str
    url:                     str = ""
    color:                   str = ""   # blue/red/yellow/grey/aborted/notbuilt/disabled
    last_build_number:       int | None = None
    last_build_status:       str | None = None
    last_build_timestamp:    int | None = None
    last_build_duration_ms:  int | None = None
    is_folder:               bool = False

    @property
    def is_failing(self) -> bool:
        return any(tok in self.color for tok in ("red", "yellow", "aborted"))


class BuildInfo(BaseModel):
    number:     int
    status:     str = ""   # SUCCESS/FAILURE/UNSTABLE/ABORTED
    timestamp:  int = 0
    duration_ms: int = 0
    causes:     list[str] = Field(default_factory=list)
    changes:    list[str] = Field(default_factory=list)
    parameters: dict = Field(default_factory=dict)
    node:       str = ""
    artifacts:  list[str] = Field(default_factory=list)


class JenkinsNode(BaseModel):
    name:           str
    online:         bool = False
    idle:           bool = False
    offline_cause:  str | None = None
    num_executors:  int  = 0
    labels:         list[str] = Field(default_factory=list)
    temp_offline:   bool = False


class QueueItem(BaseModel):
    id:            int
    job_name:      str = ""
    why:           str = ""
    blocked:       bool = False
    buildable:     bool = False
    params:        dict = Field(default_factory=dict)
    stuck_minutes: int  = 0


class JenkinsDiagnosis(BaseModel):
    job_name:      str = ""
    build_number:  int = 0
    problem_type:  JenkinsProblemType = JenkinsProblemType.UNKNOWN
    root_cause:    str = ""
    confidence:    str = "low"     # high/medium/low
    fix_action:    JenkinsFixAction = JenkinsFixAction.MANUAL_ONLY
    fix_params:    dict = Field(default_factory=dict)
    explanation:   str = ""
    prevention:    str = ""
    auto_fixable:  bool = False
    risk_level:    str = "medium"  # low/medium/high


class JenkinsScanReport(BaseModel):
    total_jobs:          int = 0
    failing_jobs:        int = 0
    unstable_jobs:       int = 0
    offline_nodes:       int = 0
    stuck_queue_items:   int = 0
    diagnoses:           list[JenkinsDiagnosis] = Field(default_factory=list)
    auto_fixable_count:  int = 0
    manual_count:        int = 0
    health_score:        int = 100
    generated_at:        str = ""


class JenkinsFixResult(BaseModel):
    success:          bool = False
    action_taken:     str  = ""
    new_build_number: int | None = None
    message:          str  = ""
    verified:         bool = False


class JenkinsPattern(BaseModel):
    title:          str = ""
    likely_cause:   str = ""
    recommendation: str = ""
    affected_jobs:  list[str] = Field(default_factory=list)
