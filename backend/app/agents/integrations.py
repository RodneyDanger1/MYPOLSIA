"""The agents' third-party action adapters — one honest call site per external capability.

Each function here performs ONE real-world action and returns either
`{"ok": True, "result": ...}` or a NAMED refusal (`E_INTEGRATION`) explaining exactly why the
action could not be performed (library missing, credentials unset, provider error).

Two rules from the build brief are enforced by construction:

  * integrations are imported LAZILY inside the function (CLAUDE.md:116) — importing the agent
    package must never drag in tweepy/stripe/sendgrid/github;
  * a capability that is not configured returns a refusal, never a success-shaped empty result.
    CORRECTED (RD15 dead-sweep): the provider libraries ARE installed in this venv
    (requirements.txt pins them — verified importable 2026-10-13), so the refusal on this
    deployment comes from the UNSET `*_API_KEY`/`*_TOKEN` settings, not from a missing library;
    with the libraries AND those settings present the same code path makes the provider call.

Nothing in this module is reachable while `SANDBOX_MODE=true`: the policy firewall refuses the
action first and the agent records a labelled draft instead.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.parse import urlsplit

from app.agents.side_effects import (
    ProviderTransportError,
    integration_call,
    named_refusal,
)
from app.agents.tool_registry import egress_policy_refusal
from app.config import settings
from app.core.errors import E_INTEGRATION, E_VALIDATION
from app.core.timeutil import now_utc, today_utc

logger = logging.getLogger(__name__)

_TWEEPY_HINT = "pip install tweepy==4.14.0 and set TWITTER_API_KEY/TWITTER_API_SECRET/TWITTER_ACCESS_TOKEN/TWITTER_ACCESS_SECRET"
_SENDGRID_HINT = "pip install sendgrid and set SENDGRID_API_KEY/SENDGRID_FROM_EMAIL"
_PYGITHUB_HINT = "pip install PyGithub and set GITHUB_TOKEN"
_STRIPE_HINT = "pip install stripe and set STRIPE_SECRET_KEY"
_ADS_HINT = ("pip install google-ads==24.1.0 facebook-business==21.0.0 and set the "
             "GOOGLE_ADS_* / META_ADS_* credentials")
_HTTPX_HINT = "pip install httpx==0.28.1 (already pinned in requirements.txt)"
_LINKEDIN_HINT = (
    "set LINKEDIN_ACCESS_TOKEN and LINKEDIN_AUTHOR_URN (a LinkedIn app token with "
    "w_member_social/w_organization_social plus the author urn, e.g. urn:li:person:XXXX or "
    "urn:li:organization:YYYY)"
)
_META_HINT = (
    "set META_PAGE_ACCESS_TOKEN, META_FACEBOOK_PAGE_ID and META_INSTAGRAM_USER_ID (one Meta app "
    "with pages_manage_posts + instagram_content_publish; the page token posts as the Facebook "
    "page and publishes to its linked Instagram business account)"
)
_TIKTOK_HINT = (
    "set TIKTOK_ACCESS_TOKEN (a TikTok user token authorized for the Content Posting API's "
    "video.publish scope; direct posts also need the media URL's domain or URL prefix verified "
    "in that TikTok app)"
)
_YOUTUBE_HINT = (
    "set YOUTUBE_ACCESS_TOKEN (an OAuth token for the YouTube Data API with the youtube.upload "
    "scope, which videos.insert requires)"
)

# Where the provider calls go. ONE pinned Graph version for both Meta platforms (aligned with the
# facebook-business==21.0.0 pin); LinkedIn's versioned REST API documents its version per request.
# V8/T4: TikTok's Content Posting API pins ONE path for the direct-post init; YouTube's Data API
# upload host (`/upload/youtube/v3`) is separate from its read host.
_LINKEDIN_POSTS_URL = "https://api.linkedin.com/rest/posts"
_LINKEDIN_API_VERSION = "202401"
_GRAPH_BASE = "https://graph.facebook.com/v21.0"
_TIKTOK_POST_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
_YOUTUBE_VIDEOS_INSERT_URL = "https://www.googleapis.com/upload/youtube/v3/videos"

#: Hard cap on the media bytes the YouTube adapter will download+upload (streamed; abort past
#: it). U2-03 (wave-v14): pre-fix `httpx.get` materialised an attacker-sized response in memory.
YOUTUBE_MEDIA_MAX_BYTES = 100 * 1024 * 1024

# ---------------------------------------------------------------------------
# BOUNDED PROVIDER CALLS (wave 8).  One named bound per network leg, for two reasons:
#
#   * a call with NO timeout pins a Celery worker until the OS gives up — tweepy passes no
#     timeout to `requests` at all (`Client.request` calls `session.request` bare), and the
#     Stripe/PyGithub SDKs ship their own long/default bounds (80 s socket, 15 s + urllib3
#     retries).  The adapter owns the number instead of inheriting one;
#   * a no-answer fault must NAME the bound in the refusal, or "it failed" cannot be acted on
#     and a blind retry of a mutation (post / PR / refund) can perform it twice.
#
# The httpx adapters already passed 30 / 60 / 120 s literals; those values are unchanged and
# now have one definition each so a test can pin them.
# ---------------------------------------------------------------------------
SOCIAL_POST_TIMEOUT_SECONDS = 30.0
YOUTUBE_MEDIA_TIMEOUT_SECONDS = 60.0
YOUTUBE_UPLOAD_TIMEOUT_SECONDS = 120.0
#: tweepy's `Client` has no timeout parameter: the bound is installed on the `requests.Session`
#: it uses (see `_tweepy_create_tweet`).
TWITTER_TIMEOUT_SECONDS = 20.0
#: The Stripe SDK's own defaults are an 80 s socket timeout and `max_network_retries = 2`; this
#: adapter builds a per-call `StripeClient` instead (bounded, zero internal retries).
STRIPE_TIMEOUT_SECONDS = 20.0


def _no_answer(provider: str, exc: BaseException, *, timeout_seconds: float) -> ProviderTransportError:
    """The ambiguity class for ONE provider call: the request produced no answer inside its bound.

    WHY shared: every adapter classifies its OWN SDK exception shape (`requests`/`httpx`
    transport errors, Stripe `http_status`, PyGithub `GithubException.status`) at its own edge,
    then hands the ambiguity contract one identical, named object.  `ProviderTransportError` is
    what `integration_call` turns into the `outcome_unknown` refusal; the message names the
    provider and the bound so an operator can tell a hang from a rejection.
    """
    return ProviderTransportError(
        f"{provider} produced no answer within {timeout_seconds:g}s "
        f"(transport fault): {type(exc).__name__}: {exc}",
        timeout_seconds=timeout_seconds,
    )


def _missing_config(*pairs: tuple[str, str]) -> list[str]:
    return [name for name, value in pairs if not str(value or "").strip()]


def _setting(name: str, default: str = "") -> str:
    """`settings.<name>` read DEFENSIVELY, the `configured_approval_secret` pattern.

    WHY getattr: the U5 platform credentials (`linkedin_*`, `meta_page_*`, `meta_facebook_page_id`,
    `meta_instagram_user_id`) are not fields on `Settings` yet — `app/config.py` belongs to another
    U5 lane — and pydantic-settings' `extra="ignore"` means an env var alone cannot activate a
    field that does not exist. Read this way the adapter needs no edit when the fields land, and
    until then the credential check refuses BY NAME (E_INTEGRATION, `missing=[...]`) instead of
    pretending. Never returns None.
    """
    value = getattr(settings, name, default)
    return value if isinstance(value, str) else str(value or "")


def _response_header(response: Any, name: str) -> str:
    """One response header, case-insensitively (real `httpx.Headers` and plain mapping doubles).

    Same discipline as the SendGrid `X-Message-Id` read below: a provider id that arrives only as
    a header must be found by its case-insensitive name, or the caller gets "" — never an invented
    id.
    """
    headers = getattr(response, "headers", None) or {}
    items = getattr(headers, "items", None)
    if not callable(items):
        return ""
    wanted = name.lower()
    return next((str(value) for key, value in items() if str(key).lower() == wanted), "")


def _post_twitter(text: str, *, image_url: str = "", reply_to: str = "") -> dict:
    """Post one status update to X/Twitter. Sandbox never reaches here (the firewall refuses first).

    V15/4-04 (S5-04): `reply_to` is the provider id of the tweet being answered — a REAL thread
    (`create_tweet(in_reply_to_tweet_id=...)`), not a detached post that merely mentions a name.
    """
    if image_url:
        return named_refusal(
            E_VALIDATION, "twitter adapter does not upload media yet",
            how_to_fix="post text only, or extend the adapter with the v1.1 media upload flow",
            platform="twitter", image_url=image_url,
        )
    missing = _missing_config(
        ("TWITTER_API_KEY", settings.twitter_api_key),
        ("TWITTER_API_SECRET", settings.twitter_api_secret),
        ("TWITTER_ACCESS_TOKEN", settings.twitter_access_token),
        ("TWITTER_ACCESS_SECRET", settings.twitter_access_secret),
    )
    if missing:
        return named_refusal(
            E_INTEGRATION, f"Twitter credentials missing: {', '.join(missing)}",
            how_to_fix=_TWEEPY_HINT, missing=missing,
        )
    return integration_call(
        "tweepy", "Client", agent_type="social_media", install_hint=_TWEEPY_HINT,
        call=_tweepy_create_tweet, may_have_applied=True, text=text, reply_to=reply_to,
    )


def _tweepy_create_tweet(text: str, reply_to: str = "") -> Any:  # pragma: no cover - provider
    import requests
    import tweepy

    client = tweepy.Client(
        consumer_key=settings.twitter_api_key,
        consumer_secret=settings.twitter_api_secret,
        access_token=settings.twitter_access_token,
        access_token_secret=settings.twitter_access_secret,
    )
    # tweepy's Client has NO timeout parameter and `Client.request` calls `session.request` with
    # none, so a half-open connection would block the worker until the OS gives up. The bound goes
    # on the one Session the client uses; `setdefault` keeps an explicit per-call timeout (there is
    # none today) authoritative rather than overwriting it.
    session = requests.Session()
    session_request = session.request

    def _bounded_request(method: str, url: str, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", TWITTER_TIMEOUT_SECONDS)
        return session_request(method, url, **kwargs)

    session.request = _bounded_request  # type: ignore[assignment]
    client.session = session
    kwargs: dict[str, Any] = {}
    if reply_to:
        kwargs["in_reply_to_tweet_id"] = reply_to
    try:
        response = client.create_tweet(text=text, **kwargs)
    except requests.RequestException as exc:
        # A `tweepy.errors.HTTPException` (the provider ANSWERED with a status) is not a requests
        # exception: it propagates untouched and the generic handler reports the provider's own
        # text. A requests transport exception means NO answer arrived — ambiguous for a mutation
        # (a retry can post a second tweet), so it is raised as the named ambiguity class.
        raise _no_answer("X/Twitter", exc, timeout_seconds=TWITTER_TIMEOUT_SECONDS) from exc
    return {"id": str(response.data.get("id"))}


def _post_linkedin(text: str, *, image_url: str = "") -> dict:
    """Post one text update to LinkedIn through the versioned REST API (`/rest/posts`).

    Credentials are read defensively (`_setting`): LINKEDIN_ACCESS_TOKEN + LINKEDIN_AUTHOR_URN.
    A media post is REFUSED BY NAME — LinkedIn's images API is a documented limitation (deliberate
    scope, not an open work item), never a silent drop of the image.
    """
    if image_url:
        return named_refusal(
            E_VALIDATION, "linkedin adapter does not upload media yet",
            how_to_fix="post text only, or extend the adapter with LinkedIn's images API "
                       "(initializeUpload -> upload -> attach)",
            platform="linkedin", image_url=image_url,
        )
    missing = _missing_config(
        ("LINKEDIN_ACCESS_TOKEN", _setting("linkedin_access_token")),
        ("LINKEDIN_AUTHOR_URN", _setting("linkedin_author_urn")),
    )
    if missing:
        return named_refusal(
            E_INTEGRATION, f"LinkedIn credentials missing: {', '.join(missing)}",
            how_to_fix=_LINKEDIN_HINT, missing=missing,
        )
    return integration_call(
        "httpx", "post", agent_type="social_media", install_hint=_HTTPX_HINT,
        call=_linkedin_create_post, may_have_applied=True, text=text,
    )


def _linkedin_create_post(text: str) -> Any:  # pragma: no cover - requires the provider
    import httpx

    try:
        response = httpx.post(
            _LINKEDIN_POSTS_URL,
            headers={
                "Authorization": f"Bearer {_setting('linkedin_access_token')}",
                "Content-Type": "application/json",
                "LinkedIn-Version": _LINKEDIN_API_VERSION,
                "X-Restli-Protocol-Version": "2.0.0",
            },
            json={
                "author": _setting("linkedin_author_urn"),
                "commentary": text,
                "visibility": "PUBLIC",
                "distribution": {
                    "feedDistribution": "MAIN_FEED",
                    "targetEntities": [],
                    "thirdPartyDistributionChannels": [],
                },
                "lifecycleState": "PUBLISHED",
                "isReshareDisabledByAuthor": False,
            },
            timeout=SOCIAL_POST_TIMEOUT_SECONDS,
        )
    except httpx.TransportError as exc:
        # No answer arrived.  A post is a MUTATION, so the request may already be visible on the
        # author's feed — the named ambiguity class is the honest report, never a bare "failed".
        raise _no_answer("LinkedIn", exc, timeout_seconds=SOCIAL_POST_TIMEOUT_SECONDS) from exc
    if response.status_code not in (200, 201):
        # The provider's own status + text travel in the exception — `integration_call` turns this
        # into a named E_INTEGRATION refusal that KEEPS that text (never swallowed).
        raise RuntimeError(f"LinkedIn API returned {response.status_code}: {response.text[:300]}")
    return {"id": _response_header(response, "x-restli-id")}


def _post_facebook(text: str, *, image_url: str = "") -> dict:
    """Post one text update to the Facebook PAGE feed.

    ONE Meta unit with instagram: both read the same `META_PAGE_ACCESS_TOKEN` — the page token is
    what posts as the page and what publishes to its linked Instagram business account.
    """
    if image_url:
        return named_refusal(
            E_VALIDATION, "facebook adapter does not upload media yet",
            how_to_fix="post text only, or extend the adapter with the page /photos edge",
            platform="facebook", image_url=image_url,
        )
    missing = _missing_config(
        ("META_PAGE_ACCESS_TOKEN", _setting("meta_page_access_token")),
        ("META_FACEBOOK_PAGE_ID", _setting("meta_facebook_page_id")),
    )
    if missing:
        return named_refusal(
            E_INTEGRATION, f"Facebook credentials missing: {', '.join(missing)}",
            how_to_fix=_META_HINT, missing=missing,
        )
    return integration_call(
        "httpx", "post", agent_type="social_media", install_hint=_HTTPX_HINT,
        call=_facebook_create_post, may_have_applied=True, text=text,
    )


def _facebook_create_post(text: str) -> Any:  # pragma: no cover - requires the provider
    import httpx

    page_id = _setting("meta_facebook_page_id")
    try:
        response = httpx.post(
            f"{_GRAPH_BASE}/{page_id}/feed",
            data={"message": text, "access_token": _setting("meta_page_access_token")},
            timeout=SOCIAL_POST_TIMEOUT_SECONDS,
        )
    except httpx.TransportError as exc:
        raise _no_answer("Facebook", exc, timeout_seconds=SOCIAL_POST_TIMEOUT_SECONDS) from exc
    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"Facebook Graph API returned {response.status_code}: {response.text[:300]}"
        )
    return {"id": str(response.json().get("id") or "")}


def _post_instagram(text: str, *, image_url: str = "") -> dict:
    """Publish one Instagram media post (container -> publish), the API's ONLY posting flow.

    Instagram has no text-only post: without `image_url` this refuses BY NAME (E_VALIDATION) and
    never touches the provider — a fabricated success here would be the worst possible lie.
    """
    if not str(image_url or "").strip():
        return named_refusal(
            E_VALIDATION,
            "instagram requires image_url: the Content Publishing API has no text-only post",
            how_to_fix=("set image_url on the post item to a PUBLIC https image URL the "
                        "Instagram account can fetch"),
            platform="instagram",
        )
    missing = _missing_config(
        ("META_PAGE_ACCESS_TOKEN", _setting("meta_page_access_token")),
        ("META_INSTAGRAM_USER_ID", _setting("meta_instagram_user_id")),
    )
    if missing:
        return named_refusal(
            E_INTEGRATION, f"Instagram credentials missing: {', '.join(missing)}",
            how_to_fix=_META_HINT, missing=missing,
        )
    return integration_call(
        "httpx", "post", agent_type="social_media", install_hint=_HTTPX_HINT,
        call=_instagram_create_post, may_have_applied=True, text=text, image_url=str(image_url),
    )


def _instagram_create_post(text: str, image_url: str) -> Any:  # pragma: no cover - provider
    import httpx

    token = _setting("meta_page_access_token")
    user_id = _setting("meta_instagram_user_id")
    # The CONTAINER leg is deliberately NOT wrapped in the ambiguity class: a fault there means
    # nothing was published (a retry creates another container and publishes once — no duplicate
    # post).  Only the media_publish leg below can have produced the visible post.
    container = httpx.post(
        f"{_GRAPH_BASE}/{user_id}/media",
        data={"image_url": image_url, "caption": text, "access_token": token},
        timeout=SOCIAL_POST_TIMEOUT_SECONDS,
    )
    if container.status_code not in (200, 201):
        raise RuntimeError(
            f"Instagram media container returned {container.status_code}: {container.text[:300]}"
        )
    creation_id = str(container.json().get("id") or "")
    if not creation_id:
        raise RuntimeError("Instagram media container returned no id")
    try:
        published = httpx.post(
            f"{_GRAPH_BASE}/{user_id}/media_publish",
            data={"creation_id": creation_id, "access_token": token},
            timeout=SOCIAL_POST_TIMEOUT_SECONDS,
        )
    except httpx.TransportError as exc:
        raise _no_answer("Instagram", exc, timeout_seconds=SOCIAL_POST_TIMEOUT_SECONDS) from exc
    if published.status_code not in (200, 201):
        raise RuntimeError(
            f"Instagram publish returned {published.status_code}: {published.text[:300]}"
        )
    return {"id": str(published.json().get("id") or "")}


def _post_tiktok(text: str, *, image_url: str = "") -> dict:
    """Post one video to TikTok through the Content Posting API's Direct Post (PULL_FROM_URL).

    TikTok has no text-only post: without `image_url` (the public video URL) this refuses BY NAME
    (E_VALIDATION) and never touches the provider. The token is read defensively (`_setting`) and
    must carry the `video.publish` scope; PULL_FROM_URL additionally needs the media URL's domain
    or URL prefix verified in that TikTok app. SELF_ONLY is the privacy level sent — the docs
    require a value that matches one of the creator's `privacy_level_options`, and the
    most-private option is the automatable default for an unattended agent (TikTok restricts
    unaudited clients to private viewing mode anyway).
    """
    media_url = str(image_url or "").strip()
    if not media_url:
        return named_refusal(
            E_VALIDATION,
            "tiktok requires image_url: the Content Posting API has no text-only post",
            how_to_fix=("set image_url on the post item to a PUBLIC https video URL on a domain "
                        "verified in the TikTok app (PULL_FROM_URL)"),
            platform="tiktok",
        )
    missing = _missing_config(("TIKTOK_ACCESS_TOKEN", _setting("tiktok_access_token")))
    if missing:
        return named_refusal(
            E_INTEGRATION, f"TikTok credentials missing: {', '.join(missing)}",
            how_to_fix=_TIKTOK_HINT, missing=missing,
        )
    return integration_call(
        "httpx", "post", agent_type="social_media", install_hint=_HTTPX_HINT,
        call=_tiktok_publish_video, may_have_applied=True, text=text, image_url=media_url,
    )


def _tiktok_publish_video(text: str, image_url: str) -> Any:  # pragma: no cover - provider
    import httpx

    # ONE documented call: POST /v2/post/publish/video/init/ (Direct Post). `publish_id` is
    # TikTok's id for the publish request; the PULL_FROM_URL fetch + processing complete
    # asynchronously (status/fetch is the documented polling endpoint — polling is a documented
    # limitation of this adapter, deliberate scope; completion is never faked here).
    try:
        response = httpx.post(
            _TIKTOK_POST_INIT_URL,
            headers={
                "Authorization": f"Bearer {_setting('tiktok_access_token')}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json={
                "post_info": {
                    "title": text[:2200],          # TikTok's caption limit (UTF-16 runes)
                    "privacy_level": "SELF_ONLY",
                },
                "source_info": {"source": "PULL_FROM_URL", "video_url": image_url},
            },
            timeout=SOCIAL_POST_TIMEOUT_SECONDS,
        )
    except httpx.TransportError as exc:
        raise _no_answer("TikTok", exc, timeout_seconds=SOCIAL_POST_TIMEOUT_SECONDS) from exc
    if response.status_code not in (200, 201):
        # The provider's own status + text travel in the exception — `integration_call` turns
        # this into a named E_INTEGRATION refusal that KEEPS that text (never swallowed).
        raise RuntimeError(
            f"TikTok publish init returned {response.status_code}: {response.text[:300]}"
        )
    body = response.json()
    publish_id = str(((body.get("data") or {}).get("publish_id")) or "")
    if not publish_id:
        raise RuntimeError(
            f"TikTok publish init returned no publish_id: {response.text[:300]}"
        )
    return {"id": publish_id}


def _media_url_refusal(url: str) -> dict | None:
    """Scheme check + the shared egress floor for the YouTube media download (U2-03, wave-v14).

    `image_url` is the agent's post payload (LLM output) and this adapter is the worker's one
    other arbitrary-URL fetch besides `web_fetch` — pre-fix there was NO validation at all and
    the whole response was materialised in memory (FINDINGS U2-03). Same floor as web_fetch,
    one shared definition. Returns the NAMED refusal, or None when the fetch may proceed.
    """
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return named_refusal(
            E_VALIDATION,
            f"youtube: image_url must be an absolute http(s) URL (got {url!r})",
            how_to_fix=("set image_url on the post item to a PUBLIC https URL of the video "
                        "file to upload"),
            platform="youtube", image_url=url)
    return egress_policy_refusal(url, agent_type="social_media", tool="youtube media fetch")


def _post_youtube(text: str, *, image_url: str = "") -> dict:
    """Upload one video to YouTube through the Data API's `videos.insert`.

    YouTube has no text-only post: without `image_url` (a public video-file URL) this refuses BY
    NAME (E_VALIDATION) and never touches the provider. The token is read defensively
    (`_setting`) and must carry the `youtube.upload` OAuth scope. The upload is ONE
    `videos.insert` call with `uploadType=multipart` — a JSON `snippet`/`status` metadata part
    plus the media part, as the API documents media uploads; the adapter downloads the media from
    `image_url` first (YouTube has no PULL_FROM_URL equivalent).
    """
    media_url = str(image_url or "").strip()
    if not media_url:
        return named_refusal(
            E_VALIDATION,
            "youtube requires image_url: videos.insert uploads a media file and has no "
            "text-only post",
            how_to_fix=("set image_url on the post item to a PUBLIC https URL of the video file "
                        "to upload (the adapter downloads it and sends the bytes to YouTube)"),
            platform="youtube",
        )
    missing = _missing_config(("YOUTUBE_ACCESS_TOKEN", _setting("youtube_access_token")))
    if missing:
        return named_refusal(
            E_INTEGRATION, f"YouTube credentials missing: {', '.join(missing)}",
            how_to_fix=_YOUTUBE_HINT, missing=missing,
        )
    media_denial = _media_url_refusal(media_url)
    if media_denial is not None:
        return media_denial
    return integration_call(
        "httpx", "post", agent_type="social_media", install_hint=_HTTPX_HINT,
        call=_youtube_insert_video, may_have_applied=True, text=text, image_url=media_url,
    )


def _youtube_insert_video(text: str, image_url: str) -> Any:  # pragma: no cover - provider
    import json
    import uuid

    import httpx

    # The media must travel as BYTES: fetch the caller's public URL, then upload it as the
    # multipart body's media part (Google's documented upload shape). The download is STREAMED
    # under a byte cap and NEVER follows redirects (U2-03, wave-v14): pre-fix `httpx.get`
    # auto-followed a 302 anywhere and materialised an attacker-sized response in memory — the
    # same SSRF class `web_fetch` now closes (the URL floor itself is enforced by the caller,
    # `_post_youtube`, before this adapter is ever invoked). `privacyStatus: "public"` is the
    # product intent (mirrors LinkedIn's visibility PUBLIC); YouTube itself restricts uploads
    # from unverified API projects to private viewing until the project passes its audit.
    cap = int(YOUTUBE_MEDIA_MAX_BYTES)
    chunks: list[bytes] = []
    bytes_read = 0
    # The DOWNLOAD is deliberately NOT wrapped in the ambiguity class: the upload has not started,
    # so a fault here cannot have published anything.  Only the videos.insert POST below can.
    with httpx.stream("GET", image_url, timeout=YOUTUBE_MEDIA_TIMEOUT_SECONDS,
                      follow_redirects=False) as media:
        if media.status_code in (301, 302, 303, 307, 308):
            raise RuntimeError(
                f"YouTube media fetch: {image_url} answered a redirect (HTTP "
                f"{media.status_code}); redirects are not followed — pass the direct file URL"
            )
        if media.status_code != 200:
            raise RuntimeError(
                f"YouTube media fetch returned {media.status_code}: {media.read()[:300]!r}"
            )
        media_type = _response_header(media, "content-type") or "application/octet-stream"
        for chunk in media.iter_bytes():
            bytes_read += len(chunk)
            if bytes_read > cap:
                raise RuntimeError(
                    f"YouTube media exceeds the {cap}-byte cap (YOUTUBE_MEDIA_MAX_BYTES); the "
                    "download was stopped before the upload body was built — publish a smaller "
                    "file or raise the cap"
                )
            chunks.append(chunk)
    boundary = f"meridian-{uuid.uuid4().hex}"
    metadata = json.dumps({
        "snippet": {"title": text[:100], "description": text[:5000]},   # YouTube's own limits
        "status": {"privacyStatus": "public"},
    })
    body = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{metadata}\r\n"
        f"--{boundary}\r\nContent-Type: {media_type}\r\n\r\n"
    ).encode() + b"".join(chunks) + f"\r\n--{boundary}--".encode()
    try:
        response = httpx.post(
            _YOUTUBE_VIDEOS_INSERT_URL,
            params={"uploadType": "multipart", "part": "snippet,status"},
            headers={
                "Authorization": f"Bearer {_setting('youtube_access_token')}",
                "Content-Type": f"multipart/related; boundary={boundary}",
            },
            content=body,
            timeout=YOUTUBE_UPLOAD_TIMEOUT_SECONDS,
        )
    except httpx.TransportError as exc:
        # The upload POST can have been ingested before the connection died: ambiguous, so the
        # named class rather than a bare "failed" (a blind re-upload can publish twice).
        raise _no_answer("YouTube", exc, timeout_seconds=YOUTUBE_UPLOAD_TIMEOUT_SECONDS) from exc
    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"YouTube videos.insert returned {response.status_code}: {response.text[:300]}"
        )
    return {"id": str(response.json().get("id") or "")}


# ---------------------------------------------------------------------------
# The dispatch table: platform -> the ONE adapter that can really post there.
#
# U5/P1 replaced the old "twitter only" guard with this map. An unknown platform is refused BY
# NAME (never coerced to twitter, never a crash); each adapter refuses by name when it is not
# configured. Adding a platform = adding its adapter here (+ its RateCaps row and, when it can
# write rows, its entry in `channel_service._SOCIAL_CHANNELS`).
# ---------------------------------------------------------------------------
_PLATFORM_ADAPTERS: dict[str, Any] = {
    "twitter": _post_twitter,
    "linkedin": _post_linkedin,
    "facebook": _post_facebook,
    "instagram": _post_instagram,
    "tiktok": _post_tiktok,      # V8/T4
    "youtube": _post_youtube,    # V8/T4
}


def post_tweet(text: str, *, platform: str = "twitter", image_url: str = "",
               reply_to: str = "") -> dict:
    """Post one status update on `platform` — the ONE entry point the social agent calls.

    Each adapter owns its full contract (lazy import, credential refusal, real provider call) and
    every one of them refuses by NAME when it cannot really post. `image_url` is optional at this
    boundary: an adapter that cannot post media refuses by name when it is set (never a silent
    drop), and instagram REQUIRES it (its API has no text-only publish).

    V15/4-04 (S5-04): `reply_to` carries the provider's OWN id of the mention being answered.
    Only adapters that really thread accept it — twitter, via `create_tweet(
    in_reply_to_tweet_id=...)`; every other platform refuses BY NAME (never a silently detached
    post that merely looks like a reply).
    """
    adapter = _PLATFORM_ADAPTERS.get(platform)
    if adapter is None:
        return named_refusal(
            E_VALIDATION, f"post_tweet adapter does not handle platform {platform!r}",
            how_to_fix=(
                f"add a {platform!r} adapter to _PLATFORM_ADAPTERS or route it through a channel "
                f"it supports; supported platforms: {', '.join(sorted(_PLATFORM_ADAPTERS))}"
            ),
            platform=platform,
        )
    if reply_to and platform != "twitter":
        return named_refusal(
            E_VALIDATION,
            f"{platform} adapter does not support replies: threading is only implemented for "
            "twitter",
            how_to_fix=("post the reply on twitter, or extend the adapter with the platform's "
                        "own reply/thread parameter"),
            platform=platform, reply_to=reply_to,
        )
    if reply_to:
        return adapter(text, image_url=image_url, reply_to=reply_to)
    return adapter(text, image_url=image_url)


def _sendgrid_credentials() -> tuple[str, str]:
    """The SendGrid pair from the SAME source the Settings card writes: stored > env > Settings.

    F-W7-01 (wave 7): this function used to read `settings.sendgrid_api_key` only, while
    Settings -> Integrations wrote the value into the secret store — so a saved key showed
    "Configured" while every send refused "configuration missing". One resolver, one truth.
    """
    from app.services.secret_store import get_secret

    return (
        (get_secret("SENDGRID_API_KEY") or "").strip(),
        (get_secret("SENDGRID_FROM_EMAIL") or "").strip(),
    )


def send_email(*, to: str, subject: str, body: str) -> dict:
    """Send one email through SendGrid.

    `may_have_applied=True` on the call below: an email is a MUTATION, and a transport failure
    after the request was written can leave the message delivered while we never saw the 202.
    The refusal then names that ambiguity instead of reporting a definite "failed" (W7).
    """
    api_key, from_email = _sendgrid_credentials()
    missing = _missing_config(
        ("SENDGRID_API_KEY", api_key),
        ("SENDGRID_FROM_EMAIL", from_email),
    )
    if missing:
        return named_refusal(
            E_INTEGRATION, f"SendGrid configuration missing: {', '.join(missing)}",
            how_to_fix=_SENDGRID_HINT, missing=missing,
        )
    return integration_call(
        "sendgrid", "SendGridAPIClient", agent_type="email_outreach", install_hint=_SENDGRID_HINT,
        call=_sendgrid_send, may_have_applied=True, to=to, subject=subject, body=body,
        api_key=api_key, from_email=from_email,
    )


#: The SendGrid SDK ships NO default socket timeout: `SendGridAPIClient` builds a
#: `python_http_client.Client(timeout=None)` and every request then calls
#: `urlopen(..., timeout=None)`, which waits until the OS gives up on the connection — for a
#: half-open socket that is effectively forever, and one hung send would pin a Celery worker
#: slot. 20 s is the same bound the deploy (`DEPLOY_TIMEOUT_SECONDS`) and GitHub
#: (`GITHUB_TIMEOUT_S`) adapters already use.
SENDGRID_TIMEOUT_SECONDS = 20.0

#: SendGrid's documented success answer is 202 Accepted; 200/201 are accepted too rather than
#: inventing a refusal for a success. ANY other status is a refusal, never a recorded send.
SENDGRID_ACCEPTED_STATUSES = (200, 201, 202)

# ---------------------------------------------------------------------------
# THE DOCUMENTED MAIL SEND ANSWER TABLE (wave 9, SV-W8-02).
#
# Source: https://www.twilio.com/docs/sendgrid/api-reference/mail-send/mail-send
# (vendor `dateModified` 2025-09-25; fetched 2026-10-29).  The page documents EXACTLY:
#
#     202 Accepted
#     400 / 401 / 403 / 404 / 405 / 413 / 500   with the body
#     {"errors": [{"field": ..., "message": ..., "help": ...}], "id": "..."}
#
# WHY this table exists: before wave 9 every one of those answers reached the caller as the SAME
# flattened string (the pinned SDK's own `str(exc)` inside ONE `E_INTEGRATION`), so a 413 ("the
# message is too large"), a 401 ("the key is wrong") and a 500 ("SendGrid is the failing side,
# retry") were indistinguishable — and the retry decision cannot be made from an
# indistinguishable refusal.  The refusal SHAPE is still the ONE envelope; the ANSWER is named.
#
# `retryable` is a statement about the ANSWER, not about this codebase's retry policy: 5xx/429 is
# the provider's side and the same request may succeed later; a 4xx (including 413) is this
# caller's request and will fail identically until the payload/key/endpoint changes.
# ---------------------------------------------------------------------------
SENDGRID_ERROR_CLASSES: dict[int, tuple[str, bool]] = {
    400: ("caller_error", False),        # malformed request body (the page prints a field table)
    401: ("caller_error", False),        # missing/revoked API key
    403: ("caller_error", False),        # valid key, no permission to send
    404: ("caller_error", False),        # wrong endpoint/host
    405: ("caller_error", False),        # wrong method
    413: ("payload_too_large", False),   # the request body exceeds the API's limit
    500: ("upstream_error", True),       # SendGrid's side — the same request may succeed later
}
#: Answered statuses the pinned SDK can raise that the vendor page does NOT list. Kept in their
#: own table so `SENDGRID_ERROR_CLASSES` stays exactly the page, and this extension stays visible
#: (`python_http_client` 3.3.7 raises typed classes for 429/503/504). An answered status must
#: never be reported as an unclassified one.
SENDGRID_UNDOCUMENTED_STATUS_CLASSES: dict[int, tuple[str, bool]] = {
    429: ("rate_limited", True),
    503: ("upstream_error", True),
    504: ("upstream_error", True),
}

#: The correlation id this adapter mints for EVERY attempt and carries in the DOCUMENTED
#: `custom_args` body field ("values that are specific to the entire send ... carried along with
#: the email and its activity data"). It is the anchor for the `X-Message-Id` fallback below.
SENDGRID_CORRELATION_ARG = "meridian_correlation_id"
SENDGRID_CORRELATION_PREFIX = "meridian-"
#: `message_id_source` values on a successful send. A consumer must be able to tell the provider's
#: own id from "the provider accepted it and returned no id", or it will either fabricate one or
#: call an accepted send "failed".
SENDGRID_ID_SOURCE_HEADER = "response-header:x-message-id"
SENDGRID_ID_SOURCE_ABSENT = "absent"


def _sendgrid_error_class(status: int) -> tuple[str, bool]:
    """(class name, retryable) for ONE answered status — documented table first, then families."""
    if status in SENDGRID_ERROR_CLASSES:
        return SENDGRID_ERROR_CLASSES[status]
    if status in SENDGRID_UNDOCUMENTED_STATUS_CLASSES:
        return SENDGRID_UNDOCUMENTED_STATUS_CLASSES[status]
    if 500 <= status < 600:
        return ("upstream_error", True)
    if 400 <= status < 500:
        return ("caller_error", False)
    return ("unclassified", False)


def _sendgrid_answer_fields(body: Any) -> tuple[list[dict[str, str]], str]:
    """The DOCUMENTED answer surface of a Mail Send response: `errors[]` entries and body `id`.

    The vendor page documents the error body as `{"errors": [{"field", "message", "help"}],
    "id"}`.  Bytes (the pinned `python_http_client.HTTPError.body`), `str`, and an already-decoded
    mapping are all accepted, because the three shapes reach the adapter from different places
    (an exception, a returned response, a test double).  A body that is not that JSON yields
    `([], "")` — never a parse exception, never an invented field, never a guessed request id.

    Bounded on purpose: a refusal carries at most 5 entries of at most 200 characters each.  A
    provider response is attacker-adjacent input, and a refusal is not a log dump.
    """
    payload: Any = body
    if isinstance(body, bytes | bytearray):
        try:
            payload = json.loads(bytes(body).decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            return [], ""
    elif isinstance(body, str):
        try:
            payload = json.loads(body)
        except ValueError:
            return [], ""
    if not isinstance(payload, dict):
        return [], ""
    request_id = str(payload.get("id") or "").strip()[:200]
    errors: list[dict[str, str]] = []
    raw_errors = payload.get("errors")
    if isinstance(raw_errors, list):
        for entry in raw_errors[:5]:
            if not isinstance(entry, dict):
                continue
            errors.append({
                key: str(entry.get(key) or "").strip()[:200]
                for key in ("field", "message", "help")
            })
    return errors, request_id


class SendGridSendError(RuntimeError):
    """SendGrid ANSWERED the send with a status that is not an accepted one.

    Named class so ONE exception carries the whole documented answer: the status, its class
    (`payload_too_large` / `caller_error` / `upstream_error` / `rate_limited`), whether a retry is
    the right move, the provider's own `errors[{field,message,help}]` entries and its body `id`.
    `integration_call` turns it into the ordinary `E_INTEGRATION` refusal — the ONE envelope —
    with the classification in the message, so no caller can mistake a 413 for a 401 for a 500.

    Distinct from `ProviderTransportError` on purpose, in both directions: a send that produced
    an ANSWER is a definite "no" (no `outcome_unknown`), and a send that produced NO answer is the
    ambiguous class (the message may have gone out) — the two must never be collapsed.

    `retryable` is the ANSWER's verdict (`upstream_error`/`rate_limited` may succeed later;
    `caller_error`/`payload_too_large` will not until the request changes).  It is NOT an
    instruction to retry blindly: a 500 on a MUTATION still leaves the outcome unknown at the
    product level and an operator checks the provider's activity before re-sending.
    """

    def __init__(
        self,
        *,
        status: int,
        errors: list[dict[str, str]] | None = None,
        request_id: str = "",
        provider_detail: str = "",
        sdk_exception: str = "",
    ) -> None:
        self.status = int(status)
        self.error_class, self.retryable = _sendgrid_error_class(self.status)
        self.errors = list(errors or [])
        self.request_id = str(request_id or "").strip()[:200]
        self.provider_detail = str(provider_detail or "").strip()[:500]
        self.sdk_exception = str(sdk_exception or "").strip()
        super().__init__(self._render())

    def _render(self) -> str:
        rendered = (
            f"SendGrid rejected the send with HTTP {self.status} "
            f"({self.error_class}, retryable={'true' if self.retryable else 'false'}) — "
            f"not an accepted send status {SENDGRID_ACCEPTED_STATUSES}"
        )
        detail = ""
        if self.errors:
            detail = self.errors[0].get("message") or ""
        detail = detail or self.provider_detail
        if detail:
            rendered += f": {detail}"
        if self.errors:
            rendered += "; provider errors: [" + "; ".join(
                "field={field!r} message={message!r} help={help!r}".format(
                    field=entry.get("field", ""),
                    message=entry.get("message", ""),
                    help=entry.get("help", ""),
                )
                for entry in self.errors
            ) + "]"
        if self.request_id:
            rendered += f"; provider request id: {self.request_id}"
        if self.sdk_exception:
            rendered += f"; SDK exception: {self.sdk_exception}"
        return rendered


def _sendgrid_send(
    *, to: str, subject: str, body: str, api_key: str, from_email: str
) -> Any:  # pragma: no cover - provider
    import uuid

    from sendgrid import SendGridAPIClient
    from sendgrid.helpers import mail as mail_helpers

    Mail = mail_helpers.Mail
    # `CustomArg` is the message-level custom-arg wrapper. Read defensively: a client build that
    # models only `Mail` still exercises this adapter (the repo's own doubles do), and a missing
    # CustomArg must degrade to the LOGGED "no anchor" path, never to an ImportError that reads
    # like "the integration is not installed".
    custom_arg_cls = getattr(mail_helpers, "CustomArg", None)

    client = SendGridAPIClient(api_key)
    # The SDK exposes no timeout parameter; its underlying client reads `.timeout` on every
    # request (`python_http_client.client.Client._make_request`), so this is the ONE lever that
    # bounds the call (W7: measured — the pre-fix value was None). The `getattr` guard is not
    # licence to skip it: a pinned-contract test builds the REAL 6.11.0 client and asserts the
    # attribute exists, so an SDK bump that moved it fails THAT test instead of silently
    # restoring an unbounded send. A test double that models only `send()` still exercises the
    # adapter; when the lever is genuinely absent the miss is LOGGED, never swallowed.
    inner = getattr(client, "client", None)
    if inner is not None:
        inner.timeout = SENDGRID_TIMEOUT_SECONDS
    else:  # pragma: no cover - the pinned SDK always has one; a bump would land here
        logger.warning(
            "sendgrid: the installed SDK object exposes no `.client` to bound the call; this "
            "send is UNBOUNDED until the adapter is updated for the new SDK shape"
        )
    message = Mail(from_email=from_email, to_emails=to,
                   subject=subject, plain_text_content=body)

    # X-Message-Id — THE MEASURED CONTRACT, AND WHY THE FALLBACK IS ANCHORED (wave 9, SV-W8-03).
    #
    # MEASURED: the Mail Send page (vendor `dateModified` 2025-09-25) documents its answers as the
    # status codes in `SENDGRID_ERROR_CLASSES` plus the `{"errors": [...], "id": ...}` body; it
    # documents NO response headers at all, and the string `X-Message-Id` appears ZERO times on
    # it (systems-validator, wave 8, SV-W8-03). The header IS present on real 202 answers, and it
    # is the per-send id this adapter has returned since RD14 — so the dependency is real, is
    # UNDOCUMENTED, and is now pinned by a test (`test_w9_sendgrid_error_mapping.py`) instead of
    # being implied by a comment. If SendGrid ever stops sending it, that test does not fail (it
    # would be a real-provider measurement) — but the fallback below means the product still has
    # an anchored key, and the absence is logged: the failure mode is visible, not silent.
    #
    # WHY a key at all: `email_logs.sendgrid_id` is the dedupe key (`record_sent_emails` skips an
    # item without one), and the STATUS CODE is not a key — keying on "202" collapsed distinct
    # sends onto ONE row (the RD14 measurement: 3 distinct sends -> 1 row).
    #
    # THE FALLBACK IS ANCHORED, NOT FABRICATED. Every attempt mints `correlation_id` BEFORE the
    # request and carries it in the DOCUMENTED `custom_args` body field ("values that are specific
    # to the entire send ... carried along with the email and its activity data"), so on a 202
    # that arrives WITHOUT `X-Message-Id`:
    #   * `message_id` stays "" — a provider id is never invented (`provider_message_id()`'s
    #     "the provider's OWN id" stays true; it is the DEDUPE key that gets a fallback, below);
    #   * `message_id_source` says "absent" and the absence is LOGGED (never silent);
    #   * `correlation_id` is returned — a value that really travelled with THIS message and can
    #     be found in SendGrid's own activity data, so the send can be traced and deduped without
    #     a fabricated SendGrid id.
    # A consumer that records `email_logs.sendgrid_id` must therefore use `correlation_id` (and
    # keep the source) when `message_id_source == "absent"`, rather than recording "" (which would
    # call an accepted send "failed") or the status code (which would merge sends).
    # REQUESTS-W9 item 1 is that one-line consumer change in celery_app/tasks/agent_tasks.py
    # (another lane's file).
    correlation_id = f"{SENDGRID_CORRELATION_PREFIX}{uuid.uuid4().hex}"
    add_custom_arg = getattr(message, "add_custom_arg", None)
    if callable(add_custom_arg) and custom_arg_cls is not None:
        add_custom_arg(custom_arg_cls(SENDGRID_CORRELATION_ARG, correlation_id))
    else:  # pragma: no cover - the pinned SDK always has both (contract-pinned by a test)
        logger.warning(
            "sendgrid: the installed SDK exposes no Mail.add_custom_arg/CustomArg, so this send "
            "carries NO correlation anchor; a missing X-Message-Id on the answer will be "
            "untraceable at the provider (refusing to invent an id)"
        )

    try:
        response = client.send(message)
    except Exception as exc:
        # `python_http_client` attaches `status_code` to every typed HTTPError — the provider
        # ANSWERED (a 400/401/403/404/405/413/500/429/5xx is a real answer). An exception with no
        # status is a transport fault: the request may have been applied and no answer came back,
        # so it is raised as the distinct ambiguity class (never as an "answer").
        status = getattr(exc, "status_code", None)
        if status is None:
            raise ProviderTransportError(
                f"SendGrid did not answer (transport fault or timeout after "
                f"{SENDGRID_TIMEOUT_SECONDS:g}s): {type(exc).__name__}: {exc}",
                timeout_seconds=SENDGRID_TIMEOUT_SECONDS,
            ) from exc
        errors, request_id = _sendgrid_answer_fields(getattr(exc, "body", None))
        raise SendGridSendError(
            status=int(status),
            errors=errors,
            request_id=request_id,
            provider_detail=str(exc),
            sdk_exception=type(exc).__name__,
        ) from exc
    status = int(getattr(response, "status_code", 0) or 0)
    if status not in SENDGRID_ACCEPTED_STATUSES:
        # Defence in depth for a client build that RETURNS an error response instead of raising:
        # `integration_call` turns this into the named E_INTEGRATION refusal with the documented
        # classification, never a success and never a bare "failed".
        answer_body = getattr(response, "body", None)
        errors, request_id = _sendgrid_answer_fields(answer_body)
        raise SendGridSendError(
            status=status,
            errors=errors,
            request_id=request_id,
            provider_detail=str(answer_body or ""),
        )
    message_id = _response_header(response, "x-message-id")
    if not message_id:
        logger.warning(
            "sendgrid: the accepted send (HTTP %s) carried no X-Message-Id response header (the "
            "header is not on the vendor's Mail Send page); message_id stays empty and the "
            "anchored correlation id %s was carried in custom_args[%s] — record THAT as the "
            "dedupe key, never the status code",
            status, correlation_id, SENDGRID_CORRELATION_ARG,
        )
    return {
        "status_code": status,
        "message_id": message_id,
        "message_id_source": (
            SENDGRID_ID_SOURCE_HEADER if message_id else SENDGRID_ID_SOURCE_ABSENT
        ),
        "correlation_id": correlation_id,
    }


def open_pull_request(*, repo: str, branch: str, title: str, body: str,
                      base: str = "main") -> dict:
    """Open a pull request. Irreversible: the firewall requires an approval token for this."""
    missing = _missing_config(("GITHUB_TOKEN", settings.github_token))
    if missing:
        return named_refusal(
            E_INTEGRATION, f"GitHub credentials missing: {', '.join(missing)}",
            how_to_fix=_PYGITHUB_HINT, missing=missing,
        )
    return integration_call(
        "github", "Github", agent_type="code_generation", install_hint=_PYGITHUB_HINT,
        call=_pygithub_open_pr, may_have_applied=True,
        repo=repo, branch=branch, title=title, body=body, base=base,
    )


def _pygithub_open_pr(*, repo: str, branch: str, title: str, body: str, base: str) -> Any:  # pragma: no cover
    import requests
    from github import Github

    from app.services.github_service import GITHUB_TIMEOUT_S

    # PyGithub's OWN defaults are a 15 s timeout and a 10-attempt urllib3 retry that INCLUDES
    # POST — i.e. the SDK would silently re-submit a pull request on a 5xx/connection fault.  A PR
    # is a mutation, so this adapter owns both knobs: one explicit bound and no internal retries.
    client = Github(settings.github_token, timeout=int(GITHUB_TIMEOUT_S), retry=0)
    repository = client.get_repo(repo)
    try:
        pr = repository.create_pull(title=title, body=body, head=branch, base=base)
    except requests.RequestException as exc:
        # `github.GithubException` (the provider ANSWERED with a status) is not a requests
        # exception and propagates untouched; a requests transport exception means NO answer.
        raise _no_answer("GitHub", exc, timeout_seconds=GITHUB_TIMEOUT_S) from exc
    return {"number": int(pr.number), "url": str(pr.html_url)}


def refund_payment(*, payment_id: str, amount_cents: int | None = None) -> dict:
    """Issue a Stripe refund against a PaymentIntent.  Irreversible: approval-gated.

    FAIL-CLOSED identifier guard (L3-10): Stripe's create-refund call identifies its target by
    `payment_intent` (a `pi_…` id) or `charge`; an EVENT id (`evt_…`) is not a refundable
    resource, and the finance agent's old shape passed its event id straight through as
    `payment_intent` — so the only money-moving call in the product could never succeed.
    Only `pi_`-prefixed ids reach the provider; anything else (`evt_…`, a fabricated id,
    a bare integer string) is refused by NAME with `E_VALIDATION` and NO network call — the
    value may be the provider's real id of the WRONG type, which is a caller error, not an
    integration failure.  Adding a `ch_…`/`py_…` route is a documented limitation (tracked as a
    REQUESTS item), never a best-effort guess here.

    `amount_cents` caps a partial refund; `None` refunds what is left on the PaymentIntent.
    """
    identifier = str(payment_id or "").strip()
    if not identifier.startswith("pi_"):
        return named_refusal(
            E_VALIDATION,
            f"refund_payment needs a Stripe PaymentIntent id (pi_…), got {identifier!r}",
            how_to_fix=(
                "read the failed charge's PaymentIntent id (pi_…) from Stripe — an event id "
                "(evt_…) is provenance and can never refund a charge"
            ),
            payment_id=identifier,
        )
    missing = _missing_config(("STRIPE_SECRET_KEY", settings.stripe_secret_key))
    if missing:
        return named_refusal(
            E_INTEGRATION, f"Stripe credentials missing: {', '.join(missing)}",
            how_to_fix=_STRIPE_HINT, missing=missing,
        )
    return integration_call(
        "stripe", "Refund", agent_type="finance", install_hint=_STRIPE_HINT,
        call=_stripe_refund, may_have_applied=True,
        payment_id=identifier, amount_cents=amount_cents,
    )


def _stripe_refund(*, payment_id: str, amount_cents: int | None) -> Any:  # pragma: no cover
    import stripe

    # ONE client per call, bounded by this adapter instead of the SDK's own defaults: stripe 11.3
    # ships an 80 s socket timeout and `max_network_retries = 2`, so a connection fault would
    # silently re-POST a REFUND (a duplicate money movement).  `max_network_retries=0` makes this
    # adapter the only retry authority; a retry after an ambiguous answer is the operator's call.
    client = stripe.StripeClient(
        settings.stripe_secret_key,
        http_client=stripe.RequestsClient(timeout=int(STRIPE_TIMEOUT_SECONDS)),
        max_network_retries=0,
    )
    params: Any = {"payment_intent": payment_id}
    if amount_cents is not None:
        params["amount"] = int(amount_cents)
    try:
        refund = client.refunds.create(params)
    except stripe.StripeError as exc:
        # A StripeError carrying an `http_status` is the provider's ANSWER (400/401/402/…), and the
        # generic handler reports that status text.  One WITHOUT it (`APIConnectionError`) means
        # no answer arrived: ambiguous for money, so the named class — never a bare "failed".
        if getattr(exc, "http_status", None) is None:
            raise _no_answer("Stripe", exc, timeout_seconds=STRIPE_TIMEOUT_SECONDS) from exc
        raise
    return {"id": str(refund.get("id")), "status": str(refund.get("status"))}


def update_ad_budget(*, platform: str, campaign_ref: str, budget_change_usd: float) -> dict:
    """Mutate a live ad campaign's budget.

    The advertising APIs need an OAuth client + a customer/account id that this deployment does
    not provision, so this adapter refuses by NAME instead of pretending: the recommendation is
    still returned and recorded, and the operator sees exactly what is missing.
    """
    if platform == "google_ads":
        missing = _missing_config(
            ("GOOGLE_ADS_DEVELOPER_TOKEN", settings.google_ads_developer_token),
            ("GOOGLE_ADS_CLIENT_ID", settings.google_ads_client_id),
            ("GOOGLE_ADS_REFRESH_TOKEN", settings.google_ads_refresh_token),
            ("GOOGLE_ADS_CUSTOMER_ID", settings.google_ads_customer_id),
        )
    elif platform == "meta_ads":
        missing = _missing_config(
            ("META_ADS_ACCESS_TOKEN", settings.meta_ads_access_token),
            ("META_ADS_ACCOUNT_ID", settings.meta_ads_account_id),
        )
    else:
        return named_refusal(E_VALIDATION, f"unknown ad platform {platform!r}",
                             how_to_fix="use google_ads or meta_ads", platform=platform)
    if missing:
        return named_refusal(
            E_INTEGRATION,
            f"{platform} mutation is not configured: {', '.join(missing)}",
            how_to_fix=_ADS_HINT, platform=platform, campaign_ref=campaign_ref, missing=missing,
        )
    return named_refusal(
        E_INTEGRATION,
        f"{platform} budget mutation needs an account-scoped API client that this deployment "
        f"does not ship; the recommendation was recorded instead",
        how_to_fix=_ADS_HINT,
        platform=platform, campaign_ref=campaign_ref, budget_change_usd=budget_change_usd,
    )


# ---------------------------------------------------------------------------
# METRICS / FACTS READS (V15/4-02 + 4-03) — REAL provider reads, config-gated.
#
# These are INPUT legs: they read what a run needs to DECIDE, and mutate nothing.  Each refuses
# BY NAME when its credentials are unset (E_INTEGRATION, `missing=[...]`): a run that cannot read
# real numbers must say so in its prompt and its output — never invent metrics.  The providers
# are the pinned libraries (google-ads 24.1.0 / facebook-business 21.0.0 / stripe 11.3.0);
# nothing here is a mock — tests stub ONLY the client boundary.
#
# Money discipline (the repo's INTEGER-CENTS law): provider USD values are converted to cents
# once, here, and every derived ratio (`roas`) is computed from the cent values; a zero spend
# yields `roas: None` ("unmeasurable"), never a fabricated ratio.
#
# V24/L4 revenue truth: the envelope's `revenue_usd` is the provider's OWN revenue measurement,
# never a derivation from clicks or impressions:
#   * Google Ads — `metrics.conversions_value` (the value the account attributes to conversions);
#   * Meta — `purchase_roas` × measured spend (the provider's own return on ad spend,
#     de-normalised to dollars at the same attribution).  When Meta does not return
#     `purchase_roas`, the purchase action value (`action_values`, already the legacy
#     `conversions_value_usd` source) is the fallback revenue measurement — it is the same
#     purchase value the platform reports, so it is measured, not guessed.
# A provider payload that carries neither leaves `revenue_usd: None` and `roas: None`; nothing is
# estimated.  `revenue_basis` names which provider field was used (or is None).
# ---------------------------------------------------------------------------


def _day_window(days: int) -> tuple[date, date]:
    """The inclusive UTC day window `days` covers (today is the last day).

    The end is the UTC day, not the worker process's local day (E7 F-06): this window is sent to
    Google Ads (`segments.date BETWEEN`), Meta (`time_range`) and Stripe (`created.gte`), all of
    which read it as a calendar date, so a host clock west/east of UTC shifted every provider
    window by the offset while the envelope and these docstrings claimed UTC.  `today_utc()` is
    the project's one UTC-day helper (`app.core.timeutil`) — never `date.today()`.
    """
    end = today_utc()
    return end - timedelta(days=max(1, int(days or 1)) - 1), end


def _as_float(value: Any) -> float | None:
    """A provider string/number as float, or None — never an exception, never a guess."""
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _usd_to_cents(value: Any) -> int | None:
    """A provider USD string ('10.50') as INTEGER CENTS (money-exact via Decimal), or None."""
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    try:
        from decimal import Decimal, InvalidOperation

        return int((Decimal(text) * 100).to_integral_value())
    except (ImportError, InvalidOperation, ValueError):
        return None


def _cents_to_usd(cents: int | None) -> float | None:
    """Integer cents as a wire USD float, or None — never a fabricated `$0.00` for "unknown"."""
    return None if cents is None else round(cents / 100.0, 2)


def _metrics_result(platform: str, days: int, buckets: list[dict[str, Any]]) -> dict[str, Any]:
    """Assemble per-campaign rows + totals from aggregated provider facts.

    `buckets` entries carry exact, OPTIONAL cents (`spend_cents`, `value_cents`,
    `revenue_cents`) and a `revenue_basis` naming the provider field the revenue came from.
    Every USD field is a cents-derived rounding OF the measured numbers; `revenue_usd` and
    `conversions_value_usd` are `None` when the provider exposed nothing to measure, and `roas`
    is `None` unless BOTH revenue and spend are measured (a zero or absent denominator is an
    unmeasurable ratio — never `0`).

    Totals: revenue sums the measured rows; `roas` divides by the spend of the rows where BOTH
    were measured, so a measured-revenue campaign is never diluted by an unmeasured one.
    """
    campaigns: list[dict[str, Any]] = []
    total_spend_cents = 0
    total_value_cents = 0
    total_revenue_cents = 0
    paired_spend_cents = 0
    total_conversions = 0.0
    any_spend = False
    any_value = False
    any_revenue = False
    any_paired = False
    for bucket in buckets:
        spend_cents = bucket.get("spend_cents")
        value_cents = bucket.get("value_cents")
        revenue_cents = bucket.get("revenue_cents")
        conversions = float(bucket.get("conversions") or 0.0)
        if spend_cents is not None:
            total_spend_cents += int(spend_cents)
            any_spend = True
        if value_cents is not None:
            total_value_cents += int(value_cents)
            any_value = True
        if revenue_cents is not None:
            total_revenue_cents += int(revenue_cents)
            any_revenue = True
            if spend_cents is not None:
                paired_spend_cents += int(spend_cents)
                any_paired = True
        total_conversions += conversions
        measured_roas = (
            round(revenue_cents / spend_cents, 4)
            if revenue_cents is not None and spend_cents
            else None
        )
        campaigns.append({
            "campaign_id": str(bucket.get("campaign_id") or ""),
            "name": str(bucket.get("name") or ""),
            "status": str(bucket.get("status") or ""),
            "spend_cents": int(spend_cents) if spend_cents is not None else 0,
            "spend_usd": _cents_to_usd(spend_cents),
            "spend_measured": spend_cents is not None,
            "conversions": conversions,
            "conversions_value_usd": _cents_to_usd(value_cents),
            "revenue_usd": _cents_to_usd(revenue_cents),
            "revenue_basis": bucket.get("revenue_basis"),
            "roas": measured_roas,
        })
    start, end = _day_window(days)
    return {
        "platform": platform,
        "days": int(days),
        "window": {"since": start.isoformat(), "until": end.isoformat()},
        "fetched_at": now_utc().isoformat(),
        "campaigns": campaigns,
        "totals": {
            "spend_cents": total_spend_cents,
            "spend_usd": _cents_to_usd(total_spend_cents) if any_spend else None,
            "conversions": total_conversions,
            "conversions_value_usd": _cents_to_usd(total_value_cents) if any_value else None,
            "revenue_usd": _cents_to_usd(total_revenue_cents) if any_revenue else None,
            "roas": (
                round(total_revenue_cents / paired_spend_cents, 4)
                if any_paired and paired_spend_cents
                else None
            ),
        },
    }


def _google_metric_bucket(aggregated: dict[str, dict[str, Any]], row: Any) -> None:
    """Fold one GAQL row into its campaign bucket, preserving MEASURED vs absent revenue.

    `metrics.conversions_value` is Google's own conversion-value measurement.  It is optional in
    the response proto: a row that does not carry it contributes NOTHING to revenue (the bucket
    stays `value_measured=False`), never a zero.  `cost_micros` is always present on a metrics
    row, so spend counts as measured.
    """
    bucket = aggregated.setdefault(str(row.campaign.id), {
        "campaign_id": str(row.campaign.id),
        "name": str(row.campaign.name or ""),
        "status": getattr(row.campaign.status, "name", None) or str(row.campaign.status or ""),
        "micros": 0,
        "value_cents": 0,
        "value_measured": False,
        "conversions": 0.0,
    })
    metrics = row.metrics
    bucket["micros"] += int(getattr(metrics, "cost_micros", 0) or 0)
    value_cents = _usd_to_cents(getattr(metrics, "conversions_value", None))
    if value_cents is not None:
        bucket["value_cents"] += value_cents
        bucket["value_measured"] = True
    bucket["conversions"] += _as_float(getattr(metrics, "conversions", 0.0)) or 0.0


def _google_ads_read(days: int) -> Any:  # pragma: no cover - requires the provider
    """GAQL campaign-metrics read over the day window (ONE documented query shape)."""
    from google.ads.googleads.client import GoogleAdsClient

    customer_id = str(settings.google_ads_customer_id).strip()
    config: dict[str, Any] = {
        "developer_token": settings.google_ads_developer_token,
        "client_id": settings.google_ads_client_id,
        "client_secret": settings.google_ads_client_secret,
        "refresh_token": settings.google_ads_refresh_token,
        "use_proto_plus": True,
    }
    if customer_id:
        config["login_customer_id"] = customer_id
    client = GoogleAdsClient.load_from_dict(config)
    service = client.get_service("GoogleAdsService")
    start, end = _day_window(days)
    query = (
        "SELECT campaign.id, campaign.name, campaign.status, metrics.cost_micros, "
        "metrics.conversions, metrics.conversions_value, segments.date "
        "FROM campaign "
        f"WHERE segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}' "
        "ORDER BY segments.date DESC"
    )
    aggregated: dict[str, dict[str, Any]] = {}
    for row in service.search(customer_id=customer_id, query=query):
        _google_metric_bucket(aggregated, row)
    buckets: list[dict[str, Any]] = []
    for bucket in aggregated.values():
        spend_cents = int(bucket.pop("micros")) // 10_000        # micros -> cents
        measured = bool(bucket.pop("value_measured", False))
        value_cents = int(bucket.pop("value_cents") or 0) if measured else None
        buckets.append({
            **bucket,
            "spend_cents": spend_cents,
            "value_cents": value_cents,
            "revenue_cents": value_cents,                        # Google's OWN revenue measure
            "revenue_basis": "google.conversions_value" if measured else None,
        })
    return _metrics_result("google_ads", days, buckets)


def _first_purchase_value(entries: Any) -> Any:
    """The purchase action's value from Meta insights `actions` / `action_values`, or None."""
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if "purchase" in str(entry.get("action_type") or "").lower():
            return entry.get("value")
    return None


