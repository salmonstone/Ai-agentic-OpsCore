"""
EC2 on-demand hourly pricing table (us-east-1 baseline).

Prices are approximate on-demand rates as of mid-2025.
Actual cost depends on region, Spot vs Reserved, Savings Plans,
and EBS / data-transfer charges billed separately.

Use REGION_MULTIPLIER to rough-adjust for other regions.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Pricing table  — (cpu cores, memory GB, hourly USD)
# ---------------------------------------------------------------------------

EC2_PRICING: dict[str, dict] = {
    # ── General purpose m5 ──────────────────────────────────────────────────
    "m5.large":    {"cpu": 2,  "memory_gb": 8,   "hourly": 0.096},
    "m5.xlarge":   {"cpu": 4,  "memory_gb": 16,  "hourly": 0.192},
    "m5.2xlarge":  {"cpu": 8,  "memory_gb": 32,  "hourly": 0.384},
    "m5.4xlarge":  {"cpu": 16, "memory_gb": 64,  "hourly": 0.768},
    "m5.8xlarge":  {"cpu": 32, "memory_gb": 128, "hourly": 1.536},
    # ── General purpose m6i ─────────────────────────────────────────────────
    "m6i.large":   {"cpu": 2,  "memory_gb": 8,   "hourly": 0.096},
    "m6i.xlarge":  {"cpu": 4,  "memory_gb": 16,  "hourly": 0.192},
    "m6i.2xlarge": {"cpu": 8,  "memory_gb": 32,  "hourly": 0.384},
    "m6i.4xlarge": {"cpu": 16, "memory_gb": 64,  "hourly": 0.768},
    # ── General purpose m7i ─────────────────────────────────────────────────
    "m7i.large":   {"cpu": 2,  "memory_gb": 8,   "hourly": 0.1008},
    "m7i.xlarge":  {"cpu": 4,  "memory_gb": 16,  "hourly": 0.2016},
    "m7i.2xlarge": {"cpu": 8,  "memory_gb": 32,  "hourly": 0.4032},
    # ── Burstable t3 ────────────────────────────────────────────────────────
    "t3.micro":    {"cpu": 2,  "memory_gb": 1,   "hourly": 0.0104},
    "t3.small":    {"cpu": 2,  "memory_gb": 2,   "hourly": 0.0208},
    "t3.medium":   {"cpu": 2,  "memory_gb": 4,   "hourly": 0.0416},
    "t3.large":    {"cpu": 2,  "memory_gb": 8,   "hourly": 0.0832},
    "t3.xlarge":   {"cpu": 4,  "memory_gb": 16,  "hourly": 0.1664},
    "t3.2xlarge":  {"cpu": 8,  "memory_gb": 32,  "hourly": 0.3328},
    # ── Burstable t3a ───────────────────────────────────────────────────────
    "t3a.medium":  {"cpu": 2,  "memory_gb": 4,   "hourly": 0.0376},
    "t3a.large":   {"cpu": 2,  "memory_gb": 8,   "hourly": 0.0752},
    "t3a.xlarge":  {"cpu": 4,  "memory_gb": 16,  "hourly": 0.1504},
    # ── Compute optimised c5 ────────────────────────────────────────────────
    "c5.large":    {"cpu": 2,  "memory_gb": 4,   "hourly": 0.085},
    "c5.xlarge":   {"cpu": 4,  "memory_gb": 8,   "hourly": 0.170},
    "c5.2xlarge":  {"cpu": 8,  "memory_gb": 16,  "hourly": 0.340},
    "c5.4xlarge":  {"cpu": 16, "memory_gb": 32,  "hourly": 0.680},
    # ── Compute optimised c6i ───────────────────────────────────────────────
    "c6i.large":   {"cpu": 2,  "memory_gb": 4,   "hourly": 0.085},
    "c6i.xlarge":  {"cpu": 4,  "memory_gb": 8,   "hourly": 0.170},
    "c6i.2xlarge": {"cpu": 8,  "memory_gb": 16,  "hourly": 0.340},
    # ── Memory optimised r5 ─────────────────────────────────────────────────
    "r5.large":    {"cpu": 2,  "memory_gb": 16,  "hourly": 0.126},
    "r5.xlarge":   {"cpu": 4,  "memory_gb": 32,  "hourly": 0.252},
    "r5.2xlarge":  {"cpu": 8,  "memory_gb": 64,  "hourly": 0.504},
    "r5.4xlarge":  {"cpu": 16, "memory_gb": 128, "hourly": 1.008},
    # ── Memory optimised r6i ────────────────────────────────────────────────
    "r6i.large":   {"cpu": 2,  "memory_gb": 16,  "hourly": 0.126},
    "r6i.xlarge":  {"cpu": 4,  "memory_gb": 32,  "hourly": 0.252},
    "r6i.2xlarge": {"cpu": 8,  "memory_gb": 64,  "hourly": 0.504},
    # ── Storage optimised i3 ────────────────────────────────────────────────
    "i3.large":    {"cpu": 2,  "memory_gb": 15,  "hourly": 0.156},
    "i3.xlarge":   {"cpu": 4,  "memory_gb": 30,  "hourly": 0.312},
    "i3.2xlarge":  {"cpu": 8,  "memory_gb": 61,  "hourly": 0.624},
}

# Rough region price multipliers relative to us-east-1
REGION_MULTIPLIER: dict[str, float] = {
    "us-east-1":      1.00,
    "us-east-2":      1.00,
    "us-west-1":      1.10,
    "us-west-2":      1.00,
    "eu-west-1":      1.08,
    "eu-west-2":      1.11,
    "eu-central-1":   1.09,
    "ap-southeast-1": 1.13,
    "ap-southeast-2": 1.14,
    "ap-northeast-1": 1.14,
    "ap-south-1":     1.00,
    "sa-east-1":      1.19,
    "ca-central-1":   1.04,
}

HOURS_PER_MONTH = 730


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_instance_price(instance_type: str) -> dict | None:
    """Return pricing dict for instance_type, or None if unknown."""
    return EC2_PRICING.get(instance_type)


def get_monthly_cost(hourly_rate: float, region: str = "us-east-1") -> float:
    """Return monthly cost adjusted for region."""
    multiplier = REGION_MULTIPLIER.get(region, 1.0)
    return round(hourly_rate * HOURS_PER_MONTH * multiplier, 2)


def estimate_hourly_from_cpu(cpu_count: int) -> float:
    """Fallback: rough estimate when instance type is unknown (~$0.048/vCPU)."""
    return round(cpu_count * 0.048, 4)
