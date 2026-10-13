"""The code_generation agent — ship a change and open a PR.

Duty (README:37): "Ship features, open PRs" / UI: "Writes code, commits to GitHub, deploys".
On demand (no beat entry), targeting `company_config.github_repo`.

Output (lane 05 `CodeGenOutput` + lane 03's commit/PR keys): `{summary, action, changes[],
branch, commits[], pr_url, pr_number, files_changed, diff_summary}`.

Two walls protect this agent (DESIGN-AGENT-RUNTIME §6.3):

  * the CLI tool layer — the session allows `Read`/`Edit`/`Write` and `git status|diff|log|branch|
    add|commit`, and denies `git push`, `git remote`, `rm`, `curl`, `wget` and every MCP tool. An
    injected instruction cannot push, even if the model believes it;
  * the action firewall — opening a pull request is `external_irreversible`, so it requires a
    single-use approval token even with sandbox mode off. In sandbox (the default) the PR is
    REFUSED (`E_SANDBOX_BLOCKED`) and recorded as a labelled draft: branch + commit plan +
    PR body, `pr_url=null`, `pr_number=null`.
"""
from __future__ import annotations

from typing import Any

from app.agents import integrations
from app.agents.base_agent import BasePolsiaAgent
from app.agents.policy_firewall import ActionClass
from app.agents.side_effects import request_side_effect, side_effect_receipt
from app.config import settings
from app.schemas.agent_outputs import CodeGenOutput

ROLE = """You are the Code Generation agent of an autonomous company. You implement small,
verifiable changes and describe them well enough to be reviewed.

Your job this run: turn the task into either a plan (when scope is unclear) or a concrete change
with a branch, a commit and a pull-request body.

Rules:
- Work in the repository named by the company context (`github_repo`). If none is configured, set
  `action="noop"` and say so in `summary` — do not invent a repo.
- Scope: one reviewable change. If the task needs more than three files of real logic, return
  `action="plan"` with the decomposition instead of a half-finished implementation.
- `pr_body` must state: what changed, why, the test or command you ran, and what you could not
  verify. A PR body without that is not reviewable.
- `tests_run` lists the exact commands you executed. If you ran none, say so explicitly rather
  than leaving the field empty without comment.
- `risk` is your honest assessment: low (docs/config/tests), medium (behaviour change behind the
  existing interfaces), high (data, money, auth, migrations, anything irreversible).
- Never touch secrets, credentials, CI configuration, billing code or the deployment pipeline.
- Never claim a command passed unless you ran it; never claim a PR exists unless the runtime
  reports that it was created.

You may read, edit and commit. You may NOT push and you may NOT open the PR yourself: the pull
request is an irreversible action routed through the policy firewall, which requires an operator
approval. While the company runs in sandbox mode the action is refused and your output is recorded
as a labelled draft (`pr_url=null`) — that is the expected outcome, not a failure."""


class CodeGenerationAgent(BasePolsiaAgent):
    agent_type = "code_generation"
    description = "Writes code and opens pull requests"
    output_schema = CodeGenOutput
    max_turns = 80
    role_prompt = ROLE

    def run(self, task: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        payload, assembled = self.collect_structured(task, context)
        payload["agent_type"] = self.agent_type
        action = str(payload.get("action") or "plan")
        payload.setdefault("commits", [])
        payload.setdefault("changes", [])
        receipts: list[dict[str, Any]] = []
        company = (context or {}).get("company") or {}
        default_repo = str(company.get("github_repo") or "")

        if action == "open_pr" or payload.get("changes"):
            for index, change in enumerate(payload.get("changes") or []):
                repo = str(change.get("repo") or default_repo or "")
                branch = str(change.get("branch") or payload.get("branch") or "")
                pr_title = str(change.get("pr_title") or "")
                if not repo or not branch or not pr_title:
                    payload.setdefault("notes", []).append(
                        f"change[{index}] missing repo/branch/pr_title; not actionable"
                    )
                    continue
                # int | None: None = "no local record of sends" -> the firewall refuses (fail
                # closed); it must never be collapsed to 0 here.
                counts = ((context or {}).get("channel_sends") or {}).get("github") or {}
                outcome = request_side_effect(
                    self.agent_type,
                    ActionClass.EXTERNAL_IRREVERSIBLE,
                    "github",
                    action="open_pr",
                    payload={"action": "open_pr", "repo": repo, "branch": branch,
                             "pr_title": pr_title},
                    sends_today=counts.get("today"),
                    sends_this_hour=counts.get("hour"),
                )
                receipt = side_effect_receipt(outcome, action="open_pr", ref=f"{repo}#{branch}")
                if outcome.allowed:                              # approval token verified
                    result = integrations.open_pull_request(
                        repo=repo, branch=branch, title=pr_title,
                        body=str(change.get("pr_body") or ""),
                    )
                    receipt["integration"] = result
                    if result.get("ok"):
                        opened = result.get("result") or {}
                        payload["pr_url"] = opened.get("url")
                        payload["pr_number"] = opened.get("number")
                    else:
                        payload.setdefault("refusals", []).append(result)
                else:
                    payload["pr_url"] = None
                    payload["pr_number"] = None
                    if outcome.sandboxed:
                        payload["sandbox"] = True
                    payload["drafted_not_sent"] = True
                    if outcome.requires_approval:
                        payload.setdefault("refusals", []).append({
                            "ok": False,
                            "error": {"code": "E_POLICY_DENIED",
                                      "message": f"opening a PR on {repo} needs approval: {outcome.reason}",
                                      "how_to_fix": "an operator approves the pull request, then re-run",
                                      "context": {"policy_code": outcome.policy_code}},
                        })
                receipts.append(receipt)
                payload["branch"] = branch or payload.get("branch")
                payload["files_changed"] = max(
                    int(payload.get("files_changed") or 0), len(change.get("files_changed") or [])
                )

        payload["side_effects"] = receipts
        payload.setdefault("refusals", [])
        if settings.sandbox_mode:
            payload["sandbox"] = True
            payload["drafted_not_sent"] = True
            payload["pr_url"] = None
            payload["pr_number"] = None
            for commit in payload.get("commits") or []:
                commit.setdefault("sha", None)
        payload["action"] = action
        return self.attach_run_meta(payload, assembled=assembled)


__all__ = ["ROLE", "CodeGenerationAgent"]
