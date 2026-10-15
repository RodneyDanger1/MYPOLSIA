"""2R-07 (V2 handoff) — the planner/competitor ingest-side http(s)-scheme floor — RED-first legs.

V14's U2-04 closed the RENDER side (`safeHref`, site-package `_website_line`); this suite pins the
INGEST side (V2 receipt §Open): `source_url` / `source_urls` values that are not absolute http(s)
URLs must never persist, never mark a claim `verified`, and never reach a response as a link.

The ONE predicate (`app.schemas.planner.is_http_url`) is used by:
  * `PlannerClaim.source_url` — the wire model REFUSES a non-http(s) value by name (E_VALIDATION);
  * `planner_service.verify_claims` / `_fetched_urls` — the LLM's draft claims are SANITIZED: a
    non-http citation is dropped and counted into `claims_unverified`, never shipped, and a
    non-http URL can never enter the fetched set a claim is verified against;
  * `agent_outputs.Competitor.source_urls` / `.sources` — filtered to http(s) entries (never a
    fabricated link, never a silent pass-through into the planner's fetched-URL set).
"""
from __future__ import annotations

import inspect

import pytest

from app.core.errors import E_VALIDATION, PolsiaError
from app.schemas.planner import PlannerClaim, is_http_url
from app.services.planner_service import _fetched_urls, verify_claims

BAD_URLS = (
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "",
    "/relative/path",
    "ftp://example.com/file",
    "mailto:someone@example.com",
    "https://",
)


def test_predicate_accepts_only_absolute_http_s() -> None:
    assert is_http_url("https://example.com/a?b=1#c")
    assert is_http_url("http://example.com")
    assert is_http_url("HTTPS://EXAMPLE.COM/X")          # scheme comparison is case-insensitive
    assert not is_http_url("javascript:alert(1)")
    assert not is_http_url("data:text/html,x")
    assert not is_http_url("vbscript:x")
    assert not is_http_url("")
    assert not is_http_url(None)
    assert not is_http_url(12)
    assert not is_http_url("https://")                   # no host: not a usable URL


@pytest.mark.parametrize("bad", BAD_URLS)
def test_planner_claim_refuses_non_http_source_url_by_name(bad: str) -> None:
    """The wire model refuses a non-http(s) citation BY NAME (E_VALIDATION) — never ships it."""
    with pytest.raises(PolsiaError) as refusal:
        PlannerClaim(
            claim="the market is growing",
            source_url=bad,
            fetched_at="2026-10-15T00:00:00+00:00",
            quote="q",
            verified=True,
        )
    assert refusal.value.code == E_VALIDATION
    assert "source_url" in refusal.value.message
    assert refusal.value.how_to_fix


def test_planner_claim_accepts_a_real_https_citation() -> None:
    claim = PlannerClaim(
        claim="priced comparables exist",
        source_url="https://example.com/report?page=2",
        fetched_at="2026-10-15T00:00:00+00:00",
        quote="q",
        verified=True,
    )
    assert claim.source_url == "https://example.com/report?page=2"


def test_verify_claims_drops_non_http_even_when_the_url_was_fetched() -> None:
    """Sanitize leg: a `javascript:` citation is dropped + counted, never verified — even when the
    same bad URL sits in the fetched set (a hostile research output must not launder it)."""
    kept, dropped = verify_claims(
        [
            {"claim": "legit", "source_url": "https://example.com/x", "quote": "q1"},
            {"claim": "sneaky", "source_url": "javascript:alert(1)", "quote": "q2"},
            {"claim": "uncited", "source_url": "", "quote": "q3"},
        ],
        fetched_urls={"https://example.com/x", "javascript:alert(1)"},
        fetched_at="2026-10-15T00:00:00+00:00",
    )
    assert [item["source_url"] for item in kept] == ["https://example.com/x"]
    assert [item["verified"] for item in kept] == [True]
    assert dropped == 2, "both the non-http citation and the uncited claim are dropped + counted"


def test_fetched_urls_exclude_non_http_recordings() -> None:
    """A non-http URL can never enter the fetched set claims are verified against."""
    urls = _fetched_urls(
        {
            "competitors": [
                {"source_urls": ["https://a.example", "javascript:x"]},
                {"sources": ["data:text/html,x", "http://b.example"]},
            ]
        }
    )
    assert urls == {"https://a.example", "http://b.example"}


def test_competitor_source_urls_filter_non_http_entries() -> None:
    from app.schemas.agent_outputs import Competitor

    competitor = Competitor(
        name="Acme",
        source_urls=["https://a.example", "javascript:x"],
        sources=["vbscript:y"],
    )
    assert competitor.source_urls == ["https://a.example"]
    assert competitor.sources == ["https://a.example"], "the mirror keeps both consumers filled"


def test_competitor_with_only_non_http_urls_loses_them_all() -> None:
    from app.schemas.agent_outputs import Competitor

    competitor = Competitor(name="Acme", source_urls=["javascript:x"], sources=["data:text/html,x"])
    assert competitor.source_urls == []
    assert competitor.sources == []


def test_the_one_predicate_is_used_on_both_schema_surfaces() -> None:
    import app.schemas.agent_outputs as agent_outputs
    import app.schemas.planner as planner
    import app.services.planner_service as planner_service

    assert "is_http_url" in inspect.getsource(planner)
    assert "is_http_url" in inspect.getsource(agent_outputs)
    assert "is_http_url" in inspect.getsource(planner_service)
