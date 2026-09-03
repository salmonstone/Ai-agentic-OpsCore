"""
GitHub webhook mapping loader.

Reads data/webhook_mappings.yaml, caches in memory for 60 seconds,
and provides helpers to find, add, and remove mappings.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import NamedTuple

import yaml

from agent.core.models import WebhookConfig, WebhookMapping
from agent.integrations.kubectl import run_kubectl

_MAPPINGS_FILE = Path("data/webhook_mappings.yaml")


class ValidationResult(NamedTuple):
    valid:  bool
    errors: list[str]


class MappingLoader:
    _cache: WebhookConfig | None = None
    _cache_ts: float = 0.0
    _TTL: float = 60.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self) -> WebhookConfig:
        now = time.monotonic()
        if self._cache is not None and (now - self._cache_ts) < self._TTL:
            return self._cache
        self._cache    = self._read_file()
        self._cache_ts = now
        return self._cache

    def find_mapping(self, repo: str, branch: str) -> WebhookMapping | None:
        cfg = self.load()
        # exact match first
        for m in cfg.mappings:
            if m.repo == repo and m.branch == branch:
                return m
        # fallback to main
        if branch != "main":
            for m in cfg.mappings:
                if m.repo == repo and m.branch == "main":
                    return m
        return None

    def add_mapping(self, mapping: WebhookMapping) -> None:
        cfg = self._read_file()
        # replace if same repo+branch exists, otherwise append
        cfg.mappings = [
            m for m in cfg.mappings
            if not (m.repo == mapping.repo and m.branch == mapping.branch)
        ]
        cfg.mappings.append(mapping)
        self._write_file(cfg)
        self._cache = None  # bust cache

    def remove_mapping(self, repo: str, branch: str) -> bool:
        cfg      = self._read_file()
        before   = len(cfg.mappings)
        cfg.mappings = [
            m for m in cfg.mappings
            if not (m.repo == repo and m.branch == branch)
        ]
        if len(cfg.mappings) == before:
            return False
        self._write_file(cfg)
        self._cache = None
        return True

    def validate_mapping(self, mapping: WebhookMapping) -> ValidationResult:
        errors: list[str] = []

        if "/" not in mapping.repo:
            errors.append("repo must be in 'owner/name' format (e.g. username/repo)")

        ns_ok, ns_out = self._kubectl_check(
            ["get", "namespace", mapping.namespace, "--no-headers"]
        )
        if not ns_ok:
            errors.append(f"namespace '{mapping.namespace}' not found in cluster")

        dep_ok, dep_out = self._kubectl_check(
            ["get", "deployment", mapping.deployment, "-n", mapping.namespace, "--no-headers"]
        )
        if not dep_ok:
            errors.append(
                f"deployment '{mapping.deployment}' not found in namespace '{mapping.namespace}'"
            )

        if mapping.image_prefix and " " in mapping.image_prefix:
            errors.append("image_prefix must not contain spaces")

        return ValidationResult(valid=len(errors) == 0, errors=errors)

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    def _read_file(self) -> WebhookConfig:
        if not _MAPPINGS_FILE.exists():
            return WebhookConfig()
        try:
            raw  = yaml.safe_load(_MAPPINGS_FILE.read_text(encoding="utf-8")) or {}
            maps = [WebhookMapping(**m) for m in raw.get("mappings", [])]
            return WebhookConfig(
                webhook_secret        = raw.get("webhook_secret", ""),
                auto_approve_low_risk = raw.get("auto_approve_low_risk", False),
                mappings              = maps,
            )
        except Exception:
            return WebhookConfig()

    def _write_file(self, cfg: WebhookConfig) -> None:
        _MAPPINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "webhook_secret":        cfg.webhook_secret,
            "auto_approve_low_risk": cfg.auto_approve_low_risk,
            "mappings": [
                {
                    "repo":                  m.repo,
                    "branch":                m.branch,
                    "deployment":            m.deployment,
                    "namespace":             m.namespace,
                    "image_prefix":          m.image_prefix,
                    "auto_approve_low_risk": m.auto_approve_low_risk,
                }
                for m in cfg.mappings
            ],
        }
        _MAPPINGS_FILE.write_text(
            yaml.dump(data, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )

    @staticmethod
    def _kubectl_check(args: list[str]) -> tuple[bool, str]:
        r = run_kubectl(args, timeout=10)
        return r.success, r.output or r.error


# Module-level singleton
_loader = MappingLoader()


def get_loader() -> MappingLoader:
    return _loader
