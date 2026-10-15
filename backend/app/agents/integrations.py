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

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.parse import urlsplit

from app.agents.side_effects import integration_call, named_refusal
from app.agents.tool_registry import egress_policy_refusal
from app.config import settings
from app.core.errors import E_INTEGRATION, E_VALIDATION
from app.core.timeutil import now_utc

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
        call=_tweepy_create_tweet, text=text, reply_to=reply_to,
    )


def _tweepy_create_tweet(text: str, reply_to: str = "") -> Any:  # pragma: no cover - provider
    import tweepy

    client = tweepy.Client(
        consumer_key=settings.twitter_api_key,
        consumer_secret=settings.twitter_api_secret,
        access_token=settings.twitter_access_token,
        access_token_secret=settings.twitter_access_secret,
    )
    kwargs: dict[str, Any] = {}
    if reply_to:
        kwargs["in_reply_to_tweet_id"] = reply_to
    response = client.create_tweet(text=text, **kwargs)
    return {"id": str(response.data.get("id"))}


def _post_linkedin(text: str, *, image_url: str = "") -> dict:
    """Post one text update to LinkedIn through the versioned REST API (`/rest/posts`).

    Credentials are read defensively (`_setting`): LINKEDIN_ACCESS_TOKEN + LINKEDIN_AUTHOR_URN.
    A media post is refused BY NAME — LinkedIn's images API is a deliberate follow-up, never a
    silent drop of the image.
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
        call=_linkedin_create_post, text=text,
    )


def _linkedin_create_post(text: str) -> Any:  # pragma: no cover - requires the provider
    import httpx

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
        timeout=30.0,
    )
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
        call=_facebook_create_post, text=text,
    )


def _facebook_create_post(text: str) -> Any:  # pragma: no cover - requires the provider
    import httpx

    page_id = _setting("meta_facebook_page_id")
    response = httpx.post(
        f"{_GRAPH_BASE}/{page_id}/feed",
        data={"message": text, "access_token": _setting("meta_page_access_token")},
        timeout=30.0,
    )
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
        call=_instagram_create_post, text=text, image_url=str(image_url),
    )


def _instagram_create_post(text: str, image_url: str) -> Any:  # pragma: no cover - provider
    import httpx

    token = _setting("meta_page_access_token")
    user_id = _setting("meta_instagram_user_id")
    container = httpx.post(
        f"{_GRAPH_BASE}/{user_id}/media",
        data={"image_url": image_url, "caption": text, "access_token": token},
        timeout=30.0,
    )
    if container.status_code not in (200, 201):
        raise RuntimeError(
            f"Instagram media container returned {container.status_code}: {container.text[:300]}"
        )
    creation_id = str(container.json().get("id") or "")
    if not creation_id:
        raise RuntimeError("Instagram media container returned no id")
    published = httpx.post(
        f"{_GRAPH_BASE}/{user_id}/media_publish",
        data={"creation_id": creation_id, "access_token": token},
        timeout=30.0,
    )
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
        call=_tiktok_publish_video, text=text, image_url=media_url,
    )


def _tiktok_publish_video(text: str, image_url: str) -> Any:  # pragma: no cover - provider
    import httpx

    # ONE documented call: POST /v2/post/publish/video/init/ (Direct Post). `publish_id` is
    # TikTok's id for the publish request; the PULL_FROM_URL fetch + processing complete
    # asynchronously (status/fetch is the documented polling endpoint — a deliberate follow-up,
    # never faked here).
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
        timeout=30.0,
    )
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
        call=_youtube_insert_video, text=text, image_url=media_url,
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
    with httpx.stream("GET", image_url, timeout=60.0, follow_redirects=False) as media:
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
    boundary = f"polsia-{uuid.uuid4().hex}"
    metadata = json.dumps({
        "snippet": {"title": text[:100], "description": text[:5000]},   # YouTube's own limits
        "status": {"privacyStatus": "public"},
    })
    body = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{metadata}\r\n"
        f"--{boundary}\r\nContent-Type: {media_type}\r\n\r\n"
    ).encode() + b"".join(chunks) + f"\r\n--{boundary}--".encode()
    response = httpx.post(
        _YOUTUBE_VIDEOS_INSERT_URL,
        params={"uploadType": "multipart", "part": "snippet,status"},
        headers={
            "Authorization": f"Bearer {_setting('youtube_access_token')}",
            "Content-Type": f"multipart/related; boundary={boundary}",
        },
        content=body,
        timeout=120.0,
    )
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


def send_email(*, to: str, subject: str, body: str) -> dict:
    """Send one email through SendGrid."""
    missing = _missing_config(
        ("SENDGRID_API_KEY", settings.sendgrid_api_key),
        ("SENDGRID_FROM_EMAIL", settings.sendgrid_from_email),
    )
    if missing:
        return named_refusal(
            E_INTEGRATION, f"SendGrid configuration missing: {', '.join(missing)}",
            how_to_fix=_SENDGRID_HINT, missing=missing,
        )
    return integration_call(
        "sendgrid", "SendGridAPIClient", agent_type="email_outreach", install_hint=_SENDGRID_HINT,
        call=_sendgrid_send, to=to, subject=subject, body=body,
    )


def _sendgrid_send(*, to: str, subject: str, body: str) -> Any:  # pragma: no cover - provider
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail

    client = SendGridAPIClient(settings.sendgrid_api_key)
    message = Mail(from_email=settings.sendgrid_from_email, to_emails=to,
                   subject=subject, plain_text_content=body)
    response = client.send(message)
    headers = getattr(response, "headers", None) or {}
    # WHY (RD14): the send caps' producer (`outreach_service.record_sent_emails`) dedupes on
    # `email_logs.sendgrid_id`, and SendGrid's per-message id is the `X-Message-Id` response
    # header — UNIQUE per send. Returning only `status_code` made every send's dedupe key "202",
    # so consecutive real sends collapsed onto ONE row (measured: 3 distinct sends -> 1 row) and
    # the email cap counted 1 forever (recorded gap: "RateCaps send-cap producer — channel_sends
    # has no writer"). No `X-Message-Id` -> empty string, and the caller's status_code fallback
    # applies exactly as before (no behavior change there).
    message_id = ""
    if isinstance(headers, dict):
        message_id = str(headers.get("X-Message-Id") or "")
        if not message_id:
            message_id = next(
                (str(value) for key, value in headers.items() if str(key).lower() == "x-message-id"),
                "",
            )
    return {
        "status_code": int(getattr(response, "status_code", 0)),
        "message_id": message_id,
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
        call=_pygithub_open_pr, repo=repo, branch=branch, title=title, body=body, base=base,
    )


def _pygithub_open_pr(*, repo: str, branch: str, title: str, body: str, base: str) -> Any:  # pragma: no cover
    from github import Github

    client = Github(settings.github_token)
    repository = client.get_repo(repo)
    pr = repository.create_pull(title=title, body=body, head=branch, base=base)
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
    integration failure.  Adding a `ch_…`/`py_…` route is a deliberate follow-up (REQUESTS),
    never a best-effort guess here.

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
        call=_stripe_refund, payment_id=identifier, amount_cents=amount_cents,
    )


def _stripe_refund(*, payment_id: str, amount_cents: int | None) -> Any:  # pragma: no cover
    import stripe

    stripe.api_key = settings.stripe_secret_key
    params: dict[str, Any] = {"payment_intent": payment_id}
    if amount_cents is not None:
        params["amount"] = int(amount_cents)
    refund = stripe.Refund.create(**params)
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
# ---------------------------------------------------------------------------


def _day_window(days: int) -> tuple[date, date]:
    """The inclusive UTC day window `days` covers (today is the last day)."""
    end = date.today()
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


def _metrics_result(platform: str, days: int, buckets: list[dict[str, Any]]) -> dict[str, Any]:
    """Assemble per-campaign rows + totals from aggregated provider facts.

    `buckets` entries carry exact `spend_cents` / `value_cents` / `conversions` (already
    aggregated by the caller); every USD field is a cents-derived rounding OF the measured
    numbers, and `roas` is None when spend is 0 (an unmeasurable ratio stays unmeasured).
    """
    campaigns: list[dict[str, Any]] = []
    total_cents = 0
    total_value_cents = 0
    total_conversions = 0.0
    for bucket in buckets:
        spend_cents = int(bucket.get("spend_cents") or 0)
        value_cents = int(bucket.get("value_cents") or 0)
        conversions = float(bucket.get("conversions") or 0.0)
        total_cents += spend_cents
        total_value_cents += value_cents
        total_conversions += conversions
        campaigns.append({
            "campaign_id": str(bucket.get("campaign_id") or ""),
            "name": str(bucket.get("name") or ""),
            "status": str(bucket.get("status") or ""),
            "spend_cents": spend_cents,
            "spend_usd": round(spend_cents / 100.0, 2),
            "conversions": conversions,
            "conversions_value_usd": round(value_cents / 100.0, 2),
            "roas": round(value_cents / spend_cents, 4) if spend_cents else None,
        })
    start, end = _day_window(days)
    return {
        "platform": platform,
        "days": int(days),
        "window": {"since": start.isoformat(), "until": end.isoformat()},
        "fetched_at": now_utc().isoformat(),
        "campaigns": campaigns,
        "totals": {
            "spend_cents": total_cents,
            "spend_usd": round(total_cents / 100.0, 2),
            "conversions": total_conversions,
            "conversions_value_usd": round(total_value_cents / 100.0, 2),
            "roas": round(total_value_cents / total_cents, 4) if total_cents else None,
        },
    }


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
        metrics = row.metrics
        bucket = aggregated.setdefault(str(row.campaign.id), {
            "campaign_id": str(row.campaign.id),
            "name": str(row.campaign.name or ""),
            "status": getattr(row.campaign.status, "name", None) or str(row.campaign.status or ""),
            "micros": 0,
            "value_cents": 0,
            "conversions": 0.0,
        })
        bucket["micros"] += int(getattr(metrics, "cost_micros", 0) or 0)
        bucket["value_cents"] += _usd_to_cents(getattr(metrics, "conversions_value", 0.0)) or 0
        bucket["conversions"] += _as_float(getattr(metrics, "conversions", 0.0)) or 0.0
    buckets = [
        {**bucket, "spend_cents": int(bucket.pop("micros")) // 10_000}   # micros -> cents
        for bucket in aggregated.values()
    ]
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


def _meta_ads_read(days: int) -> Any:  # pragma: no cover - requires the provider
    """Campaign-level insights read through the Graph API (facebook-business)."""
    from facebook_business.adobjects.adaccount import AdAccount
    from facebook_business.api import FacebookAdsApi

    FacebookAdsApi.init(access_token=_setting("meta_ads_access_token"))
    account = AdAccount(f"act_{str(settings.meta_ads_account_id).strip()}")
    start, end = _day_window(days)
    rows = account.get_insights(
        fields=["campaign_id", "campaign_name", "spend", "actions", "action_values"],
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
        conversions = _as_float(_first_purchase_value(data.get("actions")))
        buckets.append({
            "campaign_id": str(data.get("campaign_id") or ""),
            "name": str(data.get("campaign_name") or ""),
            "status": "",
            "spend_cents": spend_cents or 0,
            "value_cents": value_cents or 0,
            "conversions": conversions or 0.0,
        })
    return _metrics_result("meta_ads", days, buckets)


def read_ad_metrics(*, platform: str = "google_ads", days: int = 7) -> dict[str, Any]:
    """Read the campaigns' OWN metrics from the ad provider — the ads run's input leg.

    H2-frozen interface (V15/4-02, branch "a" LOCKED): returns the standard integration envelope
    — `{"ok": True, "result": {platform, days, window, fetched_at, campaigns[], totals}}` with
    money in INTEGER CENTS (`spend_cents`, `conversions_value_usd`) and `roas` from measured
    values only — or a NAMED refusal (`E_INTEGRATION` naming the missing credentials;
    `E_VALIDATION` for an unknown platform).  The worker attach stores this envelope VERBATIM
    under `metadata["ad_metrics"]`; the agent consumes it via `ad_metrics_extra_context`.
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
    "open_pull_request",
    "post_tweet",
    "read_ad_metrics",
    "read_revenue_facts",
    "refund_payment",
    "send_email",
    "update_ad_budget",
]
