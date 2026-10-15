"""V14 / v2-sec — the security floor this wave ships, pinned RED-first.

Items (source: `wave-v13/u2-security/FINDINGS.md`, adjudicated by `wave-v14/V14-PLAN.md` §V2):

  * U2-01 — a non-ASCII `X-API-Key` (or a non-ASCII *configured* key) must be the named 401
    `E_AUTH`, never an unhandled `TypeError` -> 500. Pre-fix: `security._matches()` compared
    `str` with `secrets.compare_digest`, which raises for any non-ASCII operand.
  * U2-02 — `web_fetch` egress floor: http(s) only; the host must resolve to PUBLIC addresses
    only (loopback / RFC1918 / link-local+metadata / multicast / reserved / ULA denied);
    redirects are followed manually, NEVER cross the original host, and every hop re-passes
    the floor. Pre-fix: only scheme+netloc were checked and `follow_redirects=True` followed
    anything (`probes/probe3_ssrf_out.json`).
  * U2-03 — the YouTube media download is the same SSRF class, with a byte cap and no
    redirect following. Pre-fix: `integrations.py:410` `httpx.get(image_url)` with zero
    validation and an unbounded body.
  * U2-05 — the trigger route authenticates BEFORE path validation: ONE 401 `E_AUTH` shape for
    every unauthenticated caller (no valid/invalid-agent enumeration oracle), 422 only once a
    valid key is presented.
  * U2-09 — the site-package HTML never emits a non-http(s) `href` (e.g. `javascript:`),
    while the configured value stays visible as escaped text.

The loopback server below is the v13 probes' own reproduction shape: pre-fix, `web_fetch`
REALLY fetched `http://127.0.0.1:<port>/secret` and returned the body; post-fix the same call
must be a named refusal and the server must record ZERO hits (the bytes never crossed).
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.agents import integrations, tool_registry
from app.core import security
from app.core.errors import E_AUTH, PolsiaError
from app.models.company import CompanyConfig
from app.services.site_package_service import render_site_package

# ── a REAL loopback server (records hits; serves the internal secret + redirects) ────────────

class _Handler(BaseHTTPRequestHandler):
    hits: list[str] = []

    def log_message(self, *args):  # quiet the test log
        pass

    def _send(self, status: int, body: bytes, content_type: str = "text/plain; charset=utf-8",
              *, location: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 — http.server API
        type(self).hits.append(self.path)
        if self.path == "/secret":
            self._send(200, b"TOP-SECRET-INTERNAL-BODY")
        elif self.path == "/ok":
            self._send(200, b"hello-public")
        elif self.path == "/go-same":
            self._send(302, b"", location="/ok")
        elif self.path == "/go-cross":
            self._send(302, b"", location="http://198.51.100.7/ok")
        elif self.path == "/loop":
            self._send(302, b"", location="/loop")
        elif self.path == "/media.mp4":
            self._send(200, b"m" * 4096, content_type="video/mp4")
        else:
            self._send(404, b"nope")


@pytest.fixture()
def internal_server():
    """The v13 probes' loopback target (ephemeral port), with its hit list reset per test."""
    _Handler.hits = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _payload(text: str) -> dict:
    import json

    return json.loads(text)


def _fetch(server: str, path: str, **args) -> dict:
    return _payload(tool_registry.execute_tool(
        "web_fetch", {"url": f"{server}{path}", **args}, agent_type="competitor_research"))


def _stub_floor_for(monkeypatch, prefix: str) -> None:
    """Let URLs under `prefix` through the REAL floor; everything else hits the real floor.

    Needed only where a test must exercise the FETCH MACHINERY (redirect loop) which — by
    design — can no longer be reached through a loopback host. The floor itself is pinned
    against the same real server in the tests above/below this helper.
    """
    real = tool_registry.egress_policy_refusal

    def stubbed(url, **kwargs):
        if url.startswith(prefix):
            return None
        return real(url, **kwargs)

    monkeypatch.setattr(tool_registry, "egress_policy_refusal", stubbed)


