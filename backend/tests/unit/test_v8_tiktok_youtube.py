"""V8/T4 — the TikTok + YouTube adapters: dispatch routing, named refusals, media rule, schema.

What this pins (the RED legs): before V8 the dispatcher had no rows for these platforms, so
`post_tweet(platform="tiktok"/"youtube")` refused with "post_tweet adapter does not handle
platform ...", and `SocialPostDraft` rejected both platforms outright. This wave lands the two
adapters in the LinkedIn dispatch-table style, so every pin below fails on the old bytes.

Doubles are test-only and named. `sys.modules["httpx"]` is a fake whose surface mirrors the real
client for BOTH verbs the adapters use — `post(url, headers=/json=/params=/content=, timeout=)`
and `get(url, timeout=)` -> responses with `.status_code`, `.headers`, `.text`, `.content`,
`.json()`. The SETTINGS surface is a `SimpleNamespace` (the U5 idiom). No test can touch the
network: the provider legs are only ever reached with the fake installed.

V8-D3: the tiktok/youtube send counts are now MEASURABLE (`channel_service._SOCIAL_CHANNELS`
grew) — the producer->cap drill that proves a published row counts lives in
`test_u5_social_producers.py` (same-window pin move); this file pins the adapter, schema and
sandbox surfaces for the two platforms.
"""
from __future__ import annotations

import json
import sys
import types
from typing import Any

import pytest
from pydantic import ValidationError

import app.config as app_config
from app.agents import integrations
from app.core.errors import E_INTEGRATION, E_VALIDATION
from app.schemas.agent_outputs import PostedItem, SocialPostDraft

TIKTOK_ENV = ("TIKTOK_ACCESS_TOKEN",)
YOUTUBE_ENV = ("YOUTUBE_ACCESS_TOKEN",)
MEDIA_URL = "https://cdn.example.com/clip.mp4"


