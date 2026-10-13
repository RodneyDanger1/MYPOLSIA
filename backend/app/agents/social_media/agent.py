"""The social_media agent — drafts, posts and replies on X/LinkedIn.

Duty (README:33): "Draft + post tweets" / "monitors engagement". The only agent whose class name
is pinned by a shipped test (`test_agent_tasks.py:11` patches `SocialMediaAgent.run`).

Output (lane 05 `SocialMediaOutput` + lane 03's `tweets`/`drafts`/`posted`/`replies`):
`{summary, action, posts[], tweets[], drafts[], posted[], replies[]}` feeding `social_posts`
(status default `draft`) and `social_engagements` (unique `mention_id`).

Sandbox semantics (SPEC-AGENTS §5.3): with `SANDBOX_MODE=true` — the default — every post and
reply is REFUSED by the policy firewall (`E_SANDBOX_BLOCKED`) and recorded as a labelled draft
(`sandbox: true`, `drafted_not_sent: true`), with the refusal kept in `side_effects`/`refusals`.
`posted` stays empty and no `tweet_id` is ever produced. Only when an operator sets
`SANDBOX_MODE=false` — with TWITTER_* credentials configured — does the same code path reach
`integrations.post_tweet`, and if the integration is missing it returns a named `E_INTEGRATION`
refusal rather than a fabricated id.
"""
from __future__ import annotations

from typing import Any

from app.agents import integrations
from app.agents.base_agent import BasePolsiaAgent
from app.agents.policy_firewall import ActionClass
from app.agents.side_effects import (
    assert_not_reporting_real_side_effect,
    request_side_effect,
    side_effect_receipt,
)
from app.config import settings
from app.schemas.agent_outputs import SocialMediaOutput

ROLE = """You are the Social Media agent of an autonomous company: you run the company's public
voice on X and LinkedIn.

Your job this run: decide what is worth saying today, write it, and reply to the mentions that
deserve an answer.

Voice and content rules:
- Every post must be specific to THIS company: its product, its market, its evidence. Generic
  hustle-culture filler is a failure even when it is on-brand.
- One idea per post. Lead with the concrete claim or the number, not with a greeting. No hashtag
  spam (0-3, only if they are the ones the audience actually follows), no emoji strings.
- X posts: <= 280 characters. LinkedIn posts: <= 1000 characters. Count them before you answer.
- Never invent customer names, metrics, launches or quotes. If you have no evidence worth posting,
  say so in `summary` and return `action="noop"` with no posts — a silent day beats a lie.
- Never promise a feature, a discount or a date the company has not committed to.

Replies: answer the mention in the mention's own register. If a mention is hostile, be factual and
short; if it is a real complaint, do not argue publicly — say in `summary` that support should own it.

Set `action` to what this run is for: "draft" (write posts, publish nothing yet), "post" (publish),
"reply" (answer mentions) or "noop". You may return up to 3 posts. Every externally visible action
is routed through the company's policy firewall: if it is refused you will be told so, and your
output is recorded as a labelled draft, not as something that went out. Never claim a post was
published unless the runtime reports that it was."""


