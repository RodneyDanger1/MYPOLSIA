"""RD14 — the worker-level acceptance: an N-of-N count REFUSES the next real send.

Drives the REAL `_execute_agent_task` body on the file-backed `queue_db` (the conftest fixture the
queue tests use), so every leg is production code:

  * pre-run: `_with_channel_sends` asks the REAL reader (`channel_service.channel_sends_snapshot`)
    for this agent's channels — the snapshot the real agent's firewall consults;
  * the run: REAL `SocialMediaAgent` + REAL policy firewall (only the structured payload and the
    provider call are test doubles — the mock CLI carries no payload, and no network is allowed);
  * post-run: `_persist_run_artifacts` -> REAL `social_service.record_published_posts` writes the
    send on the run's own transaction, which is what makes the NEXT run's count climb to the cap.

The clock is pinned (`channel_service.now_utc` — the worker's snapshot passes `now=None`;
`social_service.utcnow` — the producer's `published_at`) so "seeded 2h ago" and "the send just
happened" are deterministic in the real day/hour windows. RED leg (receipt): remove the
`_with_channel_sends` call at `agent_tasks.py:358` and these tests fail — the run sees no counts.

Measured before this lane existed: raw/probe_state_BEFORE.txt (the same loop, scratch DB).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import app.config as app_config
from app.agents.social_media.agent import SocialMediaAgent

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)
TWO_HOURS_AGO = NOW - timedelta(hours=2)


def _latest_run_output(queue_db) -> dict[str, Any]:
    """The last run row's stored output, read from the FILE (stdlib driver, no ORM)."""
    rows = queue_db.query("SELECT output FROM agent_runs ORDER BY id DESC LIMIT 1")
    if not rows or not rows[0]["output"]:
        return {}
    return json.loads(rows[0]["output"])


def _pin_clock(monkeypatch, holder: dict[str, datetime]) -> None:
    """Deterministic windows: the worker's snapshot passes `now=None`, the producer stamps `utcnow`."""
    monkeypatch.setattr("app.services.channel_service.now_utc", lambda: NOW)
    monkeypatch.setattr("app.services.social_service.utcnow", lambda: holder["now"])


def _seed_published(queue_db, count: int) -> dict[str, Any]:
    """Seed `count` published rows through the REAL producer, on the queue database."""
    from app.services.social_service import record_published_posts

    async def _seed() -> dict[str, Any]:
        async with queue_db.factory() as db:
            report = await record_published_posts(db, {"posted": [
                {"platform": "twitter", "content": f"seed {i}", "tweet_id": f"tw-{i}"}
                for i in range(count)
            ]})
            await db.commit()
            return report

    return queue_db.run(_seed())


def test_worker_counts_a_real_send_then_refuses_the_next_one_at_the_cap(
    monkeypatch, queue_db, execute_body
):
    """11 seeded sends -> run 1 completes AND is counted (12) -> run 2 is refused "12/12"."""
    monkeypatch.setattr(app_config.settings, "company_timezone", "UTC")
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    holder = {"now": TWO_HOURS_AGO}
    _pin_clock(monkeypatch, holder)

    sent: list[str] = []

    def fake_post_tweet(text: str, *, platform: str = "twitter") -> dict[str, Any]:
        sent.append(text)
        return {"ok": True, "result": {"id": f"tw-new-{len(sent)}"}}

    monkeypatch.setattr("app.agents.integrations.post_tweet", fake_post_tweet)
    monkeypatch.setattr(
        SocialMediaAgent,
        "collect_structured",
        lambda self, task, context, **kw: (
            {"action": "post", "posts": [{"platform": "twitter", "content": "worker post"}]},
            None,
        ),
    )

    report = _seed_published(queue_db, 11)
    assert report["posts_written"] == 11
    assert queue_db.count("social_posts", "status = ?", ("published",)) == 11

    # run 1 — under the cap (11/12): completes, really sends, and the worker counts it
    holder["now"] = NOW
    first = execute_body(queue_db.create_task(agent_type="social_media"))
    assert first["status"] == "completed"
    assert len(sent) == 1                                    # the send really happened
    assert queue_db.count("social_posts", "status = ?", ("published",)) == 12
    assert _latest_run_output(queue_db)["persistence"]["posts_written"] == 1

    # run 2 — the next send sees 12/12 and is refused BY THE EXISTING CAPS, before any send
    second = execute_body(queue_db.create_task(agent_type="social_media"))
    assert second["status"] == "failed"
    assert "E_RATE_LIMITED" in (second["error"] or "")
    assert "12/12" in (second["error"] or "")
    assert len(sent) == 1                                    # the refused attempt never sent
    assert queue_db.count("social_posts", "status = ?", ("published",)) == 12


def test_worker_refuses_at_the_cap_without_persisting_anything(monkeypatch, queue_db, execute_body):
    """A run that starts AT the cap: refused, no provider call, no row, run marked failed."""
    monkeypatch.setattr(app_config.settings, "company_timezone", "UTC")
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    holder = {"now": TWO_HOURS_AGO}
    _pin_clock(monkeypatch, holder)

    sent: list[str] = []
    monkeypatch.setattr(
        "app.agents.integrations.post_tweet",
        lambda text, *, platform="twitter": (sent.append(text) or {"ok": True, "result": {"id": "x"}}),
    )
    monkeypatch.setattr(
        SocialMediaAgent,
        "collect_structured",
        lambda self, task, context, **kw: (
            {"action": "post", "posts": [{"platform": "twitter", "content": "over the cap"}]},
            None,
        ),
    )

    assert _seed_published(queue_db, 12)["posts_written"] == 12

    holder["now"] = NOW
    outcome = execute_body(queue_db.create_task(agent_type="social_media"))

    assert outcome["status"] == "failed"
    assert "E_RATE_LIMITED" in (outcome["error"] or "")
    assert "12/12" in (outcome["error"] or "")
    assert sent == []                                        # nothing ever reached the provider
    assert queue_db.count("social_posts", "status = ?", ("published",)) == 12
    assert queue_db.count("activity_log", "action = ?", ("task_failed",)) == 1
