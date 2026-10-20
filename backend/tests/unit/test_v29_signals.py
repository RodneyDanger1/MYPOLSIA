"""V29 / L5 — Signals: fixed feed allowlist, RSS/Atom parser, 30-min per-feed cache, route.

WHY THIS FILE EXISTS
--------------------
The dashboard's SIGNALS widget must be able to say "here are today's headlines" without a
scraping stack, an API key, a quota, or a user-supplied URL.  The contract pinned here:

* the feed allowlist is a module CONSTANT (`FEEDS`) and `read()` takes only `limit` — there is
  no code path that accepts a caller's URL, so the surface is SSRF-safe by construction;
* RSS (`channel/item`) and Atom (`{http://www.w3.org/2005/Atom}entry`) both parse, matched by
  LOCAL name so an unexpected namespace prefix cannot silently yield zero items;
* a title is text, never markup: tags are stripped, entities are unescaped, the length is
  capped, and a link that is not absolute http(s) is dropped rather than placed in an anchor;
* a bad date is `None` — never `now`, which would invent freshness;
* at most 20 items per feed, deduped by URL across feeds, newest first;
* each feed caches for 30 minutes and a cache hit keeps the ORIGINAL `fetched_at`;
* one broken feed is a NAMED per-source reason (`E_UPSTREAM: …`) and never fails its siblings,
  and failures are never cached;
* every answer carries `untrusted: true` and the note that says the text is third-party.

The network is a labelled double: `_CLIENT` is the module's documented seam, so a
`httpx.MockTransport` answers per host and the request COUNT is the cache evidence.  No test
here touches the network.
"""
from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import E_VALIDATION, PolsiaError, http_status_for, to_http_detail
from app.services import signals_service as svc

# ── fixtures: two real feed shapes, small enough to read ─────────────────────────────────────

RSS_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Hacker News</title>
  <item>
    <title>Show HN: A &amp; B</title>
    <link>https://example.com/a?x=1&amp;y=2</link>
    <pubDate>Mon, 19 Oct 2026 10:00:00 GMT</pubDate>
  </item>
  <item>
    <title><![CDATA[<b>Bold</b> headline]]></title>
    <link>https://example.com/b</link>
    <pubDate>Mon, 19 Oct 2026 12:00:00 GMT</pubDate>
  </item>
  <item>
    <title>Half a date</title>
    <link>https://example.com/c</link>
    <pubDate>not a date at all</pubDate>
  </item>
  <item>
    <title>Javascript link</title>
    <link>javascript:alert(1)</link>
  </item>
</channel></rss>"""

ATOM_FEED = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>TechCrunch</title>
  <entry>
    <title>Atom one</title>
    <link rel="alternate" href="https://techcrunch.com/one/"/>
    <published>2026-10-19T11:30:00Z</published>
  </entry>
  <entry>
    <title>Atom two</title>
    <link href="https://techcrunch.com/two/"/>
    <updated>2026-10-18T09:00:00+02:00</updated>
  </entry>
</feed>"""


def rss_document(items: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"{items}</channel></rss>"
    ).encode()


def rss_item(title: str, url: str, when: datetime | None = None) -> str:
    stamp = f"<pubDate>{format_datetime(when, usegmt=True)}</pubDate>" if when else ""
    return f"<item><title>{title}</title><link>{url}</link>{stamp}</item>"


