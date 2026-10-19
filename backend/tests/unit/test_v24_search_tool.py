"""V24/L2 — the `web_search` agent tool: DeepSeek `POST /v1/tools/search`, real key path.

RED-FIRST.  This file is written BEFORE `tool_registry` learns the tool and BEFORE
`policy_firewall` grants it; on the untouched tree every leg below fails by NAME ("unknown tool
'web_search': this registry ships exactly ...") — the RED run is `raw/RED-search.txt`.

WHAT IT PINS (the frozen V24 interface, verbatim from the wave plan):

  * name `web_search`, args `{query: str (1..128), count?: int (default 5, cap 10),
    ori_question?: str (1..128; the task title when the caller has one)}`;
  * request `{query, count, need_content: false}` to `https://api.deepseek.com/v1/tools/search`
    with the app's OWN key resolution (`deepseek_runtime._resolve_api_key()` — the same resolver
    the model runtime uses; there is no second key path, no env read of our own);
  * success payload `{ok:true, tool:"web_search", result:{query, results:[{title, url, snippet,
    summary, date, authority, rerank_score}], note}}` — the API's `content`/`site_name`/
    `site_icon` are NOT part of the frozen shape;
  * named refusals instead of fabricated results: `E_VALIDATION` (blank/too long query, bad
    count), `E_CONFIG` (no key), `E_AUTH` (401), `E_BALANCE` (402), `E_RATE_LIMIT` (429),
    `E_UNAVAILABLE` (408/5xx/transport/oversized/malformed body);
  * an empty `results` array is an HONEST empty list plus a note — never an error, never filled;
  * the HTTP call is bounded (timeout + read cap) and the returned text is capped per field.

The ONLY fake is the HTTP transport (`httpx.MockTransport`, installed through the registry's own
documented `_SEARCH_TRANSPORT` test seam) — a LABELLED test double.  The runner, validation,
mapping, refusal taxonomy and key resolution all run for real.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.agents import deepseek_runtime, tool_registry
from app.agents import policy_firewall as pf

#: Clearly fake, ASCII-only (httpx encodes header values as ASCII) — house pattern.
SAFE_TEST_KEY = "sk-v24-search-test-key"

SEARCH_URL = "https://api.deepseek.com/v1/tools/search"


# ── the one test double: the HTTP socket ─────────────────────────────────────────────────────

class _Wire:
    """Records every request the REAL runner sends and answers with a scripted response."""

    def __init__(self, responder) -> None:
        self.requests: list[dict] = []
        self._responder = responder

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append({
            "url": str(request.url),
            "body": json.loads(request.content or b"{}"),
            "headers": dict(request.headers),
        })
        return self._responder(len(self.requests), request)


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    """The runtime's resolver IS the key path — patch it exactly where the runtime reads it."""
    monkeypatch.setattr(deepseek_runtime, "_resolve_api_key", lambda: SAFE_TEST_KEY)


def _install(monkeypatch, responder) -> _Wire:
    wire = _Wire(responder)
    monkeypatch.setattr(tool_registry, "_SEARCH_TRANSPORT", httpx.MockTransport(wire.handler))
    return wire


def _results_payload(*rows) -> dict:
    return {"results": list(rows)}


def _row(**overrides) -> dict:
    row = {
        "rerank_score": 2.4,
        "title": "DeepSeek V4 model released",
        "url": "https://example.com/deepseek-v4",
        "snippet": "DeepSeek V4 is now available with tool calling.",
        "summary": "DeepSeek has released the V4 model; the release adds tool calling.",
        "content": "",
        "site_name": "Example News",
        "site_icon": "https://example.com/favicon.ico",
        "date": "2026-06-01T00:00:00+00:00",
        "authority": 3,
    }
    row.update(overrides)
    return row


def _search(arguments: dict, *, agent_type: str = "competitor_research", **kwargs) -> dict:
    return json.loads(tool_registry.execute_tool(
        "web_search", arguments, agent_type=agent_type, **kwargs))


