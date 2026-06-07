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
    name:      str
    namespace: str
    domain:    str    = ""
    ready:     bool   = False
    status:    str    = ""
    message:   str    = ""
    expiry:    str    = ""
    issuer:    str    = ""


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