def _purchase_roas_value(entries: Any) -> float | None:
    """Meta's own `purchase_roas` for the purchase action, or None.

    The insights field is a list of `{action_type, value}` entries (value is a string multiple,
    e.g. `"2.5"`).  Only a finite, non-negative multiple is a measurement; anything else is
    absent — never coerced to 0.
    """
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if "purchase" in str(entry.get("action_type") or "").lower():
            value = _as_float(entry.get("value"))
            if value is not None and value >= 0:
                return value
    return None


def _roas_times_spend_cents(roas_value: float | None, spend_cents: int | None) -> int | None:
    """`purchase_roas × measured spend` in integer cents — the provider's own revenue figure.

    A ratio without a measured spend has no dollar magnitude (None); the multiplication is done
    in Decimal so a `"2.5"` multiple over `1050` cents lands `2625` exactly, not a float artefact.
    """
    if roas_value is None or spend_cents is None:
        return None
    try:
        from decimal import Decimal, InvalidOperation

        return int((Decimal(str(roas_value)) * Decimal(spend_cents)).to_integral_value())
    except (ImportError, InvalidOperation, ValueError):
        return None


def _meta_ads_read(days: int) -> Any:  # pragma: no cover - requires the provider
    """Campaign-level insights read through the Graph API (facebook-business).

    Revenue: `purchase_roas` × measured `spend` when BOTH are present — the provider's own return
    on ad spend de-normalised to dollars.  When Meta does not return `purchase_roas`, the purchase
    action value (`action_values`) is the fallback measured revenue (the same value the legacy
    `conversions_value_usd` exposes).  When neither is returned, `revenue_usd` is None and
    `roas` is None: this reader never derives revenue from clicks or from an unmeasured spend.
    """
    from facebook_business.adobjects.adaccount import AdAccount
    from facebook_business.api import FacebookAdsApi

    FacebookAdsApi.init(access_token=_setting("meta_ads_access_token"))
    account = AdAccount(f"act_{str(settings.meta_ads_account_id).strip()}")
    start, end = _day_window(days)
    rows = account.get_insights(
        fields=[
            "campaign_id", "campaign_name", "spend", "actions", "action_values", "purchase_roas",
        ],
        params={
            "level": "campaign",
            "time_range": {"since": start.isoformat(), "until": end.isoformat()},
        },
    )
    buckets: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        spend_cents = _usd_to_cents(data.get("spend"))
        value_cents = _usd_to_cents(_first_purchase_value(data.get("action_values")))
        revenue_cents = _roas_times_spend_cents(
            _purchase_roas_value(data.get("purchase_roas")), spend_cents
        )
        if revenue_cents is not None:
            revenue_basis = "meta.purchase_roas_x_spend"
        elif value_cents is not None:
            revenue_cents = value_cents
            revenue_basis = "meta.purchase_action_value"
        else:
            revenue_basis = None
        buckets.append({
            "campaign_id": str(data.get("campaign_id") or ""),
            "name": str(data.get("campaign_name") or ""),
            "status": "",
            "spend_cents": spend_cents,
            "value_cents": value_cents,
            "revenue_cents": revenue_cents,
            "revenue_basis": revenue_basis,
            "conversions": _as_float(_first_purchase_value(data.get("actions"))) or 0.0,
        })
    return _metrics_result("meta_ads", days, buckets)