def _ok(count: int = 1):
    return lambda n, request: httpx.Response(
        200, json=_results_payload(*[_row(title=f"result {i}") for i in range(count)]))


# ── the frozen interface ─────────────────────────────────────────────────────────────────────

def test_the_spec_is_the_frozen_function_spec():
    spec = next(s for s in tool_registry.TOOL_SPECS
                if s["function"]["name"] == "web_search")
    assert spec["type"] == "function"
    function = spec["function"]
    parameters = function["parameters"]
    assert parameters["type"] == "object"
    assert set(parameters["properties"]) == {"query", "count", "ori_question"}
    assert parameters["required"] == ["query"]
    assert parameters["additionalProperties"] is False
    description = function["description"].lower()
    assert "search" in description and "web_fetch" in function["description"]
    assert "data" in description, "results are untrusted external DATA (never instructions)"
    assert "web_search" in tool_registry.TOOL_GUIDANCE


def test_web_search_ships_exactly_for_the_market_intelligence_agent():
    """Evidence (competitor_research ROLE): "Use WebSearch to find candidates and WebFetch to
    read the pages that matter" — the one agent whose prompt demands live discovery.  Every
    other registry allowlist is unchanged (least privilege; see the lane RECEIPT)."""
    holders = {
        agent_type for agent_type, cap in pf.AGENT_CAPABILITIES.items()
        if "web_search" in cap.agent_tools
    }
    assert holders == {"competitor_research"}
    assert tool_registry.tools_for("competitor_research") == ("web_fetch", "web_search")


# ── success: the frozen result shape ────────────────────────────────────────────────────────

def test_a_successful_search_maps_the_frozen_result_shape(monkeypatch):
    wire = _install(monkeypatch, _ok(2))

    payload = _search({"query": "DeepSeek V4 release"})

    assert payload["ok"] is True and payload["tool"] == "web_search"
    result = payload["result"]
    assert set(result) == {"query", "results", "note"}
    assert result["query"] == "DeepSeek V4 release"
    assert len(result["results"]) == 2
    first = result["results"][0]
    assert set(first) == {"title", "url", "snippet", "summary", "date", "authority",
                          "rerank_score"}
    assert first["title"] == "result 0"
    assert first["url"] == "https://example.com/deepseek-v4"
    assert first["authority"] == 3 and first["rerank_score"] == 2.4
    assert first["date"] == "2026-06-01T00:00:00+00:00"

    # the request: the frozen body, the app's key, the documented endpoint
    assert len(wire.requests) == 1
    request = wire.requests[0]
    assert request["url"] == SEARCH_URL
    assert request["body"] == {"query": "DeepSeek V4 release", "count": 5,
                              "need_content": False}
    assert "ori_question" not in request["body"], "absent task title sends no field at all"
    assert request["headers"]["authorization"] == f"Bearer {SAFE_TEST_KEY}"


def test_count_defaults_to_five_and_is_clamped_to_ten(monkeypatch):
    wire = _install(monkeypatch, _ok(1))

    _search({"query": "one"})
    _search({"query": "two", "count": 3})
    _search({"query": "three", "count": 99})

    counts = [request["body"]["count"] for request in wire.requests]
    assert counts == [5, 3, 10], "default 5; below the cap honoured; above it clamped"

    refused = _search({"query": "four", "count": 0})
    assert refused["ok"] is False and refused["error"]["code"] == "E_VALIDATION"
    assert len(wire.requests) == 3, "a malformed count never reaches the network"


def test_ori_question_rides_from_the_callers_task_title(monkeypatch):
    """`ori_question` = the agent's task title when the executor has one (V24 frozen body)."""
    wire = _install(monkeypatch, _ok(1))

    _search({"query": "pricing page teardown"}, task_title="Launch the pricing page")

    assert wire.requests[0]["body"]["ori_question"] == "Launch the pricing page"


def test_an_explicit_ori_question_argument_wins_and_is_bounded(monkeypatch):
    wire = _install(monkeypatch, _ok(1))

    _search({"query": "q", "ori_question": "the exact question"})
    long_question = "x" * 500
    _search({"query": "q2", "ori_question": long_question}, task_title="task")

    assert wire.requests[0]["body"]["ori_question"] == "the exact question"
    assert len(wire.requests[1]["body"]["ori_question"]) == tool_registry.SEARCH_QUERY_MAX_CHARS


