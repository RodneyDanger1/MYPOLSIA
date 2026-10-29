"""F-21 (wave 3) — a platform limit is a constraint, not a failed run.

Measured defect (VISUALS-2 F-21; live task 313 / run 310): a NORMAL caption brief produced a
`posts[]` item whose twitter content was longer than 280 characters.  The schema refused the WHOLE
payload (`E_AGENT_BAD_OUTPUT … twitter content must be <= 280 chars`), that raw Pydantic text
reached the activity feed and the task row, and the product spent a retry for copy the model had
already written.

The limit is a platform fact, not a bad answer.  Pinned here:

  * a generation-time repair fits the caption to the limit at a WORD boundary and the run
    SUCCEEDS — one provider call, no `E_AGENT_BAD_OUTPUT`, no retry;
  * the repair is RECORDED, never silent: the payload's own `notes` states the original length and
    `repaired_by` names the repair;
  * the schema validator stays STRICT — an over-limit caption from any other path is still refused
    (`SocialMediaOutput` keeps refusing >280 on a twitter post);
  * a caption inside the limit is untouched.
"""
from __future__ import annotations

import json

import pytest

from app.agents import base_agent as base_agent_module
from app.agents.social_media.agent import SocialMediaAgent, fit_twitter_content
from app.schemas.agent_outputs import (
    TWITTER_MAX_CHARS,
    SocialMediaOutput,
    SocialPostDraft,
)

#: The measured overflow shape: a 340+ character launch caption (the live failure was a "~60-word
#: brand blurb").  Must stay over the limit — asserted, so the fixture cannot silently shrink.
OVER_LONG = (
    "Our Fireside candle is poured by hand in small batches, one wick at a time, and it is the "
    "smell of a cabin in October: smoked cedar, a little clove, and the quiet sweetness of pine "
    "resin that has been warming on a wood stove for an hour while the rain moves in over the "
    "ridge behind the house where the two of us started pouring these in a rented kitchen."
)


def _payload(content: str) -> dict:
    return {
        "summary": "drafted one launch caption",
        "status": "ok",
        "action": "draft",
        "posts": [{"platform": "twitter", "content": content, "hashtags": ["candles"]}],
        "notes": [],
    }


def _patch_real_call(monkeypatch, payload: dict) -> list[str]:
    """Drive the REAL structured path with a fake provider call; return the prompts sent."""
    monkeypatch.setattr(base_agent_module, "is_mock_mode", lambda: False)
    sent: list[str] = []

    def fake_real_call(prompt, *, json_schema=None, system_prompt_static="", resume=False,
                       tool_less=False):
        sent.append(prompt)
        return base_agent_module._RawCall(
            text=json.dumps(payload),
            envelope={"result": json.dumps(payload)},
            exit_code=0,
            attempts=1,
        )

    agent = SocialMediaAgent()
    monkeypatch.setattr(agent, "_real_call", fake_real_call)
    return agent, sent  # type: ignore[return-value]


def _run(agent, sent: list[str]) -> dict:
    payload, _assembled = agent.collect_structured(
        {"title": "Provisional brand copy", "description": "one launch caption"},
        {"company": {"name": "Hearth & Lather"}},
    )
    return payload


def test_an_over_long_twitter_caption_is_fitted_and_the_run_succeeds(monkeypatch) -> None:
    """RED-1: before the repair this raises E_AGENT_BAD_OUTPUT and nothing is stored."""
    assert len(OVER_LONG) > TWITTER_MAX_CHARS, "the fixture must really overflow the limit"
    agent, sent = _patch_real_call(monkeypatch, _payload(OVER_LONG))

    payload = _run(agent, sent)  # must NOT raise

    post = payload["posts"][0]
    assert post["platform"] == "twitter"
    assert 1 <= len(post["content"]) <= TWITTER_MAX_CHARS, len(post["content"])
    assert post["content"].startswith("Our Fireside candle"), "the surviving copy is the model's own"
    assert len(sent) == 1, f"the repair must not cost a second provider call, sent {len(sent)}"


def test_the_repair_is_recorded_in_the_payload_notes_and_in_repaired_by(monkeypatch) -> None:
    """A silent cut would be a new lie; the note names the original length and the limit."""
    agent, sent = _patch_real_call(monkeypatch, _payload(OVER_LONG))

    payload = _run(agent, sent)

    joined = " ".join(payload.get("notes") or [])
    assert str(len(OVER_LONG)) in joined, joined
    assert str(TWITTER_MAX_CHARS) in joined, joined
    assert "word boundary" in joined, joined
    assert agent.last_repair is not None and "social_platform_fit" in agent.last_repair, agent.last_repair
    assert "social_platform_fit" in agent.usage_metadata()["repaired_by"]


def test_a_caption_inside_the_limit_is_untouched(monkeypatch) -> None:
    """No repair on a normal caption: the payload and `repaired_by` stay exactly as before."""
    short = "Poured by hand, one wick at a time. The Fireside candle, back in stock Friday."
    assert len(short) <= TWITTER_MAX_CHARS
    agent, sent = _patch_real_call(monkeypatch, _payload(short))

    payload = _run(agent, sent)

    assert payload["posts"][0]["content"] == short
    assert payload.get("notes") == []
    assert "social_platform_fit" not in agent.usage_metadata()["repaired_by"]


def test_the_schema_still_refuses_an_over_limit_caption_from_another_path() -> None:
    """The validator is NOT relaxed: the repair happened at generation time, before it ran."""
    with pytest.raises(Exception) as exc:
        SocialMediaOutput.model_validate(_payload(OVER_LONG))
    assert "280" in str(exc.value), str(exc.value)
    # And the item model still carries the platform's own bound for a direct construction path.
    assert SocialPostDraft(platform="twitter", content="x" * 280).content == "x" * 280


def test_the_fit_cuts_on_a_word_boundary_and_marks_the_cut() -> None:
    fitted = fit_twitter_content(OVER_LONG, TWITTER_MAX_CHARS)
    assert len(fitted) <= TWITTER_MAX_CHARS
    assert fitted.endswith("…"), "the cut must be visible, not implied"
    body = fitted[:-1]
    assert OVER_LONG.startswith(body), "the fitted copy keeps the model's leading text"
    assert not body.endswith(" "), "trailing whitespace before the ellipsis is stripped"
    # A word boundary: the removed tail starts a NEW word (the last kept character is not a
    # letter glued onto a word that continues in the removed part).
    removed = OVER_LONG[len(body):]
    assert removed[:1] in (" ", ",") or body.endswith(("'",)), (body[-20:], removed[:20])


def test_a_caption_inside_the_limit_is_returned_unchanged_by_the_helper() -> None:
    assert fit_twitter_content("short", TWITTER_MAX_CHARS) == "short"
    exact = "y" * TWITTER_MAX_CHARS
    assert fit_twitter_content(exact, TWITTER_MAX_CHARS) == exact