def read_ad_metrics(*, platform: str = "google_ads", days: int = 7) -> dict[str, Any]:
    """Read the campaigns' OWN metrics from the ad provider — the ads run's input leg.

    H2-frozen interface (V15/4-02, branch "a" LOCKED): returns the standard integration envelope
    — `{"ok": True, "result": {platform, days, window, fetched_at, campaigns[], totals}}` with
    money in INTEGER CENTS plus wire-USD fields, and `roas` from measured values only — or a
    NAMED refusal (`E_INTEGRATION` naming the missing credentials; `E_VALIDATION` for an unknown
    platform).  The worker attach stores this envelope VERBATIM under `metadata["ad_metrics"]`;
    the agent consumes it via `ad_metrics_extra_context`.

    V24/L4: each campaign carries the provider's own `revenue_usd` (`revenue_basis` names the
    field it came from) and `spend_measured`.  `revenue_usd`/`conversions_value_usd`/`roas` are
    `None` when the provider exposed nothing to measure — never `0` as a stand-in for absence.
    """
    normalized_days = max(1, int(days or 1))
    if platform == "google_ads":
        missing = _missing_config(
            ("GOOGLE_ADS_DEVELOPER_TOKEN", settings.google_ads_developer_token),
            ("GOOGLE_ADS_CLIENT_ID", settings.google_ads_client_id),
            ("GOOGLE_ADS_CLIENT_SECRET", settings.google_ads_client_secret),
            ("GOOGLE_ADS_REFRESH_TOKEN", settings.google_ads_refresh_token),
            ("GOOGLE_ADS_CUSTOMER_ID", settings.google_ads_customer_id),
        )
        if missing:
            return named_refusal(
                E_INTEGRATION,
                f"google_ads metrics read is not configured: {', '.join(missing)}",
                how_to_fix=_ADS_HINT, platform=platform, missing=missing,
            )
        return integration_call(
            "google.ads.googleads.client", "GoogleAdsClient", agent_type="ads_management",
            install_hint=_ADS_HINT, call=_google_ads_read, days=normalized_days,
        )
    if platform == "meta_ads":
        missing = _missing_config(
            ("META_ADS_ACCESS_TOKEN", _setting("meta_ads_access_token")),
            ("META_ADS_ACCOUNT_ID", _setting("meta_ads_account_id")),
        )
        if missing:
            return named_refusal(
                E_INTEGRATION,
                f"meta_ads metrics read is not configured: {', '.join(missing)}",
                how_to_fix=_ADS_HINT, platform=platform, missing=missing,
            )
        return integration_call(
            "facebook_business.adobjects.adaccount", "AdAccount", agent_type="ads_management",
            install_hint=_ADS_HINT, call=_meta_ads_read, days=normalized_days,
        )
    return named_refusal(
        E_VALIDATION, f"read_ad_metrics does not handle platform {platform!r}",
        how_to_fix="use google_ads or meta_ads", platform=platform,
    )