def test_empty_results_are_an_honest_empty_list_with_a_note(monkeypatch):
    _install(monkeypatch, lambda n, request: httpx.Response(200, json={"results": []}))

    payload = _search({"query": "nothing matches this"})

    assert payload["ok"] is True, "an empty answer is a real answer, not a tool failure"
    assert payload["result"]["results"] == []
    assert payload["result"]["note"], "the empty answer must say so"


def test_long_fields_are_clipped_so_one_result_cannot_blow_the_context(monkeypatch):
    big = _row(title="t" * 5000, snippet="s" * 5000, summary="m" * 40000)
    _install(monkeypatch, lambda n, request: httpx.Response(200, json=_results_payload(big)))

    payload = _search({"query": "big"})

    result = payload["result"]["results"][0]
    assert len(result["title"]) <= 300
    assert len(result["snippet"]) <= 500
    assert len(result["summary"]) <= 1500


def test_a_malformed_row_is_dropped_with_an_honest_note_never_fabricated(monkeypatch):
    _install(monkeypatch, lambda n, request: httpx.Response(200, json=_results_payload(
        _row(), {"title": "no url at all"}, "not a row")))

    payload = _search({"query": "mixed"})

    assert payload["ok"] is True
    assert len(payload["result"]["results"]) == 1
    assert "2" in payload["result"]["note"], "dropped rows are counted, never invented"


# ── every named refusal ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("status", "code"), [
    (400, "E_VALIDATION"),
    (401, "E_AUTH"),
    (402, "E_BALANCE"),
    (429, "E_RATE_LIMIT"),
    (408, "E_UNAVAILABLE"),
    (500, "E_UNAVAILABLE"),
    (503, "E_UNAVAILABLE"),
    (504, "E_UNAVAILABLE"),
])
def test_every_provider_status_maps_to_its_named_refusal(monkeypatch, status, code):
    _install(monkeypatch, lambda n, request: httpx.Response(
        status, json={"error": {"message": "provider says no", "type": "invalid_request_error"}}))

    payload = _search({"query": "refused"})

    assert payload["ok"] is False
    error = payload["error"]
    assert error["code"] == code, (status, error)
    assert error["how_to_fix"], "a refusal without a fix hint is a dead end"
    assert error["context"]["tool"] == "web_search"
    assert error["context"]["agent"] == "competitor_research"
    assert error["context"]["status"] == status
    assert "results" not in payload, "a refusal NEVER carries fabricated results"


def test_a_timeout_is_E_UNAVAILABLE(monkeypatch):
    def boom(n, request):
        raise httpx.ReadTimeout("timed out", request=request)

    _install(monkeypatch, boom)

    payload = _search({"query": "slow"})

    assert payload["ok"] is False and payload["error"]["code"] == "E_UNAVAILABLE"
    assert "timed out" in payload["error"]["message"].lower()


def test_a_transport_fault_is_E_UNAVAILABLE(monkeypatch):
    def boom(n, request):
        raise httpx.ConnectError("no route", request=request)

    _install(monkeypatch, boom)

    payload = _search({"query": "unreachable"})

    assert payload["ok"] is False and payload["error"]["code"] == "E_UNAVAILABLE"


def test_a_200_with_a_malformed_body_is_a_named_refusal(monkeypatch):
    _install(monkeypatch, lambda n, request: httpx.Response(200, json={"results": "nope"}))

    payload = _search({"query": "weird"})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_UNAVAILABLE"
    assert "results" not in payload


def test_no_key_is_a_named_E_CONFIG_and_never_reaches_the_network(monkeypatch):
    monkeypatch.setattr(deepseek_runtime, "_resolve_api_key", lambda: None)
    wire = _install(monkeypatch, _ok(1))

    payload = _search({"query": "no key"})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_CONFIG"
    assert "key" in payload["error"]["message"].lower()
    assert payload["error"]["how_to_fix"]
    assert wire.requests == [], "no key means no HTTP call at all"