class SocialMediaAgent(BasePolsiaAgent):
    agent_type = "social_media"
    description = "Drafts and posts tweets, monitors engagement"
    output_schema = SocialMediaOutput
    max_turns = 30
    role_prompt = ROLE

    # ------------------------------------------------------------------ side effects
    def _channel_counts(self, context: dict[str, Any], channel: str) -> tuple[int | None, int | None]:
        """The channel's send counts, passed through raw — `int | None`.

        `None` is "no local record of sends" (A2 F-04: the counts come from the DB snapshot, and
        a channel with no counting source has none): the firewall must see the unknown and
        refuses a capped channel it cannot verify (fail closed). The old `int(x or 0)` collapsed
        "not measured" into "measured zero", which is exactly what made the caps unfireable;
        never re-introduce that coercion here.
        """
        counts = ((context or {}).get("channel_sends") or {}).get(channel) or {}
        return counts.get("today"), counts.get("hour")

    def _publish(
        self,
        payload: dict[str, Any],
        *,
        action: str,
        ref: str,
        platform: str,
        content: str,
        context: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Attempt one external action through the firewall.

        Returns `(audit_receipt, published_item_or_None)`. `published_item` exists ONLY when the
        firewall allowed the action AND the provider integration succeeded — the single path by
        which a real id can enter the output.
        """
        sends_today, sends_hour = self._channel_counts(context, platform)
        outcome = request_side_effect(
            self.agent_type,
            ActionClass.EXTERNAL_REVERSIBLE,
            platform,
            action=action,
            payload={"action": action, "content": content[:200], "ref": ref},
            sends_today=sends_today,
            sends_this_hour=sends_hour,
        )
        receipt: dict[str, Any] = side_effect_receipt(outcome, action=action, ref=ref,
                                                      extra={"platform": platform})
        if outcome.allowed:                                     # only reachable outside sandbox
            result = integrations.post_tweet(content, platform=platform)
            receipt["integration"] = result
            if result.get("ok"):
                published = result.get("result") or {}
                receipt["provider_id"] = published.get("id")
                return receipt, {"content": content[:1000], "platform": platform,
                                 "tweet_id": published.get("id")}
            payload.setdefault("refusals", []).append(result)
        elif outcome.sandboxed:
            payload["sandbox"] = True
            payload["drafted_not_sent"] = True
        elif outcome.requires_approval:
            payload["drafted_not_sent"] = True
            payload.setdefault("refusals", []).append({
                "ok": False,
                "error": {"code": "E_POLICY_DENIED", "message": outcome.reason,
                          "how_to_fix": "an operator must approve this action",
                          "context": {"policy_code": outcome.policy_code}},
            })
        return receipt, None

    def run(self, task: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        payload, assembled = self.collect_structured(task, context)
        payload["agent_type"] = self.agent_type
        action = str(payload.get("action") or "draft")
        posts = list(payload.get("posts") or [])
        replies = list(payload.get("replies") or [])
        receipts: list[dict[str, Any]] = []
        published: list[dict[str, Any]] = []

        for index, post in enumerate(posts):
            platform = str(post.get("platform") or "twitter")
            content = str(post.get("content") or "")
            receipt, published_item = self._publish(
                payload, action=action, ref=f"post:{index}", platform=platform,
                content=content, context=context,
            )
            receipts.append(receipt)
            if published_item is not None:
                published.append(published_item)

        for index, reply in enumerate(replies):
            mention_id = str(reply.get("mention_id") or f"mention:{index}")
            receipt, _ = self._publish(
                payload, action="reply", ref=mention_id, platform="twitter",
                content=str(reply.get("reply") or ""), context=context,
            )
            receipts.append(receipt)
            reply["reply_id"] = reply.get("reply_id") if not settings.sandbox_mode else None

        # The specced `tweets` key mirrors what was written, sandboxed or not.
        if not payload.get("tweets"):
            payload["tweets"] = [str(p.get("content") or "") for p in posts][:10]
        if not payload.get("drafts"):
            payload["drafts"] = list(payload["tweets"])[:10]
        payload["posted"] = published
        payload["side_effects"] = receipts
        payload.setdefault("refusals", [])
        first_posted = (published or [{}])[0]
        assert_not_reporting_real_side_effect(
            first_posted, agent_type=self.agent_type, keys=("tweet_id", "post_id")
        )
        if settings.sandbox_mode:
            payload["sandbox"] = True
            payload["drafted_not_sent"] = True
        elif not receipts:
            payload.setdefault("notes", []).append("no external action was attempted this run")
        payload["action"] = action
        return self.attach_run_meta(payload, assembled=assembled)


__all__ = ["ROLE", "SocialMediaAgent"]