# ── U2-01: non-ASCII X-API-Key is a named E_AUTH, never a TypeError ──────────────────────────

@pytest.mark.asyncio
async def test_u2_01_non_ascii_provided_key_names_e_auth_not_typeerror(monkeypatch):
    monkeypatch.setattr(security.settings, "api_key", "test-key")
    with pytest.raises(PolsiaError) as excinfo:
        await security.verify_api_key("test-key\u00ff")  # uvicorn decodes header bytes as latin-1
    assert excinfo.value.code == E_AUTH


@pytest.mark.asyncio
async def test_u2_01_non_ascii_configured_key_compares_by_bytes_and_still_auths(monkeypatch):
    monkeypatch.setattr(security.settings, "api_key", "cl\u00e9-\U0001f511")
    assert await security.verify_api_key("cl\u00e9-\U0001f511") == "cl\u00e9-\U0001f511"
    with pytest.raises(PolsiaError) as excinfo:
        await security.verify_api_key("cl\u00e9")
    assert excinfo.value.code == E_AUTH
    assert security.api_key_matches("cl\u00e9-\U0001f511") is True


@pytest.mark.asyncio
async def test_u2_01_inline_and_ws_matchers_share_the_guarded_path(monkeypatch):
    monkeypatch.setattr(security.settings, "api_key", "test-key")
    assert security.api_key_matches("test-key\u00ff") is False
    assert security.api_key_matches("\u65e5\u672c\u8a9e") is False
    with pytest.raises(PolsiaError) as excinfo:
        await security.check_api_key("\u00e9\u00e9\u00e9")
    assert excinfo.value.code == E_AUTH


@pytest.mark.asyncio
async def test_u2_01_non_ascii_header_over_http_is_401_e_auth(api_client):
    # A raw byte >= 0x80 in the header (what curl/browsers can send) must reach the SAME named
    # refusal as any wrong key; pre-fix this escaped every handler as a TypeError -> 500 class.
    resp = await api_client.get("/api/v1/config", headers={"X-API-Key": b"test-k\xc3\xa9y"})
    assert resp.status_code == 401, resp.text[:300]
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == E_AUTH


# ── U2-02: web_fetch egress floor (loopback/RFC1918/link-local denied; redirect policy) ──────

@pytest.mark.asyncio
async def test_u2_02_web_fetch_refuses_loopback_ip_without_any_connect(internal_server):
    payload = _fetch(internal_server, "/secret")
    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_POLICY_DENIED"
    assert "127.0.0.1" in payload["error"]["message"]
    assert _Handler.hits == [], "the floor must refuse BEFORE any byte crosses to the internal host"


@pytest.mark.asyncio
async def test_u2_02_web_fetch_refuses_localhost_by_name(internal_server):
    port = internal_server.rsplit(":", 1)[1]
    payload = _payload(tool_registry.execute_tool(
        "web_fetch", {"url": f"http://localhost:{port}/secret"}, agent_type="competitor_research"))
    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_POLICY_DENIED"
    assert "localhost" in payload["error"]["message"]
    assert _Handler.hits == []


@pytest.mark.asyncio
async def test_u2_02_web_fetch_refuses_userinfo_host_confusion(internal_server):
    port = internal_server.rsplit(":", 1)[1]
    payload = _payload(tool_registry.execute_tool(
        "web_fetch", {"url": f"http://user@{'127.0.0.1'}:{port}/secret"},
        agent_type="competitor_research"))
    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_POLICY_DENIED"
    assert _Handler.hits == []


@pytest.mark.asyncio
async def test_u2_02_web_fetch_refuses_ipv6_loopback(internal_server):
    port = internal_server.rsplit(":", 1)[1]
    payload = _payload(tool_registry.execute_tool(
        "web_fetch", {"url": f"http://[::1]:{port}/secret"}, agent_type="competitor_research"))
    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_POLICY_DENIED"