@pytest.mark.parametrize("arguments", [
    {}, {"query": ""}, {"query": "   "}, {"query": 42}, {"query": None},
])
def test_a_missing_or_blank_query_is_E_VALIDATION(monkeypatch, arguments):
    wire = _install(monkeypatch, _ok(1))

    payload = _search(arguments)

    assert payload["ok"] is False and payload["error"]["code"] == "E_VALIDATION"
    assert wire.requests == []


def test_a_query_over_the_api_limit_is_refused_not_truncated(monkeypatch):
    wire = _install(monkeypatch, _ok(1))

    payload = _search({"query": "q" * (tool_registry.SEARCH_QUERY_MAX_CHARS + 1)})

    assert payload["ok"] is False and payload["error"]["code"] == "E_VALIDATION"
    assert str(tool_registry.SEARCH_QUERY_MAX_CHARS) in payload["error"]["message"]
    assert wire.requests == []


def test_a_lone_surrogate_query_is_refused_and_never_sent(monkeypatch):
    """A JSON `"\\ud800"` decodes to a lone surrogate Python cannot encode — a NAMED refusal."""
    wire = _install(monkeypatch, _ok(1))

    payload = _search({"query": "bad \ud800 query"})

    assert payload["ok"] is False and payload["error"]["code"] == "E_VALIDATION"
    assert wire.requests == []


def test_a_provider_error_that_echoes_the_key_is_scrubbed(monkeypatch):
    _install(monkeypatch, lambda n, request: httpx.Response(
        401, json={"error": {"message": f"bad key {SAFE_TEST_KEY}"}}))

    payload = _search({"query": "q"})

    assert payload["ok"] is False and payload["error"]["code"] == "E_AUTH"
    assert SAFE_TEST_KEY not in json.dumps(payload), "the key must never ride a refusal"


def test_a_non_integer_count_is_E_VALIDATION(monkeypatch):
    wire = _install(monkeypatch, _ok(1))

    payload = _search({"query": "q", "count": "many"})

    assert payload["ok"] is False and payload["error"]["code"] == "E_VALIDATION"
    assert wire.requests == []


# ── the firewall + the key path ────────────────────────────────────────────────────────────

def test_an_agent_outside_the_allowlist_is_refused_by_name(monkeypatch):
    wire = _install(monkeypatch, _ok(1))

    payload = _search({"query": "q"}, agent_type="social_media")

    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_POLICY_DENIED"
    assert "web_search" in payload["error"]["message"]
    assert "social_media" in payload["error"]["message"]
    assert wire.requests == []


def test_the_key_path_is_the_runtimes_resolver_and_nothing_else():
    """No second key path: the runner calls `deepseek_runtime._resolve_api_key` and never reads
    an env var of its own (pre-V24 rule: the tools resolve the key the SAME way the runtime does)."""
    import inspect

    source = inspect.getsource(tool_registry._deepseek_api_key)
    assert "_resolve_api_key" in source
    assert "getenv" not in source and "environ" not in source


def test_a_present_key_is_never_echoed_in_the_payload(monkeypatch):
    _install(monkeypatch, _ok(1))

    payload = _search({"query": "q"})

    assert SAFE_TEST_KEY not in json.dumps(payload)


def test_an_unknown_tool_still_names_the_full_known_set():
    payload = json.loads(tool_registry.execute_tool(
        "web_surfer", {}, agent_type="competitor_research"))

    assert payload["ok"] is False and payload["error"]["code"] == "E_VALIDATION"
    message = payload["error"]["message"]
    # V25/L2: the GitHub quartet joined the registry's name set; the refusal still names the
    # LIVE set (this is a registry-vocabulary pin, not a search pin).
    for name in ("web_fetch", "web_search", "macro_snapshot", "pricing_catalog", "phone_read",
                 "code_write", "code_read", "code_list",
                 "github_repo", "github_branch", "github_commit", "github_pr"):
        assert name in message
