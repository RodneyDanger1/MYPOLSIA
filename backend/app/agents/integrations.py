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

from typing import Any
from urllib.parse import urlsplit

from app.agents.side_effects import integration_call, named_refusal
from app.agents.tool_registry import egress_policy_refusal
from app.config import settings
from app.core.errors import E_INTEGRATION, E_VALIDATION

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


def _post_twitter(text: str, *, image_url: str = "") -> dict:
    """Post one status update to X/Twitter. Sandbox never reaches here (the firewall refuses first)."""
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
        call=_tweepy_create_tweet, text=text,
    )


def _tweepy_create_tweet(text: str) -> Any:  # pragma: no cover - requires the provider
    import tweepy

    client = tweepy.Client(
        consumer_key=settings.twitter_api_key,
        consumer_secret=settings.twitter_api_secret,
        access_token=settings.twitter_access_token,
        access_token_secret=settings.twitter_access_secret,
    )
    response = client.create_tweet(text=text)
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


def post_tweet(text: str, *, platform: str = "twitter", image_url: str = "") -> dict:
    """Post one status update on `platform` — the ONE entry point the social agent calls.

    Each adapter owns its full contract (lazy import, credential refusal, real provider call) and
    every one of them refuses by NAME when it cannot really post. `image_url` is optional at this
    boundary: an adapter that cannot post media refuses by name when it is set (never a silent
    drop), and instagram REQUIRES it (its API has no text-only publish).
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


__all__ = [
    "open_pull_request",
    "post_tweet",
    "refund_payment",
    "send_email",
    "update_ad_budget",
]