def _stripe_read_revenue(days: int) -> Any:  # pragma: no cover - requires the provider
    """Succeeded charges in the window + the account's available balance, per currency."""
    import stripe

    stripe.api_key = settings.stripe_secret_key
    start, end = _day_window(days)
    start_ts = int(datetime.combine(start, time.min, tzinfo=UTC).timestamp())
    charges = stripe.Charge.list(created={"gte": start_ts}, limit=100)
    by_currency: dict[str, dict[str, int]] = {}
    for charge in charges.auto_paging_iter():
        if str(charge.get("status") or "") != "succeeded":
            continue
        currency = str(charge.get("currency") or "unknown").lower()
        bucket = by_currency.setdefault(
            currency, {"count": 0, "gross_cents": 0, "refunded_cents": 0}
        )
        bucket["count"] += 1
        bucket["gross_cents"] += int(charge.get("amount") or 0)
        bucket["refunded_cents"] += int(charge.get("amount_refunded") or 0)
    for bucket in by_currency.values():
        bucket["net_cents"] = bucket["gross_cents"] - bucket["refunded_cents"]
    balance = stripe.Balance.retrieve()
    available = [
        {"amount_cents": int(entry.get("amount") or 0),
         "currency": str(entry.get("currency") or "").lower()}
        for entry in (balance.get("available") or []) if isinstance(entry, dict)
    ]
    return {
        "days": int(days),
        "window": {"since": start.isoformat(), "until": end.isoformat()},
        "fetched_at": now_utc().isoformat(),
        "charges_by_currency": by_currency,
        "currencies": sorted(by_currency),
        "balance_available": available,
    }