@pytest.mark.parametrize(("url", "needle"), [
    ("http://169.254.169.254/latest/meta-data/", "169.254.169.254"),  # cloud metadata
    ("http://10.0.0.1/", "10.0.0.1"),                                  # RFC1918
    ("http://192.168.1.1/", "192.168.1.1"),                            # RFC1918
    ("http://0.0.0.0/", "0.0.0.0"),                                    # unspecified
    ("http://[fd00::1]/", "[fd00::1]"),                                # IPv6 ULA
])
@pytest.mark.asyncio
async def test_u2_02_egress_floor_denies_the_private_and_metadata_matrix(url, needle, monkeypatch):
    # Pre-fix these were ATTEMPTED over the network (transport error/timeout, no denial);
    # the short timeout keeps even the pre-fix reproduction bounded.
    monkeypatch.setattr(tool_registry, "WEB_FETCH_TIMEOUT_S", 0.3)
    payload = _payload(tool_registry.execute_tool("web_fetch", {"url": url}, agent_type="competitor_research"))
    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_POLICY_DENIED"
    assert needle in payload["error"]["message"]


def test_u2_02_egress_floor_allows_a_public_address_literal():
    # 93.184.216.34 is example.com's well-known public address — a literal needs no DNS.
    assert tool_registry.egress_policy_refusal(
        "http://93.184.216.34/", agent_type="competitor_research") is None


@pytest.mark.asyncio
async def test_u2_02_cross_host_redirect_is_refused(internal_server, monkeypatch):
    _stub_floor_for(monkeypatch, f"{internal_server}/go-cross")
    payload = _fetch(internal_server, "/go-cross")
    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_POLICY_DENIED"
    assert "198.51.100.7" in payload["error"]["message"], "the refusal names the host it refused to leave to"
    assert _Handler.hits == ["/go-cross"], "the redirect target is never requested"


@pytest.mark.asyncio
async def test_u2_02_every_redirect_hop_re_passes_the_floor(internal_server, monkeypatch):
    # Only the FIRST hop is stubbed to pass; the same-host redirect target must re-pass the REAL
    # floor — proving the check runs per hop, not once per call.
    _stub_floor_for(monkeypatch, f"{internal_server}/go-same")
    payload = _fetch(internal_server, "/go-same")
    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_POLICY_DENIED"
    assert _Handler.hits == ["/go-same"], "hop 2 must be refused before it is requested"


@pytest.mark.asyncio
async def test_u2_02_same_host_redirects_are_followed_and_final_url_reported(internal_server, monkeypatch):
    # The manual redirect loop itself: a same-host 302 is followed and the FINAL url is reported
    # (this is the machinery the `follow_redirects=False` policy must preserve).
    _stub_floor_for(monkeypatch, internal_server)
    payload = _fetch(internal_server, "/go-same")
    assert payload["ok"] is True, payload
    assert payload["result"]["url"].endswith("/ok")
    assert _Handler.hits == ["/go-same", "/ok"]


@pytest.mark.asyncio
async def test_u2_02_redirect_loops_stop_at_the_cap(internal_server, monkeypatch):
    _stub_floor_for(monkeypatch, internal_server)
    payload = _fetch(internal_server, "/loop")
    max_redirects = tool_registry.WEB_FETCH_MAX_REDIRECTS
    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_POLICY_DENIED"
    assert "redirect" in payload["error"]["message"].lower()
    assert len(_Handler.hits) == max_redirects + 1, "bounded hops, then a named refusal"


# ── U2-03: YouTube media fetch — same floor, byte cap, no redirect following ─────────────────

def _youtube_token(monkeypatch) -> None:
    monkeypatch.setattr(
        integrations, "_setting",
        lambda name, default="": "test-token" if name == "youtube_access_token" else default,
    )


def _no_provider_call(monkeypatch) -> None:
    import httpx

    def _refuse(*args, **kwargs):  # a refusal test must never phone a provider
        raise AssertionError("provider call reached in a refusal test")

    monkeypatch.setattr(httpx, "post", _refuse)