class _FakeHTTPX:
    """Provider double for the REAL adapters: records `post`/`get` calls, replays scripted posts.

    Response shape mirrors what the real adapters consume (`status_code`, `headers`, `.text`,
    `.content`, `.json()`); scripted responses are consumed in order by `post` only (the YouTube
    media `get` is permissive unless a `get_response` is supplied). No test can touch the network:
    the provider legs are only ever reached with the fake installed.
    """

    def __init__(
        self,
        *responses: tuple[int, dict[str, str], str],
        get_response: tuple[int, dict[str, str], str, bytes] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses = list(responses)
        self.get_response = get_response

    def install(self, monkeypatch: pytest.MonkeyPatch) -> _FakeHTTPX:
        fake = self

        class _Response:
            def __init__(self, status_code: int, headers: dict[str, str], text: str,
                         content: bytes = b"") -> None:
                self.status_code = status_code
                self.headers = headers
                self.text = text
                self.content = content

            def json(self) -> Any:
                return json.loads(self.text)

        def post(url: str, **kwargs: Any) -> _Response:
            fake.calls.append({"method": "POST", "url": url, **kwargs})
            if fake.responses:
                status, headers, text = fake.responses.pop(0)
                return _Response(status, headers, text)
            return _Response(200, {}, json.dumps({"id": "provider-ref-1"}))

        def get(url: str, **kwargs: Any) -> _Response:
            fake.calls.append({"method": "GET", "url": url, **kwargs})
            if fake.get_response is not None:
                status, headers, text, content = fake.get_response
                return _Response(status, headers, text, content)
            return _Response(200, {"content-type": "video/mp4"}, "", b"fake-video-bytes")

        class _StreamResponse(_Response):
            """The `stream()` context-manager surface (U2-03: the media download is streamed
            under a byte cap — the adapter uses httpx.stream + iter_bytes)."""

            def __enter__(self) -> _StreamResponse:
                return self

            def __exit__(self, *exc: Any) -> bool:
                return False

            def iter_bytes(self, chunk_size: int | None = None):
                yield self.content

            def read(self) -> bytes:
                return self.content

        def stream(method: str, url: str, **kwargs: Any) -> _StreamResponse:
            response = get(url, **kwargs)               # same recording + scripted get_response
            return _StreamResponse(response.status_code, response.headers,
                                   response.text, response.content)

        module = types.ModuleType("httpx")
        module.post = post                              # type: ignore[attr-defined]
        module.get = get                                # type: ignore[attr-defined]
        module.stream = stream                          # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "httpx", module)
        return fake


def _settings(**overrides: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(**{k: v for k, v in overrides.items() if v is not None})


@pytest.fixture(autouse=True)
def _stub_the_egress_floor(monkeypatch):
    """`MEDIA_URL` is a fake host (`cdn.example.com`) that cannot resolve; the U2-03 egress floor
    (wave-v14) refuses unresolvable hosts BY NAME before the adapter fetch. This suite pins the
    adapters' routing/refusal surfaces with the provider fake installed, so the floor is stubbed
    here ONLY; the floor's own legs (loopback / metadata / redirect / byte-cap) are pinned
    end-to-end in `tests/unit/test_v14_sec.py` against a real loopback server."""
    monkeypatch.setattr(integrations, "egress_policy_refusal", lambda url, **kwargs: None)


# ── named refusals: media shape first, then credentials ─────────────────────────────────────────

def test_tiktok_and_youtube_require_a_media_url_by_name_and_never_call_the_provider(monkeypatch):
    """Both platforms post MEDIA only: no image_url -> E_VALIDATION, provider untouched."""
    monkeypatch.setattr(integrations, "settings", _settings(
        tiktok_access_token="tt-token", youtube_access_token="yt-token"))
    fake = _FakeHTTPX().install(monkeypatch)

    tiktok = integrations.post_tweet("hello", platform="tiktok")
    assert tiktok["ok"] is False
    assert tiktok["error"]["code"] == E_VALIDATION
    assert "tiktok" in tiktok["error"]["message"] and "image_url" in tiktok["error"]["message"]
    assert tiktok["error"]["how_to_fix"]

    youtube = integrations.post_tweet("hello", platform="youtube")
    assert youtube["ok"] is False
    assert youtube["error"]["code"] == E_VALIDATION
    assert "youtube" in youtube["error"]["message"] and "image_url" in youtube["error"]["message"]
    assert youtube["error"]["how_to_fix"]

    assert fake.calls == [], "an unpostable item must not reach the provider"


def test_tiktok_and_youtube_unconfigured_refuse_by_name_naming_every_variable(monkeypatch):
    """No TIKTOK_/YOUTUBE_ credentials -> E_INTEGRATION naming the missing var, real how_to_fix."""
    monkeypatch.setattr(integrations, "settings", _settings())

    tiktok = integrations.post_tweet("hello", platform="tiktok", image_url=MEDIA_URL)
    assert tiktok["ok"] is False
    assert tiktok["error"]["code"] == E_INTEGRATION
    assert tiktok["error"]["context"]["missing"] == list(TIKTOK_ENV)
    assert "TIKTOK_ACCESS_TOKEN" in tiktok["error"]["message"]
    assert "TIKTOK_ACCESS_TOKEN" in tiktok["error"]["how_to_fix"]

    youtube = integrations.post_tweet("hello", platform="youtube", image_url=MEDIA_URL)
    assert youtube["ok"] is False
    assert youtube["error"]["code"] == E_INTEGRATION
    assert youtube["error"]["context"]["missing"] == list(YOUTUBE_ENV)
    assert "YOUTUBE_ACCESS_TOKEN" in youtube["error"]["how_to_fix"]


# ── tiktok: Content Posting API direct post (PULL_FROM_URL) ─────────────────────────────────────

def test_tiktok_configured_routes_through_the_dispatch_table_to_the_real_adapter(monkeypatch):
    """Configured: the REAL adapter runs (fake httpx) — real URL, headers, and entity body."""
    monkeypatch.setattr(integrations, "settings", _settings(tiktok_access_token="tt-token"))
    fake = _FakeHTTPX((200, {}, json.dumps(
        {"data": {"publish_id": "tt-pub-1"}, "error": {"code": "ok", "message": ""}})
    )).install(monkeypatch)

    out = integrations.post_tweet("hello tiktok", platform="tiktok", image_url=MEDIA_URL)

    assert out == {"ok": True, "result": {"id": "tt-pub-1"}}
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["url"] == "https://open.tiktokapis.com/v2/post/publish/video/init/"
    assert call["headers"]["Authorization"] == "Bearer tt-token"
    assert "charset=UTF-8" in call["headers"]["Content-Type"]
    assert call["json"]["post_info"]["title"] == "hello tiktok"
    assert call["json"]["post_info"]["privacy_level"] == "SELF_ONLY"
    assert call["json"]["source_info"] == {"source": "PULL_FROM_URL", "video_url": MEDIA_URL}


def test_tiktok_provider_error_keeps_the_provider_text(monkeypatch):
    """A non-2xx from TikTok is a named E_INTEGRATION refusal carrying the provider's own text."""
    monkeypatch.setattr(integrations, "settings", _settings(tiktok_access_token="tt-token"))
    _FakeHTTPX((401, {}, '{"error": {"code": "access_token_invalid", "message": "invalid token"}}')
               ).install(monkeypatch)

    out = integrations.post_tweet("hello", platform="tiktok", image_url=MEDIA_URL)

    assert out["ok"] is False
    assert out["error"]["code"] == E_INTEGRATION
    assert "401" in out["error"]["message"]
    assert "access_token_invalid" in out["error"]["message"]


def test_tiktok_response_without_a_publish_id_is_a_named_refusal(monkeypatch):
    """A 2xx whose body carries no publish_id must refuse — an id is never invented."""
    monkeypatch.setattr(integrations, "settings", _settings(tiktok_access_token="tt-token"))
    _FakeHTTPX((200, {}, '{"data": {}, "error": {"code": "ok"}}')).install(monkeypatch)

    out = integrations.post_tweet("hello", platform="tiktok", image_url=MEDIA_URL)

    assert out["ok"] is False
    assert out["error"]["code"] == E_INTEGRATION
    assert "publish_id" in out["error"]["message"]


# ── youtube: Data API videos.insert (media fetched, then a multipart upload) ────────────────────

def test_youtube_configured_fetches_the_media_and_calls_videos_insert(monkeypatch):
    """Configured: the REAL adapter downloads `image_url` and uploads it through videos.insert."""
    monkeypatch.setattr(integrations, "settings", _settings(youtube_access_token="yt-token"))
    fake = _FakeHTTPX((200, {}, json.dumps({"id": "yt-vid-1"}))).install(monkeypatch)

    out = integrations.post_tweet("hello youtube", platform="youtube", image_url=MEDIA_URL)

    assert out == {"ok": True, "result": {"id": "yt-vid-1"}}
    fetched, uploaded = fake.calls
    assert fetched["method"] == "GET" and fetched["url"] == MEDIA_URL
    assert uploaded["method"] == "POST"
    assert uploaded["url"] == "https://www.googleapis.com/upload/youtube/v3/videos"
    assert uploaded["params"]["uploadType"] == "multipart"
    assert uploaded["params"]["part"] == "snippet,status"
    assert uploaded["headers"]["Authorization"] == "Bearer yt-token"
    assert uploaded["headers"]["Content-Type"].startswith("multipart/related; boundary=")
    assert b"hello youtube" in uploaded["content"]        # the JSON metadata part (the caption)
    assert b"fake-video-bytes" in uploaded["content"]     # the media part, the real fetched bytes


def test_youtube_provider_error_keeps_the_provider_text(monkeypatch):
    monkeypatch.setattr(integrations, "settings", _settings(youtube_access_token="yt-token"))
    _FakeHTTPX((403, {}, '{"error": {"message": "quotaExceeded"}}')).install(monkeypatch)

    out = integrations.post_tweet("hello", platform="youtube", image_url=MEDIA_URL)

    assert out["ok"] is False
    assert out["error"]["code"] == E_INTEGRATION
    assert "403" in out["error"]["message"]
    assert "quotaExceeded" in out["error"]["message"]


def test_youtube_media_fetch_failure_is_a_named_refusal(monkeypatch):
    """The media GET failing is a named refusal with the provider text — never a partial upload."""
    monkeypatch.setattr(integrations, "settings", _settings(youtube_access_token="yt-token"))
    _FakeHTTPX(get_response=(404, {}, "not found", b"")).install(monkeypatch)

    out = integrations.post_tweet("hello", platform="youtube", image_url=MEDIA_URL)

    assert out["ok"] is False
    assert out["error"]["code"] == E_INTEGRATION
    assert "404" in out["error"]["message"]


# ── dispatch + schema surfaces ──────────────────────────────────────────────────────────────────

def test_dispatch_supported_set_now_names_tiktok_and_youtube():
    out = integrations.post_tweet("hello", platform="myspace")
    assert out["ok"] is False
    err = out["error"]
    assert "tiktok" in err["how_to_fix"] and "youtube" in err["how_to_fix"]


def test_schema_admits_tiktok_and_youtube():
    """The platform Literals grew with the adapters; an unwired platform stays rejected."""
    tiktok = SocialPostDraft(platform="tiktok", content="hello", image_url=MEDIA_URL)
    assert tiktok.platform == "tiktok" and tiktok.image_url == MEDIA_URL
    youtube = SocialPostDraft(platform="youtube", content="hello")
    assert youtube.platform == "youtube" and youtube.image_url is None
    assert PostedItem(platform="youtube", content="gone out", tweet_id="yt-vid-1").tweet_id == "yt-vid-1"
    with pytest.raises(ValidationError):
        SocialPostDraft(platform="mastodon", content="still unwired")


# ── agent level: sandbox labels tiktok/youtube attempts, the integration is never reached ───────

def _stub_collection(agent: Any, payload: dict[str, Any]) -> Any:
    agent.collect_structured = lambda task, context, **kw: (dict(payload), None)
    return agent


def test_sandboxed_tiktok_and_youtube_attempts_become_labelled_drafts(monkeypatch):
    """Sandbox is the firewall's answer for every channel, adapter or not: a labelled draft, no
    raise, and the integration is never reached."""
    from app.agents.social_media.agent import SocialMediaAgent

    monkeypatch.setattr(app_config.settings, "sandbox_mode", True)

    def _explode(*args: Any, **kwargs: Any) -> dict:
        raise AssertionError("sandboxed run must never reach the integration")

    monkeypatch.setattr(integrations, "post_tweet", _explode)
    agent = _stub_collection(SocialMediaAgent(), {"action": "post", "posts": [
        {"platform": "tiktok", "content": "tt draft", "image_url": MEDIA_URL},
        {"platform": "youtube", "content": "yt draft", "image_url": MEDIA_URL},
    ]})
    out = agent.run({"title": "post"}, {})

    assert out["sandbox"] is True
    assert out["drafted_not_sent"] is True
    assert out["posted"] == []
    assert [r["sandboxed"] for r in out["side_effects"]] == [True, True]
