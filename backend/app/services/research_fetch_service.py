"""DeepSeek Fetch API adapter (V29/L3) — sync because tool runners are sync.

One page in, Markdown out: `POST https://api.deepseek.com/v1/tools/fetch` with `{"url": …}`
answers `{"title", "content"}` (docs: https://api-docs.deepseek.com/guides/search, verified
2026-10-20).  Fetch is billed $0.75 per 1,000 **successful** requests, so the cache stores the
FULL body for an hour and `max_chars` is applied per call — a later caller asking for more is
never served a previously truncated copy.  Failures are never cached, every provider outcome is
a named `PolsiaError`, and page text is marked `untrusted` for the caller.
"""
from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.errors import (
    E_AUTH,
    E_CONFIG,
    E_NOT_FOUND,
    E_RATE_LIMITED,
    E_UPSTREAM,
    E_VALIDATION,
    PolsiaError,
)
from app.core.timeutil import now_utc
from app.services import secret_store

FETCH_URL = "https://api.deepseek.com/v1/tools/fetch"
TIMEOUT_S = 30.0
MAX_CONTENT_CHARS = 40_000
MAX_CACHE_CHARS = 200_000
CACHE_TTL_S = 3600.0
CACHE_MAX_ENTRIES = 128
E_BALANCE = "E_BALANCE"  # local constant, the V24/V27 E_BALANCE precedent

#: The seam tests monkeypatch (`_CLIENT = lambda **kw: httpx.Client(transport=..., **kw)`).
_CLIENT = httpx.Client
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _key() -> str:
    key = secret_store.get_deepseek_api_key()
    if not key:
        raise PolsiaError(
            E_CONFIG,
            "research_fetch: no DeepSeek API key is configured",
            how_to_fix="save a DeepSeek key in Settings → AI model (DeepSeek)",
        )
    return key


def _validate_url(url: Any) -> str:
    if not isinstance(url, str) or not url.strip():
        raise PolsiaError(E_VALIDATION, "research_fetch requires a non-empty 'url' string",
                          how_to_fix='pass {"url": "https://…"}')
    text = url.strip()
    if len(text) > 8192:
        raise PolsiaError(E_VALIDATION, "research_fetch: url is longer than 8192 characters",
                          how_to_fix="use a shorter URL")
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise PolsiaError(E_VALIDATION, "research_fetch: url must be an absolute http(s) URL",
                          how_to_fix="pass a URL beginning with http:// or https://")
    if parsed.username or parsed.password:
        raise PolsiaError(E_VALIDATION, "research_fetch: credentials in a URL are refused",
                          how_to_fix="remove the user:password@ part")
    return text


def fetch(url: str, *, max_chars: int = MAX_CONTENT_CHARS) -> dict[str, Any]:
    """Fetch one page as Markdown. Cached for 1 h; failures are never cached."""
    target = _validate_url(url)
    # The declared contract (tool spec + this module's docstring) is an INTEGER.  `int()` alone is
    # a silent coercion — True -> 1, "5000" -> 5000, 2.5 -> 2 and then the 1,000 floor — and
    # `float("inf")` raises OverflowError straight out of this function, where the tool runner
    # (which catches only PolsiaError) would surface it as an internal error.  Refuse the caller's
    # value BY NAME before it can become a cap; a refused argument makes no provider call.
    if isinstance(max_chars, bool) or not isinstance(max_chars, int):
        raise PolsiaError(
            E_VALIDATION,
            f"research_fetch: max_chars must be an integer (got {type(max_chars).__name__})",
            how_to_fix=f"omit max_chars for {MAX_CONTENT_CHARS}, or pass an integer",
        )
    cap = max(1000, min(max_chars, MAX_CONTENT_CHARS))
    hit = _CACHE.get(target)
    if hit is not None and time.monotonic() - hit[0] < CACHE_TTL_S:
        # Cached content is the FULL body; the per-call cap is applied on the way out, so a later
        # call asking for more does not receive a previously truncated copy.  No provider call
        # happened here, so the call is not billed and its cost is zero.
        full = hit[1]["content"]
        return {**hit[1], "content": full[:cap], "content_chars": min(len(full), cap),
                "truncated": len(full) > cap, "cached": True, "billed": False,
                "estimated_usd": 0.0}

    key = _key()
    try:
        with _CLIENT(timeout=TIMEOUT_S) as client:
            response = client.post(
                FETCH_URL,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"url": target},
            )
    except httpx.TimeoutException as exc:
        raise PolsiaError(E_UPSTREAM, f"research_fetch: timed out after {TIMEOUT_S:g}s",
                          how_to_fix="retry once; if it persists the provider is slow") from exc
    except httpx.HTTPError as exc:
        raise PolsiaError(E_UPSTREAM, f"research_fetch: transport failure: {type(exc).__name__}",
                          how_to_fix="check egress and retry") from exc

    if response.status_code == 401:
        raise PolsiaError(E_AUTH, "research_fetch: the DeepSeek key was refused",
                          how_to_fix="re-save the key in Settings → AI model (DeepSeek)")
    if response.status_code == 402:
        raise PolsiaError(E_BALANCE, "research_fetch: the DeepSeek account has insufficient balance",
                          how_to_fix="top up the DeepSeek account, then retry")
    if response.status_code == 403:
        raise PolsiaError(E_UPSTREAM, "research_fetch: the fetch was rejected by the provider policy",
                          how_to_fix="use a different public page")
    if response.status_code == 404:
        raise PolsiaError(E_NOT_FOUND, "research_fetch: no usable page content was returned",
                          how_to_fix="check the URL, or use web_search to find a working page")
    if response.status_code == 429:
        raise PolsiaError(E_RATE_LIMITED, "research_fetch: the provider QPS limit was exceeded",
                          how_to_fix="wait and retry")
    if response.status_code >= 500 or response.status_code == 408:
        raise PolsiaError(E_UPSTREAM, f"research_fetch: provider answered HTTP {response.status_code}",
                          how_to_fix="retry after a short wait")
    if response.status_code != 200:
        raise PolsiaError(E_UPSTREAM, f"research_fetch: unexpected HTTP {response.status_code}",
                          how_to_fix="retry; if it persists check the provider status")

    try:
        payload = response.json()
    except ValueError as exc:
        raise PolsiaError(E_UPSTREAM, "research_fetch: provider returned non-JSON",
                          how_to_fix="retry") from exc
    if not isinstance(payload, dict):
        raise PolsiaError(E_UPSTREAM, "research_fetch: provider returned a non-object JSON body",
                          how_to_fix="retry; if it persists check the provider status")
    title_value = payload.get("title")
    title = title_value if isinstance(title_value, str) else ""
    content_value = payload.get("content")
    content = content_value if isinstance(content_value, str) else ""
    if not content.strip():
        raise PolsiaError(E_NOT_FOUND, "research_fetch: the page had no extractable Markdown",
                          how_to_fix="try another URL")
    stored_content = content[:MAX_CACHE_CHARS]
    truncated = len(content) > cap
    result = {
        "url": target,
        "title": title,
        "content": stored_content[:cap],
        "content_chars": min(len(content), cap),
        "truncated": truncated,
        "cached": False,
        "billed": True,
        "estimated_usd": 0.00075,
        "fetched_at": now_utc().isoformat(),
        "untrusted": True,
    }
    _CACHE[target] = (time.monotonic(), {**result, "content": stored_content})
    if len(_CACHE) > CACHE_MAX_ENTRIES:
        _CACHE.pop(next(iter(_CACHE)))
    return result