@pytest.mark.asyncio
async def test_u2_03_youtube_media_url_is_refused_before_any_fetch(internal_server, monkeypatch):
    _youtube_token(monkeypatch)
    _no_provider_call(monkeypatch)
    payload = integrations._post_youtube("clip", image_url=f"{internal_server}/missing")
    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_POLICY_DENIED"
    assert _Handler.hits == [], "pre-fix this really fetched the internal URL (probe3 class)"


@pytest.mark.asyncio
async def test_u2_03_youtube_media_url_must_be_http_s(monkeypatch):
    _youtube_token(monkeypatch)
    _no_provider_call(monkeypatch)
    payload = integrations._post_youtube("clip", image_url="file:///etc/passwd")
    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_VALIDATION"


@pytest.mark.asyncio
async def test_u2_03_youtube_media_download_stops_at_the_byte_cap(internal_server, monkeypatch):
    monkeypatch.setattr(integrations, "YOUTUBE_MEDIA_MAX_BYTES", 1024)
    _no_provider_call(monkeypatch)  # the cap must trip BEFORE any provider call
    with pytest.raises(RuntimeError) as excinfo:
        integrations._youtube_insert_video("clip", f"{internal_server}/media.mp4")
    assert "cap" in str(excinfo.value).lower() or "1024" in str(excinfo.value)


# ── U2-05: trigger route auth BEFORE path validation (no enumeration oracle) ─────────────────

@pytest.mark.asyncio
async def test_u2_05_unauthenticated_trigger_callers_get_one_401_shape(api_client):
    unknown_no_key = await api_client.post("/api/v1/agents/nonexistent/trigger")
    known_no_key = await api_client.post("/api/v1/agents/social_media/trigger")
    unknown_wrong_key = await api_client.post(
        "/api/v1/agents/nonexistent/trigger", headers={"X-API-Key": "wrong"})
    for leg, resp in (("unknown+no-key", unknown_no_key), ("known+no-key", known_no_key),
                      ("unknown+wrong-key", unknown_wrong_key)):
        assert resp.status_code == 401, f"{leg}: {resp.status_code} {resp.text[:200]}"
        assert resp.json()["error"]["code"] == E_AUTH, leg
    assert unknown_no_key.json() == known_no_key.json(), (
        "an unauthenticated caller must not be able to tell a valid agent name from an invalid one")


@pytest.mark.asyncio
async def test_u2_05_unknown_agent_with_a_valid_key_is_still_422(api_client, auth_headers):
    resp = await api_client.post("/api/v1/agents/nonexistent/trigger", headers=auth_headers)
    assert resp.status_code == 422


# ── U2-09: site-package HTML never emits a non-http(s) href ──────────────────────────────────

def _company(**overrides) -> CompanyConfig:
    fields = {
        "name": "North Star SEO",
        "description": "Local SEO for small Minnesota businesses.",
        "notification_email": "owner@northstar.example",
    }
    fields.update(overrides)
    return CompanyConfig(**fields)


@pytest.mark.parametrize("website", [
    "javascript:alert(5)",
    "data:text/html,<script>alert(1)</script>",
    "//evil.example/track",
    "vbscript:msgbox(1)",
])
def test_u2_09_site_package_refuses_to_link_non_http_schemes(website):
    index = render_site_package(_company(website_url=website))["index.html"]
    assert "<a " not in index, f"{website!r} must not produce a link: {index}"
    assert "<script>" not in index
    assert website.split(":")[0] in index, "the configured value stays visible as escaped text"


def test_u2_09_site_package_keeps_linking_real_http_urls():
    index = render_site_package(_company(website_url="https://northstar.example"))["index.html"]
    assert '<a href="https://northstar.example">https://northstar.example</a>' in index
    index_http = render_site_package(_company(website_url="http://northstar.example"))["index.html"]
    assert '<a href="http://northstar.example">' in index_http