def read_revenue_facts(*, days: int = 30) -> dict[str, Any]:
    """Read the company's REAL revenue facts from Stripe — the finance run's measurement path.

    H3-frozen interface (V15/4-03, branch "a" LOCKED): succeeded charges aggregated per currency
    (`charges_by_currency`: count/gross_cents/refunded_cents/net_cents) plus the account's
    available balance (`balance_available`, integer cents), or a NAMED `E_INTEGRATION` refusal
    while `STRIPE_SECRET_KEY` is unset — no snapshot may be born from a refusal.  The worker
    attach stores this envelope VERBATIM under `metadata["revenue_facts"]`; the agent consumes
    it via `revenue_facts_extra_context`.
    """
    missing = _missing_config(("STRIPE_SECRET_KEY", settings.stripe_secret_key))
    if missing:
        return named_refusal(
            E_INTEGRATION,
            f"Stripe revenue read is not configured: {', '.join(missing)}",
            how_to_fix=_STRIPE_HINT, missing=missing,
        )
    return integration_call(
        "stripe", "Charge", agent_type="finance", install_hint=_STRIPE_HINT,
        call=_stripe_read_revenue, days=max(1, int(days or 1)),
    )


__all__ = [
    "SENDGRID_ACCEPTED_STATUSES",
    "SENDGRID_CORRELATION_ARG",
    "SENDGRID_CORRELATION_PREFIX",
    "SENDGRID_ERROR_CLASSES",
    "SENDGRID_ID_SOURCE_ABSENT",
    "SENDGRID_ID_SOURCE_HEADER",
    "SENDGRID_TIMEOUT_SECONDS",
    "SENDGRID_UNDOCUMENTED_STATUS_CLASSES",
    "SendGridSendError",
    "SOCIAL_POST_TIMEOUT_SECONDS",
    "STRIPE_TIMEOUT_SECONDS",
    "TWITTER_TIMEOUT_SECONDS",
    "YOUTUBE_MEDIA_TIMEOUT_SECONDS",
    "YOUTUBE_UPLOAD_TIMEOUT_SECONDS",
    "open_pull_request",
    "post_tweet",
    "read_ad_metrics",
    "read_revenue_facts",
    "refund_payment",
    "send_email",
    "update_ad_budget",
]
