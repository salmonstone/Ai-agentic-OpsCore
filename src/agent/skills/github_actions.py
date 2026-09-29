"""
GitHub Actions skill — diagnose why a workflow run failed.

Same shape as the Jenkins skill: gather the failed jobs and the tail of
their logs, ask Claude for a structured root cause, remember the result.
Read-only: it never re-runs or changes anything.
"""
from __future__ import annotations

from agent.core import context, llm
from agent.core.async_utils import run_sync
from agent.core.models import GitHubActionsDiagnosis
from agent.core.parsing import parse_llm_json
from agent.integrations import github_actions as gha
from agent.memory.retrieval import remember, retrieve_context
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill

log = get_logger(__name__)

_MAX_JOB_LOGS = 3            # a matrix run can fail dozens of jobs; the first few say enough
_LOG_TAIL_PER_JOB = 4000

_CATEGORIES = {"test_failure", "build_error", "dependency", "lint", "config",
               "infra_flaky", "timeout", "unknown"}

_DIAGNOSE_SYSTEM = """You are a senior CI engineer diagnosing a failed GitHub Actions workflow run.
Return ONLY valid JSON, no markdown:
{
  "category": "test_failure" | "build_error" | "dependency" | "lint" | "config" | "infra_flaky" | "timeout" | "unknown",
  "root_cause": "one sentence: what actually broke, naming the file/test/step when the log shows it",
  "confidence": "high" | "medium" | "low",
  "suggested_fix": "the concrete change to make (code, config, or workflow YAML)",
  "explanation": "2-3 sentences of supporting evidence from the log",
  "rerun_likely_helps": true | false
}
rerun_likely_helps is true ONLY for transient causes (network blips, runner/infra outages,
registry rate limits, known-flaky tests). A real code, test, lint or config error is false.
If the log doesn't show the cause, say so with confidence "low" rather than guessing."""


class GitHubActionsSkill(BaseSkill):
    @property
    def name(self) -> str:
        return "github_actions"

    @property
    def description(self) -> str:
        return "Diagnose failed GitHub Actions workflow runs. Read-only."

    def execute(self, input_data: dict) -> dict:
        return self.diagnose(input_data.get("repo"), input_data.get("run_id") or 0).model_dump()

    def diagnose(self, repo: str | None = None, run_id: int = 0) -> GitHubActionsDiagnosis:
        """Diagnose one run; run_id 0 means the repo's latest failed run."""
        repo = gha.resolve_repo(repo)
        if not run_id:
            latest = gha.latest_failed_run(repo)
            if latest is None:
                return GitHubActionsDiagnosis(repo=repo, root_cause="No failed runs found.",
                                              confidence="high")
            run = latest
        else:
            run = gha.get_run(repo, run_id)

        diag = GitHubActionsDiagnosis(
            repo=repo, run_id=run["id"] or 0, run_number=run.get("run_number") or 0,
            workflow=run.get("workflow", ""), branch=run.get("branch", ""), sha=run.get("sha", ""),
            url=run.get("url", ""), conclusion=run.get("conclusion"),
        )
        if run.get("conclusion") not in ("failure", "timed_out"):
            diag.root_cause = (f"This run didn't fail (status={run.get('status')}, "
                               f"conclusion={run.get('conclusion')}).")
            diag.confidence = "high"
            return diag

        jobs = gha.get_failed_jobs(repo, diag.run_id)
        diag.failed_jobs = [j["name"] for j in jobs]
        diag.failed_steps = [f"{j['name']} › {s}" for j in jobs for s in j["failed_steps"]]
        if not jobs:
            diag.root_cause = "The run failed but no individual job is marked failed (e.g. a workflow syntax error)."
            diag.confidence = "medium"
            diag.category = "config"
            diag.suggested_fix = "Open the run page to see the workflow-level error."
            return diag

        log_sections = []
        for j in jobs[:_MAX_JOB_LOGS]:
            try:
                text = gha.get_job_log(repo, j["id"], _LOG_TAIL_PER_JOB)
            except gha.GitHubError as e:
                text = f"(log unavailable: {e})"
            log_sections.append(f"=== JOB: {j['name']} — failed steps: "
                                f"{', '.join(j['failed_steps']) or '(none listed)'} ===\n{text}")

        past = retrieve_context(f"github actions {repo} {diag.workflow} failure")
        user_text = (
            f"Repo: {repo}\nWorkflow: {diag.workflow}  Run #{diag.run_number} (id {diag.run_id})\n"
            f"Branch: {diag.branch}  Commit: {diag.sha}  Conclusion: {diag.conclusion}\n"
            f"Failed jobs: {', '.join(diag.failed_jobs)}\n\n"
            + "\n\n".join(log_sections)
        )
        response = run_sync(llm.chat(
            messages=[context.user_message(user_text)],
            system=context.build_system_prompt(_DIAGNOSE_SYSTEM, past),
            json_mode=True,
            max_tokens=1200,
        ))
        parsed = parse_llm_json(response.content)

        category = str(parsed.get("category", "unknown")).strip().lower()
        diag.category = category if category in _CATEGORIES else "unknown"
        diag.root_cause = str(parsed.get("root_cause", "")).strip()
        confidence = str(parsed.get("confidence", "low")).strip().lower()
        diag.confidence = confidence if confidence in ("high", "medium", "low") else "low"
        diag.suggested_fix = str(parsed.get("suggested_fix", "")).strip()
        diag.explanation = str(parsed.get("explanation", "")).strip()
        diag.rerun_likely_helps = parsed.get("rerun_likely_helps") is True

        try:
            remember(
                content=(f"GitHub Actions {repo} '{diag.workflow}' #{diag.run_number} failed "
                         f"({diag.category}): {diag.root_cause}"),
                source="github_actions",
                metadata={"repo": repo, "run_id": diag.run_id, "category": diag.category},
            )
        except Exception as e:
            log.warning("github_actions.remember_failed", error=str(e))
        log.info("github_actions.diagnose.done", repo=repo, run_id=diag.run_id, category=diag.category)
        return diag
