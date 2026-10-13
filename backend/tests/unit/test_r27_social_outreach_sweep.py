"""R27 — the social/outreach SERVICES sweep: every real branch of the two cap producers.

Lane R27 (seat-owned files: `app/services/social_service.py`, `app/services/outreach_service.py`,
`app/api/v1/social.py`, `app/api/v1/outreach.py`; see build/R27-social-outreach/PLAN.md for the
read-verified anchors). The two `record_*` functions are the ONLY writers of the rows
`channel_service` counts the per-channel send caps against (twitter/linkedin from `social_posts`
status='published'; email from `email_logs` status='sent'), so their disciplines are load-bearing:

  * provider-id discipline — a row may exist ONLY for an action the provider really confirmed
    (a "posted" item without `tweet_id`, a "sent" item without `sendgrid_id`, is NOT a send and
    is never counted; RD14's defect class was a non-unique dedupe key collapsing distinct sends
    onto one row, which made the email cap unfireable);
  * dedupe identity — `(platform, tweet_id)` for social, `sendgrid_id` for email; a retried run
    cannot double-count;
  * never raises — any failure comes back in the report's `error` field (the worker folds it into
    the run row), so a broken read can never look like "nothing to persist";
  * refusal honesty — malformed input is a named `E_VALIDATION`/`E_NOT_FOUND` refusal, never a
    silent `None`/`[]`.

Doubles live only here: the CLI seam (`collect_structured` — the mock CLI carries no structured
payload), the provider SDKs (fake `sendgrid` module mirroring the real 202 + X-Message-Id shape;
a recording `post_tweet`), and the clock (fixed anchor/pinned `utcnow`, the repo's own pattern).
Every DB row is written by the REAL function under test on the real (sqlite) schema.
"""
from __future__ import annotations

import sys
import types
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

import app.config as app_config
from app.agents import integrations
from app.agents.email_outreach.agent import EmailOutreachAgent
from app.agents.social_media.agent import SocialMediaAgent
from app.core.errors import E_DB, E_NOT_FOUND, E_RATE_LIMITED, E_VALIDATION, PolsiaError
from app.models.outreach import EmailLog, Prospect
from app.models.social import SocialEngagement, SocialPost
from app.services import outreach_service, social_service
from app.services.channel_service import channel_sends_snapshot

# A fixed mid-day anchor, exactly the pattern test_channel_sends/test_send_cap_producer_e2e use:
# the company-local day and the trailing hour are deterministic and producer writes are pinned to it.
NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)
TWO_HOURS_AGO = NOW - timedelta(hours=2)      # inside the day window, outside the hour window


# ── helpers ───────────────────────────────────────────────────────────────────────────────────

