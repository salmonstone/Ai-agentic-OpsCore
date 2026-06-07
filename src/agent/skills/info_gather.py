"""
InfoGatherSkill — collect a full OS/environment context snapshot and
optionally ask Claude to analyze it.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from agent.core import llm
from agent.integrations.system_collector import SYSTEM_COLLECTORS, collect_all_system
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill

log = get_logger(__name__)

_SYSTEM_PROMPT = """\
You are an agentic OS context analyzer. You receive a structured snapshot of the \
user's local environment: OS info, installed tools, Kubernetes context, AWS config, \
Gmail setup, Git repos, Docker, and Python packages.

Your job:
1. Summarize the overall environment in 2-3 sentences — what kind of setup this is \
   (dev machine, CI server, cloud VM, etc.) and what the agent can do.
2. Call out anything that looks broken, missing, or worth the user's attention \
   (e.g. tool not found, no K8s context, daemon not running).
3. List the top 3 capabilities the agentic OS can use right now.

Keep the analysis under 200 words. Be direct — no preamble.
"""


class InfoGatherSkill(BaseSkill):
    @property
    def name(self) -> str:
        return "info-gather"

    @property
    def description(self) -> str:
        return "Collect a full environment snapshot and optionally analyze it with Claude."

    def execute(self, input_data: dict) -> dict:
        areas    = input_data.get("areas")          # None = all
        analyze  = input_data.get("analyze", False)  # call Claude?

        results, elapsed_ms = collect_all_system(areas)

        ok_areas     = [k for k, v in results.items() if v.get("ok")]
        failed_areas = [k for k, v in results.items() if not v.get("ok")]

        # Build a flat text snapshot for Claude (if analyze=True)
        analysis: str | None = None
        if analyze:
            lines = ["=== ENVIRONMENT SNAPSHOT ===", ""]
            for area, res in results.items():
                lines.append(f"[{area.upper()}]")
                lines.append(res.get("summary", "(no summary)"))
                if res.get("ok") and res.get("data"):
                    try:
                        lines.append(json.dumps(res["data"], indent=2)[:600])
                    except Exception:
                        pass
                lines.append("")

            snapshot_text = "\n".join(lines)

            try:
                llm_resp = asyncio.run(llm.chat(
                    messages=[{"role": "user", "content": snapshot_text}],
                    system=_SYSTEM_PROMPT,
                    max_tokens=400,
                ))
                analysis = llm_resp.content
            except Exception as exc:
                log.warning("info_gather.analyze_failed", error=str(exc))
                analysis = f"(Analysis failed: {exc})"

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "collection_ms": elapsed_ms,
            "areas_ok":     ok_areas,
            "areas_failed": failed_areas,
            "results":      results,
            "analysis":     analysis,
        }
