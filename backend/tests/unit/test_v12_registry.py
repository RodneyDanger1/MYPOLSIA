"""V12/W-B — tool_registry: the REAL tool layer (web_fetch / macro_snapshot / pricing_catalog).

No mocks on the real path: `web_fetch` is exercised against a REAL loopback HTTP server
(stdlib `http.server`, ephemeral port) — the bytes cross a socket.  macro/pricing read their
REAL services (`economics_service._CACHE` is seeded exactly like the service serves it;
`pricing_service` reads the shipped 50-row catalog).  Refusals are NAMED: bad scheme, timeout,
non-2xx, oversize, unknown tool, denied agent, kill switch — never a throw.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import UTC, date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.agents import tool_registry
from app.core.timeutil import today_in

CATALOG_AS_OF = "2026-10-14"  # the frozen snapshot date (pricing_service module docstring)

_PAGE = (
    "<html><head><title>W-B probe page</title><style>h1{color:red}</style></head>"
    "<body><h1>Hello</h1><script>var x = 41 + 1;</script>"
    "<p>unique-marker-12345</p></body></html>"
)


# ── a REAL loopback server ───────────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet the test log
        pass

    def _send(self, status: int, body: bytes, content_type: str, *, location: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 — http.server API
        if self.path == "/ok":
            self._send(200, _PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/redirect":
            self._send(302, b"", "text/plain", location="/ok")
        elif self.path == "/missing":
            self._send(404, b"not found", "text/plain; charset=utf-8")
        elif self.path == "/big":
            self._send(200, b"x" * (300 * 1024), "text/plain; charset=utf-8")
        elif self.path == "/json":
            self._send(200, json.dumps({"answer": 42}).encode(), "application/json")
        elif self.path == "/slow":
            time.sleep(1.5)
            self._send(200, b"late", "text/plain; charset=utf-8")
        else:
            self._send(404, b"nope", "text/plain; charset=utf-8")


@pytest.fixture()
def live_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _payload(text: str) -> dict:
    return json.loads(text)


def _fetch(server: str, path: str, *, agent_type: str = "competitor_research", **args) -> dict:
    return _payload(tool_registry.execute_tool(
        "web_fetch", {"url": f"{server}{path}", **args}, agent_type=agent_type))


# ── the frozen interface ─────────────────────────────────────────────────────────────────────

def test_specs_are_openai_function_schemas_with_the_frozen_names():
    names = [spec["function"]["name"] for spec in tool_registry.TOOL_SPECS]
    assert set(names) == {"web_fetch", "macro_snapshot", "pricing_catalog"}
    for spec in tool_registry.TOOL_SPECS:
        assert spec["type"] == "function"
        function = spec["function"]
        assert isinstance(function["description"], str) and function["description"]
        params = function["parameters"]
        assert params["type"] == "object" and isinstance(params["properties"], dict)
    url_prop = tool_registry.TOOL_SPECS[names.index("web_fetch")]["function"]["parameters"]
    assert "url" in url_prop["properties"] and "url" in url_prop["required"]


# ── web_fetch: real bytes over a real socket ─────────────────────────────────────────────────

def test_web_fetch_fetches_a_real_page_over_the_socket(live_server):
    payload = _fetch(live_server, "/ok")

    assert payload["ok"] is True, payload
    result = payload["result"]
    assert result["status"] == 200
    assert result["url"].endswith("/ok")
    assert "text/html" in result["content_type"]
    assert "unique-marker-12345" in result["text"], "the page's text must arrive"
    assert "<h1>" not in result["text"], "HTML tags are stripped from the returned text"
    assert "var x" not in result["text"], "script content is not page text"
    assert result["truncated"] is False


def test_web_fetch_follows_redirects_and_reports_the_final_url(live_server):
    payload = _fetch(live_server, "/redirect")

    assert payload["ok"] is True
    assert payload["result"]["status"] == 200
    assert payload["result"]["url"].endswith("/ok"), "the FINAL url is reported, not the asked one"


def test_a_json_page_is_returned_as_text_verbatim(live_server):
    payload = _fetch(live_server, "/json")

    assert payload["ok"] is True
    assert payload["result"]["text"] == json.dumps({"answer": 42})


def test_a_non_2xx_is_a_named_refusal_with_the_status(live_server):
    payload = _fetch(live_server, "/missing")

    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_INTEGRATION"
    assert "404" in payload["error"]["message"]


def test_the_oversize_read_stops_at_the_byte_cap(live_server):
    cap = tool_registry.WEB_FETCH_MAX_BYTES
    payload = _fetch(live_server, "/big", max_chars=500)

    assert payload["ok"] is True, payload
    result = payload["result"]
    assert result["bytes_read"] == cap, "the read stops exactly at the cap"
    assert result["size_cap_hit"] is True
    assert len(result["text"]) <= 500, "the excerpt honours max_chars"
    assert result["truncated"] is True


def test_a_timeout_is_a_named_refusal(live_server, monkeypatch):
    monkeypatch.setattr(tool_registry, "WEB_FETCH_TIMEOUT_S", 0.4)
    payload = _fetch(live_server, "/slow")

    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_INTEGRATION"
    assert "timed out" in payload["error"]["message"]


@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "not a url"])
def test_non_http_schemes_are_refused_by_name(url):
    payload = _payload(tool_registry.execute_tool(
        "web_fetch", {"url": url}, agent_type="competitor_research"))

    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_VALIDATION"
    assert "http" in payload["error"]["message"]


@pytest.mark.parametrize("args", [{}, {"url": ""}, {"url": 42}])
def test_a_missing_or_non_string_url_refuses(args):
    payload = _payload(tool_registry.execute_tool(
        "web_fetch", args, agent_type="competitor_research"))

    assert payload["ok"] is False and payload["error"]["code"] == "E_VALIDATION"


@pytest.mark.parametrize("bad", ["abc", 0, -5])
def test_a_malformed_max_chars_refuses(live_server, bad):
    payload = _fetch(live_server, "/ok", max_chars=bad)

    assert payload["ok"] is False and payload["error"]["code"] == "E_VALIDATION"


# ── refusals: unknown tool / denied agent / kill switch ──────────────────────────────────────

def test_an_unknown_tool_is_refused_by_name():
    payload = _payload(tool_registry.execute_tool("shell_exec", {}, agent_type="code_generation"))

    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_VALIDATION"
    assert "shell_exec" in payload["error"]["message"]


def test_a_denied_agent_is_refused_by_name(live_server):
    payload = _fetch(live_server, "/ok", agent_type="social_media")

    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_POLICY_DENIED"
    assert "web_fetch" in payload["error"]["message"]
    assert "social_media" in payload["error"]["message"]
    assert payload["error"]["how_to_fix"], "a refusal must name the way out"


def test_an_unknown_agent_type_is_refused(live_server):
    payload = _fetch(live_server, "/ok", agent_type="no_such_agent")

    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_POLICY_DENIED"


def test_kill_switch_freeze_refuses_tool_calls(monkeypatch):
    monkeypatch.setenv("POLSIA_KILL_SWITCH", "freeze")
    payload = _payload(tool_registry.execute_tool("macro_snapshot", {}, agent_type="finance"))

    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_POLICY_DENIED"
    assert "kill switch" in payload["error"]["message"]


# ── macro_snapshot: the service's own honest answer, passed through ──────────────────────────

def _seed_macro_cache(monkeypatch) -> None:
    from app.services import economics_service

    fetched_at = "2026-10-14T22:00:00+00:00"
    monkeypatch.setattr(economics_service, "_CACHE", {
        "inflation": (datetime.now(UTC), {
            "year": 2024, "value": 2.9,
            "source": economics_service.WORLD_BANK_INFLATION_URL, "fetched_at": fetched_at}),
        "cpi": (datetime.now(UTC), {
            "date": "2026-08-01", "value": 320.1,
            "source": economics_service.FRED_CPI_CSV_URL, "fetched_at": fetched_at}),
        "fx": (datetime.now(UTC), {
            "usd_base": True, "date": "2026-10-14", "rates": {"EUR": 0.92}, "reason": None,
            "source": economics_service.FRANKFURTER_LATEST_URL, "fetched_at": fetched_at}),
    })


def test_macro_snapshot_passes_the_fresh_cache_through(monkeypatch):
    _seed_macro_cache(monkeypatch)
    payload = _payload(tool_registry.execute_tool("macro_snapshot", {}, agent_type="finance"))

    assert payload["ok"] is True
    snapshot = payload["result"]
    assert snapshot["status"] == "ok"
    assert snapshot["as_of"] == "2026-10-14T22:00:00+00:00"
    assert snapshot["cpi"]["value"] == 320.1
    assert all(snapshot[f"{name}_reason"] is None for name in ("inflation", "cpi", "fx"))


def test_macro_snapshot_without_a_cache_is_the_services_own_unavailable_answer(monkeypatch):
    from app.services import economics_service

    monkeypatch.setattr(economics_service, "_CACHE", {})
    payload = _payload(tool_registry.execute_tool("macro_snapshot", {}, agent_type="business_planning"))

    assert payload["ok"] is True, "an empty cache is an ANSWER (degraded), not a tool failure"
    snapshot = payload["result"]
    assert snapshot["status"] == "unavailable" and snapshot["as_of"] is None
    for name in ("inflation", "cpi", "fx"):
        assert snapshot[name] is None
        assert snapshot[f"{name}_reason"], "each unavailable section is named"


# ── pricing_catalog: the real 50-row snapshot + its staleness provenance ─────────────────────

def test_pricing_catalog_reads_the_real_snapshot_with_provenance():
    payload = _payload(tool_registry.execute_tool("pricing_catalog", {}, agent_type="business_planning"))

    assert payload["ok"] is True
    result = payload["result"]
    assert result["row_count"] == 50
    assert len(result["rows"]) == 50
    first = result["rows"][0]
    assert isinstance(first["service"], str) and first["service"]
    assert "price_low" in first
    provenance = result["provenance"]
    assert set(provenance) == {"catalog_as_of", "staleness_days", "stale"}
    assert provenance["catalog_as_of"] == CATALOG_AS_OF
    expected_age = (today_in() - date.fromisoformat(CATALOG_AS_OF)).days
    assert provenance["staleness_days"] == expected_age, "staleness computed, never stored"
    assert provenance["stale"] is False  # threshold is 180 days (pricing_service)


def test_pricing_catalog_is_denied_for_agents_outside_the_mapping():
    payload = _payload(tool_registry.execute_tool("pricing_catalog", {}, agent_type="social_media"))

    assert payload["ok"] is False and payload["error"]["code"] == "E_POLICY_DENIED"
