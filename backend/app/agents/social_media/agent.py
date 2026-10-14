"""The social_media agent — drafts, posts and replies on the company's social platforms
(X, LinkedIn, Facebook, Instagram, TikTok, YouTube).

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

Run-action semantics (L3-01): the run's own `action` field is the consent record the ROLE prompt
sells — `"draft"` means "write posts, publish nothing yet" and `"noop"` means nothing worth
saying; only `"post"` attempts the posts and only `"post"`/`"reply"` attempt the replies. A
disclaimed run performs no firewall call and consumes no cap slot. The firewall, the channel caps
(in-run `performed` counter included, L3-05) and the taint machinery all stay in the path for the
actions that ARE claimed.
"""
from __future__ import annotations

from typing import Any

from app.agents import integrations
from app.agents.base_agent import BasePolsiaAgent
from app.agents.policy_firewall import ActionClass
from app.agents.side_effects import (
    assert_not_reporting_real_side_effect,
    capped_item_refusal,
    request_side_effect,
    side_effect_receipt,
)
from app.config import settings
from app.core.errors import E_RATE_LIMITED, PolsiaError
from app.schemas.agent_outputs import SocialMediaOutput

ROLE = """You are the Social Media agent of an autonomous company: you run the company's public
voice on X, LinkedIn, Facebook, Instagram, TikTok and YouTube.

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
        performed: int = 0,
        image_url: str = "",
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Attempt one external action through the firewall.

        Returns `(audit_receipt, published_item_or_None)`. `published_item` exists ONLY when the
        firewall allowed the action AND the provider integration succeeded — the single path by
        which a real id can enter the output.

        `performed` is what THIS run has already really performed on this channel (L3-05): the
        channel caps are check-and-consume and the snapshot in `context` is read ONCE per run, so
        every item used to see the same stale count and one batch could overshoot a cap by its
        whole ceiling. `None` (no local record) stays `None` — unknown must reach the firewall as
        unknown so it refuses by name (fail closed).
        """
        sends_today, sends_hour = self._channel_counts(context, platform)
        sends_today_now = None if sends_today is None else sends_today + performed
        sends_hour_now = None if sends_hour is None else sends_hour + performed
        try:
            outcome = request_side_effect(
                self.agent_type,
                ActionClass.EXTERNAL_REVERSIBLE,
                platform,
                action=action,
                payload={"action": action, "content": content[:200], "ref": ref},
                sends_today=sends_today_now,
                sends_this_hour=sends_hour_now,
            )
        except PolsiaError as exc:
            # A cap denial with NOTHING performed this run stays fatal (the run should not have
            # been dispatched into a capped channel); a cap denial AFTER this run performed a
            # real send is recorded and the item stays a draft — see `capped_item_refusal` for
            # why (the performed work must persist; the retryable refusal must not re-run the
            # batch). Anything that is not a cap denial re-raises unchanged.
            if exc.code != E_RATE_LIMITED or performed == 0:
                raise
            capped_receipt, refusal = capped_item_refusal(
                exc, agent_type=self.agent_type, action=action, ref=ref, channel=platform,
            )
            payload.setdefault("refusals", []).append(refusal)
            payload["drafted_not_sent"] = True
            return capped_receipt, None
        receipt: dict[str, Any] = side_effect_receipt(outcome, action=action, ref=ref,
                                                      extra={"platform": platform})
        if outcome.allowed:                                     # only reachable outside sandbox
            # U5/P1: media rides ONLY when the item carries it, so a text post keeps the exact
            # `post_tweet(content, platform=...)` call shape every caller and test double mirrors;
            # instagram requires the field and its adapter refuses by name without it.
            media = {"image_url": image_url} if image_url else {}
            result = integrations.post_tweet(content, platform=platform, **media)
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
        published_replies: list[dict[str, Any]] = []
        # L3-05: what THIS run has really performed, per channel. Incremented only after a
        # performed AND successful provider call; `None` snapshots stay `None`.
        performed_by_channel: dict[str, int] = {}
        # L3-01: the run's own `action` is the consent record the ROLE prompt sells ("draft" =
        # write posts, publish nothing yet; "noop" = nothing worth saying) and the schema encodes
        # (`SocialMediaOutput.action`). Dispatch on it: a disclaimed run attempts NOTHING (no
        # firewall call, no cap slot), while "post"/"reply" still traverses the firewall, the
        # channel caps and the taint machinery exactly as before.
        will_publish_replies = action in ("post", "reply")

        for index, post in enumerate(posts if action == "post" else []):
            platform = str(post.get("platform") or "twitter")
            content = str(post.get("content") or "")
            # U5/P1: instagram needs a media URL; the optional field rides through only when the
            # item carries one (text posts keep the unchanged call shape).
            image_url = str(post.get("image_url") or "")
            receipt, published_item = self._publish(
                payload, action=action, ref=f"post:{index}", platform=platform,
                content=content, context=context,
                performed=performed_by_channel.get(platform, 0),
                image_url=image_url,
            )
            receipts.append(receipt)
            if published_item is not None:
                published.append(published_item)
                performed_by_channel[platform] = performed_by_channel.get(platform, 0) + 1

        for index, reply in enumerate(replies):
            if not will_publish_replies:
                # L3-01/L3-07: this run disclaimed the reply ("draft"/"noop") — nothing was
                # attempted, and the runtime-owned `reply_id` stays None (it must never carry
                # the model's invented string; the schema comment promises None while unwritten).
                reply["reply_id"] = None
                continue
            mention_id = str(reply.get("mention_id") or f"mention:{index}")
            receipt, published_item = self._publish(
                payload, action="reply", ref=mention_id, platform="twitter",
                content=str(reply.get("reply") or ""), context=context,
                performed=performed_by_channel.get("twitter", 0),
            )
            receipts.append(receipt)
            if published_item is not None:
                performed_by_channel["twitter"] = performed_by_channel.get("twitter", 0) + 1
                published_replies.append(published_item)
            # L3-07: `reply_id` carries the PROVIDER's id for the created reply (the schema's own
            # comment says so). The old line kept whatever the model wrote — a platform id no
            # platform ever issued, in a payload that reaches `agent_runs.output` — while the
            # provider's id (already returned by `_publish`) was discarded. Fill it from the
            # published item, or None when nothing was published (sandbox / refusal / failure).
            reply["reply_id"] = None if settings.sandbox_mode else (published_item or {}).get("tweet_id")

        # The specced `tweets` key mirrors what was written, sandboxed or not.
        if not payload.get("tweets"):
            payload["tweets"] = [str(p.get("content") or "") for p in posts][:10]
        if not payload.get("drafts"):
            payload["drafts"] = list(payload["tweets"])[:10]
        payload["posted"] = published
        payload["side_effects"] = receipts
        payload.setdefault("refusals", [])
        # L3-07: the sandbox guard runs over EVERY published item — posts AND replies — not just
        # `posts[0]`. A fabricated reply id was structurally invisible to the old call.
        for item in [*published, *published_replies]:
            assert_not_reporting_real_side_effect(
                item, agent_type=self.agent_type, keys=("tweet_id", "post_id")
            )
        if settings.sandbox_mode:
            payload["sandbox"] = True
            payload["drafted_not_sent"] = True
        elif not receipts:
            # L3-01: a draft/noop run disclaimed every external action — nothing was attempted,
            # and the payload says so (the sandbox branch's marker, for the gate's branch).
            payload["drafted_not_sent"] = True
            payload.setdefault("notes", []).append("no external action was attempted this run")
        payload["action"] = action
        return self.attach_run_meta(payload, assembled=assembled)


__all__ = ["ROLE", "SocialMediaAgent"]