HN = "news.ycombinator.com"
TC = "techcrunch.com"


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Every test starts from an empty cache (module state would otherwise leak between cases)."""
    svc._CACHE.clear()
    yield
    svc._CACHE.clear()


@pytest.fixture
def install_transport(monkeypatch):
    """Install the labelled network double; returns the list of request URLs it answered."""

    def install(routes: dict[str, tuple[int, bytes] | Exception]) -> list[str]:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            answer = routes.get(request.url.host or "")
            if answer is None:
                return httpx.Response(404, content=b"no route for this host")
            if isinstance(answer, Exception):
                raise answer
            status, body = answer
            return httpx.Response(status, content=body)

        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(svc, "_CLIENT", lambda **kwargs: httpx.Client(transport=transport, **kwargs))
        return calls

    return install


# ── the allowlist ────────────────────────────────────────────────────────────────────────────


def test_feeds_are_the_fixed_keyless_allowlist():
    assert [(feed.id, feed.url) for feed in svc.FEEDS] == [
        ("hn", "https://news.ycombinator.com/rss"),
        ("tc", "https://techcrunch.com/feed/"),
    ]
    assert [feed.label for feed in svc.FEEDS] == ["Hacker News", "TechCrunch"]


def test_read_accepts_no_feed_url_so_a_caller_cannot_point_it_elsewhere():
    assert set(inspect.signature(svc.read).parameters) == {"limit"}


# ── parsing ──────────────────────────────────────────────────────────────────────────────────


def test_rss_items_carry_title_link_source_and_published(install_transport):
    install_transport({HN: (200, RSS_FEED), TC: (200, ATOM_FEED)})

    result = svc.read(limit=10)
    by_url = {item["url"]: item for item in result["items"]}

    first = by_url["https://example.com/a?x=1&y=2"]
    assert first["title"] == "Show HN: A & B"
    assert first["source"] == "Hacker News"
    assert first["id"] == "hn"
    assert first["published_at"] == "2026-10-19T10:00:00Z"


def test_atom_entries_parse_through_their_namespace(install_transport):
    install_transport({HN: (200, RSS_FEED), TC: (200, ATOM_FEED)})

    result = svc.read(limit=10)
    atom = {item["url"]: item for item in result["items"] if item["id"] == "tc"}

    assert set(atom) == {"https://techcrunch.com/one/", "https://techcrunch.com/two/"}
    assert atom["https://techcrunch.com/one/"]["title"] == "Atom one"
    assert atom["https://techcrunch.com/one/"]["published_at"] == "2026-10-19T11:30:00Z"
    # `updated` is the fallback, normalized to UTC (09:00+02:00 == 07:00Z).
    assert atom["https://techcrunch.com/two/"]["published_at"] == "2026-10-18T07:00:00Z"


def test_a_title_is_text_never_markup(install_transport):
    install_transport({HN: (200, RSS_FEED), TC: (200, ATOM_FEED)})

    by_url = {item["url"]: item for item in svc.read(limit=10)["items"]}

    assert by_url["https://example.com/b"]["title"] == "Bold headline"
    assert "<" not in by_url["https://example.com/b"]["title"]


def test_a_long_title_is_capped(install_transport):
    long = "x" * 500
    feed = rss_document(rss_item(long, "https://example.com/long"))
    install_transport({HN: (200, feed), TC: (200, ATOM_FEED)})

    item = next(i for i in svc.read(limit=10)["items"] if i["url"] == "https://example.com/long")
    assert len(item["title"]) == svc.MAX_TITLE_CHARS


def test_a_bad_date_is_none_never_now(install_transport):
    install_transport({HN: (200, RSS_FEED), TC: (200, ATOM_FEED)})

    by_url = {item["url"]: item for item in svc.read(limit=10)["items"]}
    assert by_url["https://example.com/c"]["published_at"] is None


def test_a_non_http_link_is_dropped_not_rendered(install_transport):
    install_transport({HN: (200, RSS_FEED), TC: (200, ATOM_FEED)})

    urls = {item["url"] for item in svc.read(limit=10)["items"]}
    assert "javascript:alert(1)" not in urls


# ── failure isolation ────────────────────────────────────────────────────────────────────────


def test_malformed_xml_is_a_named_reason_and_the_sibling_still_serves(install_transport):
    install_transport({HN: (200, b"<rss><channel><item><title>cut off"), TC: (200, ATOM_FEED)})

    result = svc.read(limit=10)
    sources = {source["id"]: source for source in result["sources"]}

    assert sources["hn"]["ok"] is False
    assert sources["hn"]["reason"].startswith("E_UPSTREAM")
    assert sources["hn"]["count"] == 0
    assert sources["tc"]["ok"] is True
    assert {item["id"] for item in result["items"]} == {"tc"}


def test_an_http_error_is_a_named_reason(install_transport):
    install_transport({HN: (500, b"boom"), TC: (200, ATOM_FEED)})

    sources = {s["id"]: s for s in svc.read(limit=10)["sources"]}
    assert sources["hn"]["ok"] is False
    assert "HTTP 500" in sources["hn"]["reason"]
    assert sources["hn"]["reason"].startswith("E_UPSTREAM")


def test_a_timeout_is_a_named_reason(install_transport):
    install_transport({HN: httpx.ReadTimeout("slow feed"), TC: (200, ATOM_FEED)})

    sources = {s["id"]: s for s in svc.read(limit=10)["sources"]}
    assert sources["hn"]["ok"] is False
    assert "timed out" in sources["hn"]["reason"]


def test_both_feeds_failing_names_each_one_and_invents_nothing(install_transport):
    install_transport({HN: (503, b""), TC: (503, b"")})

    result = svc.read(limit=10)
    assert result["items"] == []
    assert [source["ok"] for source in result["sources"]] == [False, False]
    assert all(source["reason"] for source in result["sources"])
    assert result["untrusted"] is True


def test_a_failed_feed_is_never_cached(install_transport):
    calls = install_transport({HN: (500, b"boom"), TC: (200, ATOM_FEED)})

    svc.read(limit=10)
    svc.read(limit=10)

    assert len([url for url in calls if HN in url]) == 2


# ── cache ───────────────────────────────────────────────────────────────────────────────────


def test_a_cache_hit_makes_one_request_and_keeps_the_original_fetched_at(install_transport):
    calls = install_transport({HN: (200, RSS_FEED), TC: (200, ATOM_FEED)})

    first = svc.read(limit=10)
    second = svc.read(limit=10)

    assert len([url for url in calls if HN in url]) == 1
    hn_first = next(s for s in first["sources"] if s["id"] == "hn")
    hn_second = next(s for s in second["sources"] if s["id"] == "hn")
    assert hn_first["cached"] is False
    assert hn_second["cached"] is True
    assert hn_second["fetched_at"] == hn_first["fetched_at"]


def test_the_cache_expires_after_thirty_minutes(install_transport):
    calls = install_transport({HN: (200, RSS_FEED), TC: (200, ATOM_FEED)})

    svc.read(limit=10)
    stamp, payload = svc._CACHE["hn"]
    svc._CACHE["hn"] = (stamp - svc.CACHE_TTL_S - 1, payload)
    svc.read(limit=10)

    assert len([url for url in calls if HN in url]) == 2


# ── assembly ────────────────────────────────────────────────────────────────────────────────


def test_one_url_in_both_feeds_appears_once(install_transport):
    shared = "https://example.com/shared"
    install_transport(
        {
            HN: (200, rss_document(rss_item("Shared", shared))),
            TC: (200, rss_document(rss_item("Shared again", shared))),
        }
    )

    titles = [item["title"] for item in svc.read(limit=10)["items"] if item["url"] == shared]
    assert titles == ["Shared"]


def test_at_most_twenty_per_feed_and_newest_first(install_transport):
    base = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
    items = "".join(
        rss_item(f"story {index}", f"https://example.com/{index}", base - timedelta(minutes=index))
        for index in range(25)
    )
    install_transport({HN: (200, rss_document(items)), TC: (200, ATOM_FEED)})

    result = svc.read(limit=30)
    hn = [item for item in result["items"] if item["id"] == "hn"]

    assert len(hn) == svc.PER_FEED_LIMIT == 20
    # the 20 NEWEST survive the cap (0..19), not the first 20 in document order by accident
    assert {item["url"] for item in hn} == {f"https://example.com/{i}" for i in range(20)}
    published = [item["published_at"] for item in result["items"] if item["published_at"]]
    assert published == sorted(published, reverse=True)


def test_every_read_is_marked_untrusted_and_says_what_it_is(install_transport):
    install_transport({HN: (200, RSS_FEED), TC: (200, ATOM_FEED)})

    result = svc.read(limit=10)
    assert result["untrusted"] is True
    assert "third-party" in result["note"]
    assert "headlines" in result["note"].lower()
    assert result["fetched_at"].endswith("Z")


def test_limit_must_be_an_integer_in_range(install_transport):
    install_transport({HN: (200, RSS_FEED), TC: (200, ATOM_FEED)})

    for bad in (0, 31, -1, True, "5", 2.5, None):
        with pytest.raises(PolsiaError) as caught:
            svc.read(limit=bad)
        assert caught.value.code == E_VALIDATION


# ── route ────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
async def signals_client(install_transport, monkeypatch):
    """The REAL router on a local app: correct prefix, auth dependency and refusal envelope."""
    from app.api.v1 import signals
    from app.config import settings

    monkeypatch.setattr(settings, "api_key", "test-key", raising=False)
    install_transport({HN: (200, RSS_FEED), TC: (200, ATOM_FEED)})

    app = FastAPI()
    app.include_router(signals.router, prefix="/api/v1")

    @app.exception_handler(PolsiaError)
    async def _polsia_error(_request, exc: PolsiaError) -> JSONResponse:
        return JSONResponse(
            status_code=http_status_for(exc.code),
            content=to_http_detail(exc),
            headers={"X-Polsia-Error-Code": exc.code},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _coded_http(_request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and isinstance(detail.get("code"), str) and detail["code"]:
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "ok": False,
                    "error": {
                        "code": detail["code"],
                        "message": detail.get("message") or "",
                        "how_to_fix": detail.get("how_to_fix") or "",
                        "context": detail.get("context") or {},
                    },
                },
                headers={"X-Polsia-Error-Code": detail["code"]},
            )
        return JSONResponse(status_code=exc.status_code, content={"detail": detail})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_route_serves_the_contract_and_honours_limit(signals_client):
    response = await signals_client.get(
        "/api/v1/signals?limit=1", headers={"X-API-Key": "test-key"}
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["untrusted"] is True
    assert {source["id"] for source in body["sources"]} == {"hn", "tc"}
    assert set(body) == {"items", "sources", "fetched_at", "note", "untrusted"}


async def test_route_bounds_the_limit(signals_client):
    too_many = await signals_client.get(
        "/api/v1/signals?limit=99", headers={"X-API-Key": "test-key"}
    )
    zero = await signals_client.get("/api/v1/signals?limit=0", headers={"X-API-Key": "test-key"})

    assert too_many.status_code == 422
    assert zero.status_code == 422


async def test_route_requires_the_api_key(signals_client):
    response = await signals_client.get("/api/v1/signals?limit=2")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "E_AUTH"