class _Raiser:
    """A callable that raises `exc` — for forcing a real read/write failure path."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise self.exc


def _break_execute(monkeypatch: pytest.MonkeyPatch, db: Any) -> None:
    """Make every `db.execute` fail like a dead driver — the failure the refusals must name."""
    monkeypatch.setattr(db, "execute", _Raiser(SQLAlchemyError("simulated driver failure")))


async def _count(db: Any, model: Any) -> int:
    return int((await db.execute(select(func.count()).select_from(model))).scalar())


def _stub_collection(agent: Any, payload: dict[str, Any]) -> Any:
    """Hand the agent the payload its CLI collection would have produced (mock CLI carries none)."""
    agent.collect_structured = lambda task, context, **kw: (dict(payload), None)
    return agent


class _FakeSendGrid:
    """Provider double: 202 + a UNIQUE `X-Message-Id` per send (the real API's shape).

    `headers={}` in the no-id variant is the RD14 residual's trigger (see the KNOWN-RESIDUAL test).
    """

    def __init__(self, *, send_ids: bool = True) -> None:
        self.sent_message_ids: list[str] = []
        self._send_ids = send_ids

    def install(self, monkeypatch: pytest.MonkeyPatch) -> _FakeSendGrid:
        fake = self

        class _Response:
            def __init__(self, message_id: str) -> None:
                self.status_code = 202
                self.headers = {"X-Message-Id": message_id} if fake._send_ids else {}

        class _Client:
            def __init__(self, api_key: str) -> None:
                self.api_key = api_key

            def send(self, message: Any) -> _Response:
                message_id = f"sg-msg-{len(fake.sent_message_ids) + 1}"
                fake.sent_message_ids.append(message_id)
                return _Response(message_id)

        sendgrid_mod = types.ModuleType("sendgrid")
        helpers = types.ModuleType("sendgrid.helpers")
        mail_mod = types.ModuleType("sendgrid.helpers.mail")
        sendgrid_mod.SendGridAPIClient = _Client           # type: ignore[attr-defined]
        mail_mod.Mail = lambda **kw: {"mail": kw}           # type: ignore[attr-defined]
        helpers.mail = mail_mod                             # type: ignore[attr-defined]
        sendgrid_mod.helpers = helpers                      # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "sendgrid", sendgrid_mod)
        monkeypatch.setitem(sys.modules, "sendgrid.helpers", helpers)
        monkeypatch.setitem(sys.modules, "sendgrid.helpers.mail", mail_mod)
        return fake


def _email_payload(index: int) -> dict[str, Any]:
    return {"action": "send", "emails": [{
        "prospect_email": f"prospect{index}@example.com",
        "subject": f"s{index}", "body": f"b{index}",
    }]}


def _outreach_email(index: int, sendgrid_id: str) -> dict[str, Any]:
    return {"status": "sent", "sendgrid_id": sendgrid_id,
            "prospect_email": f"prospect{index}@example.com",
            "subject": f"s{index}", "body": f"b{index}"}


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# social_service
# ═══════════════════════════════════════════════════════════════════════════════════════════════

async def test_list_posts_empty_state_is_an_empty_list(async_db_session):
    """The honest empty state: `[]` from a READ, not from a swallowed failure (that is the E_DB test)."""
    assert await social_service.list_posts(async_db_session) == []


async def test_list_posts_status_and_platform_filters_are_exact(async_db_session):
    db = async_db_session
    db.add_all([
        SocialPost(platform="twitter", content="t-draft", status="draft"),
        SocialPost(platform="twitter", content="t-pub", status="published"),
        SocialPost(platform="linkedin", content="l-pub", status="published"),
    ])
    await db.flush()

    by_status = await social_service.list_posts(db, status="published")
    assert {p.content for p in by_status} == {"t-pub", "l-pub"}
    by_platform = await social_service.list_posts(db, platform="twitter")
    assert {p.content for p in by_platform} == {"t-draft", "t-pub"}
    both = await social_service.list_posts(db, status="published", platform="twitter")
    assert [p.content for p in both] == ["t-pub"]
    assert await social_service.list_posts(db, status="archived") == []


async def test_list_posts_orders_newest_first_with_the_id_tiebreak(async_db_session):
    """REQUESTS #3: `(created_at DESC, id DESC)` — same-second rows cannot reorder the feed."""
    db = async_db_session
    same_second = NOW - timedelta(days=1)
    db.add_all([
        SocialPost(platform="twitter", content="old", status="draft", created_at=same_second),
        SocialPost(platform="twitter", content="old-but-newer-id", status="draft",
                   created_at=same_second),
        SocialPost(platform="twitter", content="newest", status="draft", created_at=NOW),
    ])
    await db.flush()

    order = [p.content for p in await social_service.list_posts(db)]
    assert order == ["newest", "old-but-newer-id", "old"]


async def test_list_posts_limit_is_literal_and_one_returns_the_newest(async_db_session):
    """The route guards `ge=1, le=500`; at the service level the limit is applied verbatim."""
    db = async_db_session
    db.add_all([
        SocialPost(platform="twitter", content=f"p{i}", status="draft",
                   created_at=NOW - timedelta(minutes=i))
        for i in range(3)
    ])
    await db.flush()

    assert [p.content for p in await social_service.list_posts(db, limit=1)] == ["p0"]
    assert len(await social_service.list_posts(db, limit=2)) == 2
    assert await social_service.list_posts(db, limit=0) == []      # documented: SQL LIMIT 0
    assert len(await social_service.list_posts(db, limit=500)) == 3


async def test_list_posts_read_failure_is_a_named_E_DB_refusal(async_db_session, monkeypatch):
    """A dead read must NEVER come back as `[]` (an empty list that reads like "no posts")."""
    _break_execute(monkeypatch, async_db_session)
    with pytest.raises(PolsiaError) as excinfo:
        await social_service.list_posts(async_db_session)
    assert excinfo.value.code == E_DB
    assert "could not read social posts" in excinfo.value.message
    assert "SQLAlchemyError" in excinfo.value.message


async def test_create_post_refuses_blank_content_and_writes_nothing(async_db_session):
    db = async_db_session
    for blank in ("", "   ", "\n\t "):
        with pytest.raises(PolsiaError) as excinfo:
            await social_service.create_post(db, blank)
        assert excinfo.value.code == E_VALIDATION
        assert excinfo.value.message == "post content must not be empty"
        assert excinfo.value.context == {"platform": "twitter"}
        assert excinfo.value.how_to_fix                       # a refusal says how to fix it
    assert await _count(db, SocialPost) == 0


async def test_create_post_persists_defaults_and_supplied_fields(async_db_session):
    db = async_db_session
    scheduled = NOW + timedelta(days=1)

    defaulted = await social_service.create_post(db, "defaults")
    assert (defaulted.platform, defaulted.status) == ("twitter", "draft")
    assert defaulted.id is not None                          # flush ran (the worker owns the commit)

    explicit = await social_service.create_post(
        db, "  spaced but kept verbatim  ", platform="linkedin", status="scheduled",
        scheduled_for=scheduled, engagement={"likes": 0},
    )
    assert explicit.content == "  spaced but kept verbatim  "   # only EMPTINESS is rejected
    assert explicit.platform == "linkedin" and explicit.status == "scheduled"
    assert explicit.scheduled_for is not None and explicit.engagement == {"likes": 0}

    reloaded = await social_service.get_post(db, explicit.id)
    assert reloaded is not None and reloaded.content == explicit.content


async def test_get_post_returns_none_only_for_a_missing_row(async_db_session):
    db = async_db_session
    post = await social_service.create_post(db, "find me")
    found = await social_service.get_post(db, post.id)
    assert found is not None and found.id == post.id
    assert await social_service.get_post(db, 999_999) is None    # a lookup, not a refusal


async def test_post_status_transition_draft_to_published(async_db_session, monkeypatch):
    db = async_db_session
    monkeypatch.setattr(social_service, "utcnow", lambda: NOW)
    post = await social_service.create_post(db, "transition")

    marked = await social_service.mark_published(db, post.id, tweet_id="tw-transition")
    assert marked.status == "published" and marked.tweet_id == "tw-transition"
    assert marked.published_at is not None

    published = await social_service.list_posts(db, status="published")
    assert [p.id for p in published] == [post.id]


async def test_mark_published_refuses_an_unknown_id_with_E_NOT_FOUND(async_db_session):
    with pytest.raises(PolsiaError) as excinfo:
        await social_service.mark_published(async_db_session, 4242, tweet_id="tw-x")
    assert excinfo.value.code == E_NOT_FOUND
    assert excinfo.value.message == "social post 4242 does not exist"
    assert excinfo.value.context == {"post_id": 4242}


async def test_mark_published_keeps_an_explicit_published_at(async_db_session):
    db = async_db_session
    post = await social_service.create_post(db, "dated")
    explicit = NOW - timedelta(hours=3)
    marked = await social_service.mark_published(db, post.id, tweet_id="tw-dated",
                                                 published_at=explicit)
    assert marked.published_at == explicit
    assert marked.status == "published"


async def test_list_engagements_empty_and_limit(async_db_session):
    db = async_db_session
    assert await social_service.list_engagements(db) == []
    db.add_all([
        SocialEngagement(mention_id=f"m{i}", author_handle="@a", content=f"c{i}",
                         created_at=NOW - timedelta(minutes=i))
        for i in range(3)
    ])
    await db.flush()
    assert [e.mention_id for e in await social_service.list_engagements(db, limit=1)] == ["m0"]
    assert len(await social_service.list_engagements(db)) == 3


# ── record_published_posts: the twitter/linkedin cap's producer ────────────────────────────────

async def test_record_published_posts_empty_states_report_zero_and_write_nothing(async_db_session):
    db = async_db_session
    for result in ({}, {"posted": None}, {"posted": []}):
        report = await social_service.record_published_posts(db, result)
        assert report == {"ok": True, "posts_written": 0, "duplicates_skipped": 0,
                          "skipped_invalid": 0, "error": None}
    assert await _count(db, SocialPost) == 0


async def test_record_published_posts_never_counts_an_item_without_a_provider_id(async_db_session):
    """`posted` without `tweet_id` was NEVER sent — counted as invalid, never written."""
    db = async_db_session
    report = await social_service.record_published_posts(db, {"posted": [
        "not-a-dict",
        {"platform": "twitter", "content": "no id at all"},
        {"platform": "twitter", "content": "blank id", "tweet_id": "   "},
        {"platform": "twitter", "content": "none id", "tweet_id": None},
    ]})
    assert report["ok"] is True
    assert report["skipped_invalid"] == 4
    assert report["posts_written"] == 0 and report["duplicates_skipped"] == 0
    assert await _count(db, SocialPost) == 0


async def test_record_published_posts_writes_one_published_row_per_real_id(async_db_session,
                                                                           monkeypatch):
    db = async_db_session
    monkeypatch.setattr(social_service, "utcnow", lambda: NOW)
    report = await social_service.record_published_posts(db, {"posted": [
        {"platform": "twitter", "content": "tweet one", "tweet_id": "tw-1"},
        {"platform": "linkedin", "tweet_id": "li-1"},                    # content defaults to ""
    ]})
    assert report == {"ok": True, "posts_written": 2, "duplicates_skipped": 0,
                      "skipped_invalid": 0, "error": None}

    rows = {p.tweet_id: p for p in (await db.execute(select(SocialPost))).scalars()}
    assert set(rows) == {"tw-1", "li-1"}
    assert rows["tw-1"].status == "published" and rows["tw-1"].content == "tweet one"
    assert rows["li-1"].content == ""                                    # defaulted, not fabricated
    assert all(p.published_at is not None for p in rows.values())


async def test_record_published_posts_dedupes_on_the_platform_id_pair(async_db_session):
    db = async_db_session
    first = {"posted": [{"platform": "twitter", "content": "a", "tweet_id": "tw-1"},
                        {"platform": "twitter", "content": "b", "tweet_id": "tw-2"}]}
    report = await social_service.record_published_posts(db, first)
    assert report["posts_written"] == 2

    # a retried run: same provider ids, nothing new counted
    again = await social_service.record_published_posts(db, first)
    assert (again["posts_written"], again["duplicates_skipped"]) == (0, 2)
    assert await _count(db, SocialPost) == 2

    # the identity is the PAIR: the same tweet_id on another platform is a different send
    cross = await social_service.record_published_posts(
        db, {"posted": [{"platform": "linkedin", "content": "c", "tweet_id": "tw-1"}]}
    )
    assert cross["posts_written"] == 1 and cross["duplicates_skipped"] == 0

    # and a duplicate INSIDE one batch is caught by the per-item flush, not written twice
    in_batch = await social_service.record_published_posts(
        db, {"posted": [{"platform": "twitter", "content": "d", "tweet_id": "tw-9"},
                        {"platform": "twitter", "content": "e", "tweet_id": "tw-9"}]}
    )
    assert (in_batch["posts_written"], in_batch["duplicates_skipped"]) == (1, 1)
    assert await _count(db, SocialPost) == 4


async def test_record_published_posts_reports_a_named_PolsiaError_without_raising(async_db_session,
                                                                                   monkeypatch):
    db = async_db_session
    monkeypatch.setattr(social_service, "fetch_scalar",
                       _Raiser(PolsiaError(E_DB, "the read refused by name")))
    report = await social_service.record_published_posts(
        db, {"posted": [{"platform": "twitter", "content": "x", "tweet_id": "tw-err"}]}
    )
    assert report["ok"] is False
    assert report["error"] == {"code": E_DB, "message": "the read refused by name"}
    assert report["posts_written"] == 0


async def test_record_published_posts_wraps_an_unexpected_failure_as_E_DB(async_db_session,
                                                                          monkeypatch):
    db = async_db_session
    monkeypatch.setattr(social_service, "fetch_scalar",
                       _Raiser(SQLAlchemyError("driver gone")))
    report = await social_service.record_published_posts(
        db, {"posted": [{"platform": "twitter", "content": "x", "tweet_id": "tw-err2"}]}
    )
    assert report["ok"] is False
    assert report["error"]["code"] == E_DB
    assert "could not persist social posts" in report["error"]["message"]
    assert "SQLAlchemyError" in report["error"]["message"]


async def test_twitter_rows_written_by_the_producer_drive_the_cap_to_refusal(async_db_session,
                                                                             monkeypatch):
    """Counted-to-cap through REAL functions: producer rows -> snapshot -> refusal, provider never called."""
    monkeypatch.setattr(app_config.settings, "company_timezone", "UTC")
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    monkeypatch.setattr(social_service, "utcnow", lambda: NOW)
    db = async_db_session

    report = await social_service.record_published_posts(db, {"posted": [
        {"platform": "twitter", "content": f"seed {i}", "tweet_id": f"tw-{i}"} for i in range(3)
    ]})
    assert report["posts_written"] == 3

    snapshot = await channel_sends_snapshot(db, ["twitter"], now=NOW)
    assert snapshot == {"twitter": {"today": 3, "hour": 3}}     # the hour cap is 3

    provider_calls: list[str] = []

    def fake_post_tweet(text: str, *, platform: str = "twitter") -> dict[str, Any]:
        provider_calls.append(text)
        return {"ok": True, "result": {"id": "tw-new"}}

    monkeypatch.setattr(integrations, "post_tweet", fake_post_tweet)
    agent = _stub_collection(SocialMediaAgent(), {"action": "post", "posts": [
        {"platform": "twitter", "content": "one too many"},
    ]})
    with pytest.raises(PolsiaError) as excinfo:
        agent.run({"title": "post"}, {"channel_sends": snapshot})
    assert excinfo.value.code == E_RATE_LIMITED
    assert "hourly cap" in excinfo.value.message and "3/3" in excinfo.value.message
    assert provider_calls == []                                  # the refused attempt sent nothing
    assert await _count(db, SocialPost) == 3                     # and added no row


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# outreach_service
# ═══════════════════════════════════════════════════════════════════════════════════════════════

async def test_list_prospects_empty_status_filter_and_limit(async_db_session):
    db = async_db_session
    assert await outreach_service.list_prospects(db) == []
    db.add_all([
        Prospect(email=f"p{i}@example.com", status="new" if i < 2 else "contacted",
                 created_at=NOW - timedelta(minutes=i))
        for i in range(3)
    ])
    await db.flush()

    assert [p.email for p in await outreach_service.list_prospects(db, limit=1)] == ["p0@example.com"]
    assert {p.email for p in await outreach_service.list_prospects(db, status="new")} == {
        "p0@example.com", "p1@example.com"}
    assert await outreach_service.list_prospects(db, status="archived") == []


async def test_list_campaigns_and_email_logs_empty_and_limited(async_db_session):
    db = async_db_session
    assert await outreach_service.list_campaigns(db) == []
    assert await outreach_service.list_email_logs(db) == []

    from app.models.outreach import EmailCampaign

    prospect = Prospect(email="logs@example.com")
    db.add(prospect)
    await db.flush()
    db.add_all([
        EmailCampaign(name=f"camp {i}") for i in range(2)
    ])
    db.add_all([
        EmailLog(prospect_id=prospect.id, subject=f"log {i}", status="sent", sent_at=NOW)
        for i in range(3)
    ])
    await db.flush()

    assert [c.name for c in await outreach_service.list_campaigns(db, limit=1)] == ["camp 1"]
    assert len(await outreach_service.list_campaigns(db)) == 2
    assert len(await outreach_service.list_email_logs(db, limit=2)) == 2
    assert len(await outreach_service.list_email_logs(db)) == 3


async def test_create_prospect_refuses_unusable_addresses(async_db_session):
    db = async_db_session
    for raw in ("", "   ", "no-at-sign", "@leading", "trailing@"):
        with pytest.raises(PolsiaError) as excinfo:
            await outreach_service.create_prospect(db, raw)
        assert excinfo.value.code == E_VALIDATION
        assert f"prospect email {raw!r} is not a usable address" == excinfo.value.message
        assert excinfo.value.context == {"email": raw}
    assert await _count(db, Prospect) == 0
    # documented looseness: the check is "has an '@', neither leading nor trailing" — a string
    # like "a@b@c" passes it (the DB is the uniqueness authority, this is a shape gate).
    accepted = await outreach_service.create_prospect(db, "a@b@c")
    assert accepted.email == "a@b@c"


async def test_create_prospect_strips_the_address_and_keeps_optional_fields(async_db_session):
    db = async_db_session
    prospect = await outreach_service.create_prospect(
        db, "  Ada@Example.com  ", first_name="Ada", company="Analytical", source="directory",
    )
    assert prospect.email == "Ada@Example.com"                  # stripped, case preserved
    assert (prospect.first_name, prospect.company, prospect.source) == ("Ada", "Analytical",
                                                                        "directory")
    assert prospect.status == "new"
    assert prospect.id is not None


async def test_create_prospect_duplicate_is_a_validation_refusal_and_rollback_recovers(
    async_db_session,
):
    """`prospects.email` is UNIQUE: the duplicate is a NAMED refusal, not an escaping 500.

    Honest session semantics, measured: the failed flush leaves the caller's transaction in the
    failed state (`PendingRollbackError` on the next statement) until an explicit `rollback()` —
    which is exactly why `_find_or_create_prospect` wraps its create attempt in a savepoint.
    """
    db = async_db_session
    await outreach_service.create_prospect(db, "dup@example.com")
    await db.commit()

    with pytest.raises(PolsiaError) as excinfo:
        await outreach_service.create_prospect(db, "dup@example.com")
    assert excinfo.value.code == E_VALIDATION
    assert excinfo.value.message == "prospect dup@example.com already exists"
    assert "prospects.email is unique" in excinfo.value.how_to_fix

    with pytest.raises(Exception) as poisoned:
        await db.execute(select(func.count()).select_from(Prospect))
    assert "PendingRollbackError" in type(poisoned.value).__name__

    await db.rollback()                                          # the caller's documented recovery
    assert await _count(db, Prospect) == 1
    recovered = await outreach_service.create_prospect(db, "next@example.com")
    assert recovered.id is not None


async def test_create_prospect_real_dbapi_failure_is_a_named_E_DB_refusal(async_db_session):
    """A non-IntegrityError flush failure is translated (`db_refusal`), never left to escape."""
    db = async_db_session
    with pytest.raises(PolsiaError) as excinfo:
        await outreach_service.create_prospect(db, "dict@example.com", status={"not": "a string"})
    assert excinfo.value.code == E_DB
    assert "could not read prospect insert" in excinfo.value.message
    await db.rollback()
    assert await _count(db, Prospect) == 0


# ── record_sent_emails: the email cap's producer ───────────────────────────────────────────────

async def test_record_sent_emails_records_only_performed_sends(async_db_session, monkeypatch):
    """status=='sent' AND a non-empty provider id — everything else is a draft/failure, not a send."""
    monkeypatch.setattr(outreach_service, "utcnow", lambda: NOW)
    db = async_db_session

    report = await outreach_service.record_sent_emails(db, {"emails": [
        "not-a-dict",
        {"status": "draft", "sendgrid_id": "sg-draft", "prospect_email": "a@example.com"},
        {"status": "sent", "sendgrid_id": "", "prospect_email": "b@example.com"},
        {"status": "sent", "sendgrid_id": None, "prospect_email": "c@example.com"},
        {"status": "failed", "sendgrid_id": "sg-failed", "prospect_email": "d@example.com"},
        _outreach_email(5, "sg-real"),
    ]})
    assert report == {"ok": True, "emails_written": 1, "prospects_created": 1,
                      "duplicates_skipped": 0, "error": None}

    logs = list((await db.execute(select(EmailLog))).scalars())
    assert len(logs) == 1
    log = logs[0]
    assert (log.sendgrid_id, log.status, log.subject, log.body) == ("sg-real", "sent", "s5", "b5")
    assert log.sent_at is not None
    prospects = list((await db.execute(select(Prospect))).scalars())
    assert [p.email for p in prospects] == ["prospect5@example.com"]
    assert log.prospect_id == prospects[0].id


async def test_record_sent_emails_dedupes_on_the_provider_id_and_reuses_the_prospect(
    async_db_session, monkeypatch,
):
    monkeypatch.setattr(outreach_service, "utcnow", lambda: NOW)
    db = async_db_session
    result = {"emails": [_outreach_email(0, "sg-once")]}

    first = await outreach_service.record_sent_emails(db, result)
    assert (first["emails_written"], first["prospects_created"]) == (1, 1)

    again = await outreach_service.record_sent_emails(db, result)
    assert (again["emails_written"], again["duplicates_skipped"], again["prospects_created"]) == (
        0, 1, 0)
    assert await _count(db, EmailLog) == 1
    assert await _count(db, Prospect) == 1


async def test_record_sent_emails_finds_an_existing_prospect_without_creating_one(
    async_db_session, monkeypatch,
):
    monkeypatch.setattr(outreach_service, "utcnow", lambda: NOW)
    db = async_db_session
    existing = Prospect(email="known@example.com")
    db.add(existing)
    await db.flush()

    report = await outreach_service.record_sent_emails(db, {"emails": [
        {"status": "sent", "sendgrid_id": "sg-known", "prospect_email": "known@example.com",
         "subject": "hi", "body": "there"},
    ]})
    assert report["emails_written"] == 1 and report["prospects_created"] == 0
    log = (await db.execute(select(EmailLog))).scalars().one()
    assert log.prospect_id == existing.id


async def test_record_sent_emails_resolves_the_select_insert_race_via_the_savepoint(
    async_db_session, monkeypatch,
):
    """A prospect that appears between the SELECT and the INSERT is resolved by the re-select.

    The create attempt runs inside `db.begin_nested()`: without the savepoint the failed flush
    would roll back (and poison) the caller's whole transaction, and the re-select this function
    needs would itself die with `PendingRollbackError` (measured, RD14/S2 probe). The simulated
    race is exactly the SQL-visible one: the first SELECT misses a row that IS there.
    """
    db = async_db_session
    existing = Prospect(email="race@example.com")
    db.add(existing)
    await db.flush()

    real_fetch_scalar = outreach_service.fetch_scalar
    calls = {"n": 0}

    async def flaky_fetch_scalar(db_: Any, stmt: Any, *, what: str) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            return None                              # the racing SELECT that missed the row
        return await real_fetch_scalar(db_, stmt, what=what)

    monkeypatch.setattr(outreach_service, "fetch_scalar", flaky_fetch_scalar)

    prospect, created = await outreach_service._find_or_create_prospect(db, "race@example.com")
    assert created is False
    assert prospect.id == existing.id
    assert calls["n"] >= 2                           # initial miss + re-resolve

    # the session survived the duplicate's failed flush: the next write works on the same session
    fresh = await outreach_service.create_prospect(db, "after-race@example.com")
    assert fresh.id is not None
    assert await _count(db, Prospect) == 2


async def test_record_sent_emails_refuses_a_performed_send_with_a_malformed_address(
    async_db_session,
):
    """A performed send whose address cannot become a prospect row is a NAMED refusal, no row.

    `email_logs.prospect_id` is a required FK: there is no honest row to write without a prospect,
    so the report says so (`ok=False`, `E_VALIDATION`) instead of quietly inventing a prospect with
    a fake address or a row with a missing FK.
    """
    db = async_db_session
    report = await outreach_service.record_sent_emails(db, {"emails": [
        {"status": "sent", "sendgrid_id": "sg-no-address", "prospect_email": "   "},
    ]})
    assert report["ok"] is False
    assert report["error"]["code"] == E_VALIDATION
    assert "is not a usable address" in report["error"]["message"]
    assert report["emails_written"] == 0
    assert await _count(db, EmailLog) == 0 and await _count(db, Prospect) == 0


async def test_record_sent_emails_re_raises_when_the_duplicate_row_vanishes(
    async_db_session, monkeypatch,
):
    """The vanishing-race fall-through: duplicate refusal + a re-select that finds nothing.

    The window this pins is real but not forceable with a single sqlite connection: the row that
    tripped the unique constraint is GONE by the time the re-select runs (deleted/rolled back by
    the racing writer). The function's contract for that window is to re-raise the original refusal
    — never to return a prospect it did not see, and never to swallow the failure.
    """
    db = async_db_session

    async def always_miss(db_: Any, stmt: Any, *, what: str) -> Any:
        return None                                  # every SELECT misses (the initial one and the re-select)

    async def duplicate_refusal(db_: Any, address: str, **values: Any) -> Any:
        raise PolsiaError(
            E_VALIDATION, f"prospect {address} already exists",
            how_to_fix="re-use the existing prospect row (prospects.email is unique)",
            context={"email": address},
        )

    monkeypatch.setattr(outreach_service, "fetch_scalar", always_miss)
    monkeypatch.setattr(outreach_service, "create_prospect", duplicate_refusal)

    report = await outreach_service.record_sent_emails(
        db, {"emails": [_outreach_email(7, "sg-vanished")]})
    assert report["ok"] is False
    assert report["error"]["code"] == E_VALIDATION
    assert "already exists" in report["error"]["message"]
    assert report["emails_written"] == 0
    assert await _count(db, EmailLog) == 0


async def test_record_sent_emails_aborts_the_batch_at_a_malformed_address(
    async_db_session, monkeypatch,
):
    """MEASURED RESIDUAL — REQUIRED-FOLLOW-UP: one malformed address stops the WHOLE batch.

    The first item is written, the malformed second item raises `E_VALIDATION`, and the THIRD item
    (a genuinely performed send with a provider id) is never reached — so a batch of N real sends
    can permanently undercount by the items after the bad one, and the email cap reads a count
    below reality (the same class RD14 closed for the collapse case; not silent — the report is
    `ok=False` — but uncounted). The session is NOT poisoned by this refusal (no flush was
    attempted: the address check refuses before it), so continuing item-scoped is possible; the
    fix belongs with the counting contract, not in this lane's frozen surface. Pinned as measured.
    """
    monkeypatch.setattr(outreach_service, "utcnow", lambda: NOW)
    db = async_db_session
    report = await outreach_service.record_sent_emails(db, {"emails": [
        _outreach_email(1, "sg-first"),
        {"status": "sent", "sendgrid_id": "sg-bad", "prospect_email": "no-at-sign"},
        _outreach_email(3, "sg-after"),
    ]})
    assert report["ok"] is False
    assert report["error"]["code"] == E_VALIDATION
    assert report["emails_written"] == 1                  # the 3rd performed send went UNCOUNTED
    assert await _count(db, EmailLog) == 1
    # the refusal did not poison the session: the caller can keep working on it
    assert (await db.execute(select(func.count()).select_from(Prospect))).scalar() == 1


async def test_record_sent_emails_reports_a_named_PolsiaError_without_raising(
    async_db_session, monkeypatch,
):
    db = async_db_session
    monkeypatch.setattr(outreach_service, "fetch_scalar",
                       _Raiser(PolsiaError(E_VALIDATION, "the read refused by name")))
    report = await outreach_service.record_sent_emails(
        db, {"emails": [_outreach_email(1, "sg-x")]})
    assert report["ok"] is False
    assert report["error"] == {"code": E_VALIDATION, "message": "the read refused by name"}


async def test_record_sent_emails_wraps_an_unexpected_failure_as_E_DB(async_db_session, monkeypatch):
    db = async_db_session
    monkeypatch.setattr(outreach_service, "fetch_scalar", _Raiser(SQLAlchemyError("driver gone")))
    report = await outreach_service.record_sent_emails(
        db, {"emails": [_outreach_email(1, "sg-y")]})
    assert report["ok"] is False
    assert report["error"]["code"] == E_DB
    assert "could not persist sent emails" in report["error"]["message"]


async def test_email_rows_written_by_the_producer_drive_the_day_cap_to_refusal(
    async_db_session, monkeypatch,
):
    """49 counted sends + one REAL send through the agent = 50/50 (the day cap) -> the next is refused."""
    monkeypatch.setattr(app_config.settings, "company_timezone", "UTC")
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    monkeypatch.setattr(app_config.settings, "sendgrid_api_key", "SG.test-key")
    monkeypatch.setattr(app_config.settings, "sendgrid_from_email", "ops@example.com")
    fake = _FakeSendGrid().install(monkeypatch)
    db = async_db_session

    # (a) 49 real producer writes, two hours before the anchor: inside the day, outside the hour
    monkeypatch.setattr(outreach_service, "utcnow", lambda: TWO_HOURS_AGO)
    for i in range(49):
        report = await outreach_service.record_sent_emails(
            db, {"emails": [_outreach_email(i, f"sg-seed-{i}")]})
        assert report["emails_written"] == 1
    seeded = await channel_sends_snapshot(db, ["email"], now=NOW)
    assert seeded == {"email": {"today": 49, "hour": 0}}

    # (b) the 50th send is a REAL agent send (fake SDK), then the worker's producer records it
    monkeypatch.setattr(outreach_service, "utcnow", lambda: NOW)
    agent = _stub_collection(EmailOutreachAgent(), _email_payload(99))
    out = agent.run({"title": "send"}, {"channel_sends": seeded})
    assert out["emails"][0]["status"] == "sent"
    assert out["emails"][0]["sendgrid_id"] == fake.sent_message_ids[-1]
    assert (await outreach_service.record_sent_emails(db, out))["emails_written"] == 1

    at_cap = await channel_sends_snapshot(db, ["email"], now=NOW)
    assert at_cap == {"email": {"today": 50, "hour": 1}}

    # (c) the NEXT send: the existing cap refuses it by name; the provider is never reached
    agent2 = _stub_collection(EmailOutreachAgent(), _email_payload(100))
    with pytest.raises(PolsiaError) as excinfo:
        agent2.run({"title": "send"}, {"channel_sends": at_cap})
    assert excinfo.value.code == E_RATE_LIMITED
    assert "daily cap for email: 50/50" in excinfo.value.message
    assert len(fake.sent_message_ids) == 1                       # only the one real agent send
    assert await _count(db, EmailLog) == 50


async def test_known_residual_no_provider_id_collapses_distinct_sends(async_db_session,
                                                                     monkeypatch):
    """KNOWN RESIDUAL (RD14's class, surviving sibling) — REQUIRED-FOLLOW-UP, pinned as measured.

    When SendGrid's response carries no `X-Message-Id`, `email_outreach/agent.py:90-91` falls back
    to `status_code`, so every such send's `sendgrid_id` is `"202"` and the producer (whose dedupe
    key is the provider id) collapses DISTINCT sends onto ONE `email_logs` row — the email cap
    then counts 1 for N real sends (RD14's exact defect class, whose fix is in the agent's
    fallback / the adapter, neither of which is this lane's file). This test pins the measured
    behavior so a future fix trips it deliberately; it is NOT an endorsement.
    """
    monkeypatch.setattr(app_config.settings, "company_timezone", "UTC")
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    monkeypatch.setattr(app_config.settings, "sendgrid_api_key", "SG.test-key")
    monkeypatch.setattr(app_config.settings, "sendgrid_from_email", "ops@example.com")
    monkeypatch.setattr(outreach_service, "utcnow", lambda: NOW)
    fake = _FakeSendGrid(send_ids=False).install(monkeypatch)
    db = async_db_session

    sends = []
    for i in range(2):
        agent = _stub_collection(EmailOutreachAgent(), _email_payload(i))
        sends.append(agent.run({"title": "send"}, {"channel_sends": {"email": {"today": 0,
                                                                               "hour": 0}}}))

    assert len(fake.sent_message_ids) == 2                       # two real provider sends happened
    assert [s["emails"][0]["sendgrid_id"] for s in sends] == ["202", "202"]
    for out in sends:
        await outreach_service.record_sent_emails(db, out)

    logs = list((await db.execute(select(EmailLog))).scalars())
    assert len(logs) == 1                                        # ...but only one counted row
    assert await channel_sends_snapshot(db, ["email"], now=NOW) == {
        "email": {"today": 1, "hour": 1}}
