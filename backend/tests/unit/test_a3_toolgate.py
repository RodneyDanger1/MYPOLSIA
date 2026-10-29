"""Wave A3 / seat S2 — the TOOL BOUNDARY: one gate for registry tools and for posts.

The findings this file pins (each with the defect it replaces):

  * A2A8-01 — the registry built `ActionRequest(channel="", payload={}, taint=None)`, so the
    firewall's payload/fingerprint, per-channel cap, taint and approval legs were dead for
    every model tool call.  The gate now receives the REAL channel, the EXACT arguments, the
    run's published taint, the tool name and the run's measured counts.
  * A2A9-01 — the run's taint never reached the gate: a tainted run could commit.  It is read
    from the `RUN_TAINT` contextvar the assembler publishes (the same store the sibling
    `request_side_effect` path reads) and escalates the call to REQUIRE_APPROVAL.
  * A2A9-02 — a tool that needs approval could never be approved (`execute_tool` flattened
    every non-ALLOW into one refusal).  REQUIRE_APPROVAL now returns the action fingerprint
    and a `require_token` marker, so the operator's token can be attached and the SAME call
    then runs.
  * A7-04 — the tool path gated a GitHub mutation `external_reversible` with no channel while
    the code agent's own `open_pr` path gated the same action `external_irreversible` on the
    `github` channel: two classes, two counters, two answers.  There is now ONE class table
    and ONE channel table, and both paths answer REQUIRE_APPROVAL for the same real mutation.
  * A2A4-02 — `fingerprint()` hashed `str(sorted(payload.items()))`, so two 280-char posts
    differing at char 240 shared a token (an approved token authorised an unseen body).  It
    now hashes `action_class|channel|<canonical json of the payload>`, which is injective.
  * F-28 — `web_fetch` let httpx resolve a second time after the floor had vetted a different
    answer.  It now dials through S1's `trust_service.pinned_http_get` (CONTRACTS §4.15).
  * F-31 — no ceiling where tool text reaches the model: a 200 000-char runner payload came
    back as a 200 063-char tool message.  `TOOL_RESULT_CEILING` refuses BY NAME instead.
  * F-32 — the two payloads made of third-party text carried no `untrusted` flag.
  * F-33 — no per-run meter on metered provider calls: 25 of 25 were permitted.

The ONLY doubles here are the tree's own labelled seams: `tool_registry._RUNNERS` entries (the
gate is what these tests judge, so the provider is not called), `_SEARCH_TRANSPORT`,
`trust_service._doh` / `_CLIENT` (S1's socket seam), and `deepseek_runtime._resolve_api_key`.
No product code is monkeypatched into a different answer.
"""
from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from app.agents import deepseek_runtime
from app.agents import policy_firewall as pf
from app.agents import side_effects as se
from app.agents import tool_registry as registry
from app.agents.policy_firewall import ActionClass, Decision, RunApproval, RunTaint
from app.config import settings
from app.core.errors import MeridianError
from app.services import trust_service

SECRET = b"s2-toolgate-secret-not-a-real-key"
REPO = "acme/widgets"

#: A REAL commit: `dry_run: false` is what the schema tells the model to send for a mutation.
COMMIT_ARGS = {
    "repo": REPO, "branch": "main", "message": "tighten the gate",
    "files": [{"path": "app.py", "content": "print('hi')\n"}], "dry_run": False,
}
COMMIT_RESULT = {
    "ok": True, "tool": "github_commit",
    "result": {"repo": REPO, "branch": "main", "commit_sha": "sha-stub", "url": "https://x.test/c",
               "files": [], "dry_run": False},
}


# ── isolation: no test may inherit (or leave) a run-scoped verdict ──────────────────────────

@pytest.fixture(autouse=True)
def _isolate_run_scope():
    pf.reset_run_taint()
    pf.reset_run_approval()
    pf.reset_run_channel_sends()
    trust_service._CACHE.clear()
    yield
    pf.reset_run_taint()
    pf.reset_run_approval()
    pf.reset_run_channel_sends()
    trust_service._CACHE.clear()


@pytest.fixture()
def counts():
    """The run's measured per-channel counts — what the worker publishes for a run."""
    pf.set_run_channel_sends({"github": {"today": 0, "hour": 0}})


@pytest.fixture()
def run_scope():
    """Open and close a real per-run tool budget (the runtime's `try/finally` shape)."""
    token = registry.begin_run_tool_budget()
    try:
        yield token
    finally:
        registry.end_run_tool_budget(token)


@pytest.fixture(autouse=True)
def _sandbox_off(monkeypatch):
    """The gate is only reachable for an external class with the sandbox off (the default
    sandbox returns a labelled draft before any of these legs run — pinned elsewhere)."""
    monkeypatch.setattr(settings, "sandbox_mode", False)


# ── the runner double: the gate is under test, not GitHub ───────────────────────────────────

class _Runner:
    def __init__(self, result: dict | None = None) -> None:
        self.calls: list[dict] = []
        self._result = dict(result if result is not None else COMMIT_RESULT)

    def __call__(self, *, agent_type: str, arguments: dict) -> dict:
        self.calls.append({"agent_type": agent_type, "arguments": arguments})
        return dict(self._result)


def _install(monkeypatch, name: str = "github_commit", result: dict | None = None) -> _Runner:
    runner = _Runner(result)
    monkeypatch.setitem(registry._RUNNERS, name, runner)
    return runner


def _call(name: str, arguments: dict | None = None, *, agent_type: str = "code_generation") -> dict:
    return json.loads(registry.execute_tool(name, arguments or {}, agent_type=agent_type))


def _record_decide(monkeypatch) -> list[tuple]:
    """A spy that RECORDS the request the gate was handed and then decides for real."""
    seen: list[tuple] = []
    real = registry.decide

    def spy(req, **kwargs):
        seen.append((req, kwargs))
        return real(req, **kwargs)

    monkeypatch.setattr(registry, "decide", spy)
    return seen


# ── A2A8-01: the request the gate is handed ─────────────────────────────────────────────────

def test_the_tool_gate_receives_the_real_channel_payload_taint_and_counts(monkeypatch, counts):
    """RED before the fix: channel `""`, payload `{}`, taint `None`, no counts."""
    seen = _record_decide(monkeypatch)
    taint = RunTaint(tainted=True, flagged_blocks=[0], sources=["layer:extra_context"])
    pf.set_run_taint(taint)
    pf.set_run_approval(RunApproval(secret=SECRET, token="opaque-not-a-token-yet"))
    runner = _install(monkeypatch)

    _call("github_commit", COMMIT_ARGS)

    assert len(seen) == 1, "one tool call makes exactly one policy decision"
    req, kwargs = seen[0]
    assert req.agent_type == "code_generation"
    assert req.action_class is ActionClass.EXTERNAL_IRREVERSIBLE
    assert req.channel == "github", "a GitHub mutation acts on the github channel"
    assert req.tool == "github_commit"
    assert req.payload == COMMIT_ARGS, "the fingerprint must name the EXACT arguments"
    assert req.taint is taint, "the run's published taint, not None"
    assert (req.sends_today, req.sends_this_hour) == (0, 0), "the run's measured counts"
    assert kwargs["approval_secret"] == SECRET, "the run's approval secret reaches decide()"
    assert runner.calls == [], "a tainted, unapproved commit must not reach the runner"


def test_a_read_tool_carries_its_arguments_and_no_channel(monkeypatch):
    """A read is not a channel send: it keeps channel `""` and is still allowed, taint or not.

    OLD (pre-wave-6): this called `pricing_catalog` with `{"limit": 3}` — an argument that tool's
    schema does not declare — and asserted `ok is True`, i.e. it depended on the registry silently
    dropping the key.  NEW: `pricing_catalog` takes NO arguments, so the call is `{}` and the
    payload assertion is `{}`.  The "arguments ride the decide payload" half is not lost: it is
    proven on REAL arguments by `test_the_runs_own_performed_sends_are_added_to_the_decision`
    below (a `github_commit` payload) and, in both directions, by
    `tests/unit/test_w6_tool_behaviour_census.py` (F-W6-01: a schema declaring
    `additionalProperties: false` must be honoured, so this test may not pin the old tolerance).
    """
    seen = _record_decide(monkeypatch)
    pf.set_run_taint(RunTaint(tainted=True, sources=["probe"]))

    out = _call("pricing_catalog", {}, agent_type="orchestrator")

    req, _kwargs = seen[0]
    assert req.channel == ""
    assert req.payload == {}
    assert req.action_class is ActionClass.READ
    assert out["ok"] is True, "a read is never escalated by the taint leg"


def test_the_channel_table_names_real_tools_and_channels_the_agent_may_touch():
    """A channel the capability does not grant would DENY every call; a stale name is drift."""
    names = {spec["function"]["name"] for spec in registry.TOOL_SPECS}
    assert set(registry._TOOL_CHANNEL) <= names
    granted = pf.capability_for("code_generation")
    assert granted is not None
    assert set(registry._TOOL_CHANNEL.values()) <= set(granted.channels)


def test_the_runs_own_performed_sends_are_added_to_the_decision(monkeypatch, counts, run_scope):
    """`performed` is the run's own meter: the second real commit sees the first one counted."""
    seen = _record_decide(monkeypatch)
    fp = pf.fingerprint(ActionClass.EXTERNAL_IRREVERSIBLE.value, "github", COMMIT_ARGS)
    token = pf.issue_approval(SECRET, action_fingerprint=fp)
    pf.set_run_approval(RunApproval(secret=SECRET, token=token.token))
    runner = _install(monkeypatch)

    _call("github_commit", COMMIT_ARGS)
    _call("github_commit", COMMIT_ARGS)

    assert [req.sends_today for req, _kwargs in seen] == [0, 1]
    assert [req.sends_this_hour for req, _kwargs in seen] == [0, 1]
    assert len(runner.calls) == 2


def test_a_dry_run_plan_does_not_consume_a_send_slot(monkeypatch, counts, run_scope):
    """A plan performs nothing, so it must not shift the run's `github` count either."""
    seen = _record_decide(monkeypatch)
    plan = {key: value for key, value in COMMIT_ARGS.items() if key != "dry_run"}
    pf.set_run_approval(RunApproval(secret=SECRET, token=pf.issue_approval(
        SECRET, action_fingerprint=pf.fingerprint(ActionClass.EXTERNAL_IRREVERSIBLE.value,
                                                  "github", COMMIT_ARGS)).token))
    _install(monkeypatch)

    _call("github_commit", plan)
    _call("github_commit", COMMIT_ARGS)

    assert seen[0][0].action_class is ActionClass.READ, "a plan is not an external action"
    assert seen[1][0].sends_today == 0, "the plan sent nothing, so it counted nothing"


# ── A2A9-01: the run's taint reaches the gate ───────────────────────────────────────────────

def test_a_tainted_run_cannot_commit_through_the_tool_gate(monkeypatch, counts):
    """RED before the fix: the runner ran (`ok: true`) for a TAINTED run."""
    pf.set_run_taint(RunTaint(tainted=True, flagged_blocks=[0], sources=["layer:extra_context"]))
    pf.set_run_approval(RunApproval(secret=SECRET))
    runner = _install(monkeypatch)

    out = _call("github_commit", COMMIT_ARGS)

    assert out["ok"] is False
    assert out["error"]["code"] == "E_POLICY_DENIED"
    assert out["error"]["context"]["require_token"] is True
    assert "context tainted by untrusted content" in out["error"]["message"]
    assert runner.calls == [], "an unapproved commit from a tainted run must not be executed"


def test_a_clean_run_with_an_attached_token_reaches_the_runner(monkeypatch, counts):
    """The control leg: the escalation is the TAINT, not a blanket denial."""
    pf.set_run_taint(RunTaint(tainted=False))
    fp = pf.fingerprint(ActionClass.EXTERNAL_IRREVERSIBLE.value, "github", COMMIT_ARGS)
    token = pf.issue_approval(SECRET, action_fingerprint=fp)
    pf.set_run_approval(RunApproval(secret=SECRET, token=token.token))
    runner = _install(monkeypatch)

    out = _call("github_commit", COMMIT_ARGS)

    assert out["ok"] is True, out
    assert len(runner.calls) == 1
    assert runner.calls[0]["arguments"] == COMMIT_ARGS


# ── A2A9-02 + A7-04: the approval path is real end to end ───────────────────────────────────

def test_an_unapproved_irreversible_tool_call_is_refused_with_a_typed_code(monkeypatch, counts):
    """RED before the fix: `E_POLICY_DENIED` claimed no approval channel existed at all."""
    pf.set_run_approval(RunApproval(secret=SECRET))
    runner = _install(monkeypatch)

    out = _call("github_commit", COMMIT_ARGS)

    error = out["error"]
    assert out["ok"] is False and error["code"] == "E_POLICY_DENIED"
    context = error["context"]
    assert context["require_token"] is True, "the owner CAN act on this refusal"
    assert context["action_fingerprint"] == pf.fingerprint(
        ActionClass.EXTERNAL_IRREVERSIBLE.value, "github", COMMIT_ARGS)
    assert context["channel"] == "github" and context["action_class"] == "external_irreversible"
    assert "needs the owner's approval" in error["message"]
    assert "/api/v1/approvals" in error["how_to_fix"], "the refusal names the way to mint a token"
    assert runner.calls == []


def test_github_commit_requires_approval_then_runs_when_the_token_is_attached(monkeypatch, counts):
    """The full loop: require -> mint for the reported fingerprint -> attach -> ALLOW -> run."""
    pf.set_run_approval(RunApproval(secret=SECRET))
    runner = _install(monkeypatch)

    first = _call("github_commit", COMMIT_ARGS)
    fp = first["error"]["context"]["action_fingerprint"]
    assert first["error"]["context"]["require_token"] is True and runner.calls == []

    # The fingerprint is the documented arithmetic, computed independently of `fingerprint()`.
    canonical = json.dumps(COMMIT_ARGS, sort_keys=True, separators=(",", ":"))
    want = hashlib.sha256(
        f"external_irreversible|github|{canonical}".encode()).hexdigest()[:24]
    assert fp == want, "action_class|channel|canonical(payload), 24 hex chars"

    token = pf.issue_approval(SECRET, action_fingerprint=fp)
    pf.set_run_approval(RunApproval(secret=SECRET, token=token.token))

    second = _call("github_commit", COMMIT_ARGS)
    assert second["ok"] is True, second
    assert len(runner.calls) == 1, "the approved call reaches the runner exactly once"


def test_a_missing_approval_channel_is_named_not_blamed_on_the_owner(monkeypatch, counts):
    """No `RunApproval` at all: the refusal says so (a DENY, not a token request)."""
    runner = _install(monkeypatch)

    out = _call("github_commit", COMMIT_ARGS)

    assert out["ok"] is False and out["error"]["code"] == "E_POLICY_DENIED"
    assert "approval required but no approval channel configured" in out["error"]["message"]
    assert "require_token" not in out["error"]["context"], "a hard DENY is not a token request"
    assert runner.calls == []


def test_the_tool_path_and_the_post_path_agree_on_require_approval(monkeypatch, counts):
    """A7-04: ONE class, ONE channel, ONE answer for the same real GitHub mutation."""
    pf.set_run_approval(RunApproval(secret=SECRET))
    post = se.request_side_effect(
        "code_generation", ActionClass.EXTERNAL_IRREVERSIBLE, "github",
        action="open_pr",
        payload={"action": "open_pr", "repo": REPO, "branch": "feat/x", "pr_title": "t"},
        sends_today=0, sends_this_hour=0,
    )
    runner = _install(monkeypatch, "github_pr",
                      result={"ok": True, "tool": "github_pr", "result": {"repo": REPO}})

    tool = _call("github_pr", {"repo": REPO, "title": "t", "head": "feat/x",
                               "base": "main", "dry_run": False})

    assert post.decision.decision is Decision.REQUIRE_APPROVAL
    assert post.requires_approval is True
    assert tool["error"]["context"]["require_token"] is True
    assert tool["error"]["context"]["channel"] == "github"
    assert tool["error"]["context"]["action_class"] == "external_irreversible"
    assert runner.calls == [], "neither path may execute the mutation unapproved"


def test_a_github_dry_run_plan_is_not_gated_as_a_mutation(monkeypatch):
    """A plan sends nothing, so it is not an external action: no cap, no token, no draft."""
    runner = _install(monkeypatch, result={"ok": True, "tool": "github_commit",
                                           "result": {"dry_run": True}})
    plan = {key: value for key, value in COMMIT_ARGS.items() if key != "dry_run"}

    out = _call("github_commit", plan)

    assert out["ok"] is True, out
    assert len(runner.calls) == 1, "the plan is produced by the runner (dry_run defaults true)"


# ── A2A4-02: the token binds the ACTION, not a truncated draft ──────────────────────────────

def _post_payload(body: str) -> dict:
    return {"action": "post", "content": body, "ref": "social-7"}


def test_two_posts_differing_at_char_240_do_not_share_a_token():
    """RED before the fix: equal fingerprints and an ALLOW for the unseen body."""
    body_a = "A" * 240 + "alpha" + "A" * 35
    body_b = "A" * 240 + "bravo" + "A" * 35
    assert len(body_a) == len(body_b) == 280 and body_a[:240] == body_b[:240]

    fp_a = pf.fingerprint(ActionClass.EXTERNAL_REVERSIBLE.value, "twitter", _post_payload(body_a))
    fp_b = pf.fingerprint(ActionClass.EXTERNAL_REVERSIBLE.value, "twitter", _post_payload(body_b))
    assert fp_a != fp_b, "two different 280-char posts must not share an approval"

    token = pf.issue_approval(SECRET, action_fingerprint=fp_a).token
    other = pf.decide(
        pf.ActionRequest("social_media", ActionClass.EXTERNAL_REVERSIBLE, "twitter",
                         payload=_post_payload(body_b), value_usd=30.0,
                         sends_today=0, sends_this_hour=0, approval_token=token),
        approval_secret=SECRET, sandbox_mode=False,
    )
    assert other.decision is Decision.REQUIRE_APPROVAL, "the approved post was a DIFFERENT post"

    same = pf.decide(
        pf.ActionRequest("social_media", ActionClass.EXTERNAL_REVERSIBLE, "twitter",
                         payload=_post_payload(body_a), value_usd=30.0,
                         sends_today=0, sends_this_hour=0, approval_token=token),
        approval_secret=SECRET, sandbox_mode=False,
    )
    assert same.decision is Decision.ALLOW, "an UNCHANGED action still verifies"
    assert same.audit.get("approved") is True


def test_the_fingerprint_is_canonical_and_injective():
    one = {"action": "post", "meta": {"b": 2, "a": 1}, "ref": "r"}
    reordered = {"ref": "r", "meta": {"a": 1, "b": 2}, "action": "post"}
    assert pf.fingerprint("read", "ch", one) == pf.fingerprint("read", "ch", reordered), \
        "nested key order must not change the fingerprint"

    # Two DIFFERENT payloads whose old `str(sorted(items))` rendition was ambiguous.
    left = {"action": "a", "content": "x', ('b', 'y"}
    right = {"action": "a", "content": "x", "b": "y"}
    assert left != right
    assert pf.fingerprint("read", "ch", left) != pf.fingerprint("read", "ch", right)

    assert pf.canonical_payload(one) == '{"action":"post","meta":{"a":1,"b":2},"ref":"r"}'


# ── F-31: the ceiling at the one seam every tool result crosses ─────────────────────────────

def test_a_tool_result_over_the_ceiling_is_refused_by_name(monkeypatch):
    """RED before the fix: a 200 000-char runner payload came back as a 200 063-char message."""
    _install(monkeypatch, "pricing_catalog",
             result={"ok": True, "tool": "pricing_catalog", "result": {"rows": "x" * 200_000}})

    out = _call("pricing_catalog", {}, agent_type="orchestrator")

    assert out["ok"] is False and out["error"]["code"] == "E_BUDGET_EXCEEDED"
    assert out["error"]["context"]["chars"] > registry.TOOL_RESULT_CEILING
    assert out["error"]["context"]["ceiling"] == registry.TOOL_RESULT_CEILING
    assert "NOT truncated" in out["error"]["message"], "a refusal, never a silent truncation"


def test_a_tool_result_under_the_ceiling_passes_through_unchanged(monkeypatch):
    body = "y" * (registry.TOOL_RESULT_CEILING // 2)
    _install(monkeypatch, "pricing_catalog",
             result={"ok": True, "tool": "pricing_catalog", "result": {"rows": body}})

    text = registry.execute_tool("pricing_catalog", {}, agent_type="orchestrator")

    assert len(text) > len(body)
    assert json.loads(text)["result"]["rows"] == body


# ── F-32: third-party text is labelled untrusted ────────────────────────────────────────────

def _pin_web(monkeypatch, *, body: bytes = b"<html><body>hello from the web</body></html>",
             status: int = 200, headers: dict | None = None) -> list[httpx.Request]:
    """Stub S1's two egress seams: the DoH answer and the socket.  No name is resolved."""
    monkeypatch.setattr(trust_service, "_doh",
                        lambda host, rrtype: {"status": 0,
                                              "answers": ["93.184.216.34"] if rrtype == "A" else []})
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, headers=headers or {"content-type": "text/html"},
                              content=body)

    def client(**kwargs) -> httpx.Client:
        kwargs.pop("transport", None)
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(trust_service, "_CLIENT", client)
    return seen


def _search_wire(monkeypatch, *, results: list[dict] | None = None) -> list[dict]:
    monkeypatch.setattr(deepseek_runtime, "_resolve_api_key", lambda: "sk-a3-toolgate-test-key")
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"url": str(request.url), "body": json.loads(request.content or b"{}")})
        return httpx.Response(200, json={"results": results if results is not None else [
            {"rerank_score": 1.0, "title": "t", "url": "https://example.com/a",
             "snippet": "s", "summary": "m", "site_name": "s", "date": "2026-01-01",
             "authority": 3},
        ]})

    monkeypatch.setattr(registry, "_SEARCH_TRANSPORT", httpx.MockTransport(handler))
    return seen


def test_web_payloads_are_flagged_untrusted(monkeypatch):
    """RED before the fix: `untrusted` was absent from both third-party payloads."""
    requests = _pin_web(monkeypatch)
    _search_wire(monkeypatch)

    fetched = _call("web_fetch", {"url": "https://example.com/page"}, agent_type="competitor_research")
    searched = _call("web_search", {"query": "x"}, agent_type="competitor_research")

    assert fetched["ok"] is True and fetched["result"]["untrusted"] is True
    assert searched["ok"] is True and searched["result"]["untrusted"] is True
    assert requests, "the fetch went out through the pinned client"


# ── F-28: web_fetch dials the address the resolver vetted (§4.15) ───────────────────────────

def test_web_fetch_dials_the_vetted_address_and_keeps_the_host_name(monkeypatch):
    """RED before the fix: httpx resolved the NAME again (a second answer, never checked)."""
    requests = _pin_web(monkeypatch)

    out = _call("web_fetch", {"url": "https://example.com/x?q=1"}, agent_type="competitor_research")

    assert out["ok"] is True, out
    assert len(requests) == 1
    assert requests[0].url.host == "93.184.216.34", "the socket dialled the VETTED address"
    assert requests[0].headers["host"] == "example.com", "the origin still sees its own name"
    assert str(requests[0].url).endswith("/x?q=1")
    assert out["result"]["url"] == "https://example.com/x?q=1", "the payload reports the logical URL"


def test_web_fetch_reports_a_refused_egress_as_a_named_refusal(monkeypatch):
    """No fallback connect path: the resolver's refusal IS the tool's answer."""
    def refuse(host: str, port: int) -> list[str]:
        raise MeridianError(
            trust_service.E_EGRESS_BLOCKED,
            f"egress: refusing {host!r}:{port} — not a public address",
            how_to_fix="use a public host", context={"host": host, "port": port})

    monkeypatch.setattr(trust_service, "resolve_egress", refuse)

    out = _call("web_fetch", {"url": "https://internal.test/secret"},
                agent_type="competitor_research")

    assert out["ok"] is False
    assert out["error"]["code"] == "E_POLICY_DENIED", "the tool's published egress code"
    assert out["error"]["context"]["egress_code"] == trust_service.E_EGRESS_BLOCKED
    assert "internal.test" in out["error"]["message"]


# ── F-33: a per-run meter on the tools that cost money ─────────────────────────────────────

def test_metered_tools_are_capped_per_run(monkeypatch, run_scope):
    """RED before the fix: 25 of 25 metered calls were permitted."""
    runner = _install(monkeypatch, "research_fetch",
                      result={"ok": True, "tool": "research_fetch", "result": {}})

    outs = [_call("research_fetch", {"url": "https://example.com"},
                  agent_type="competitor_research")
            for _ in range(registry.METERED_CALLS_PER_RUN + 1)]

    assert all(out["ok"] is True for out in outs[:-1]), "the cap is the last call, not earlier ones"
    assert outs[-1]["ok"] is False and outs[-1]["error"]["code"] == "E_BUDGET_EXCEEDED"
    assert outs[-1]["error"]["context"]["cap"] == registry.METERED_CALLS_PER_RUN
    assert outs[-1]["error"]["context"]["used"] == registry.METERED_CALLS_PER_RUN
    assert len(runner.calls) == registry.METERED_CALLS_PER_RUN, "the refused call is not sent"


def test_the_meter_is_per_run_and_clears_with_the_budget(monkeypatch):
    _install(monkeypatch, "research_fetch", result={"ok": True, "tool": "research_fetch",
                                                    "result": {}})
    for _ in range(2):                                  # two runs, each with a full budget
        token = registry.begin_run_tool_budget()
        try:
            outs = [_call("research_fetch", {"url": "https://example.com"},
                          agent_type="competitor_research")
                    for _ in range(registry.METERED_CALLS_PER_RUN)]
        finally:
            registry.end_run_tool_budget(token)
        assert all(out["ok"] is True for out in outs)

    # No run scope at all: nothing is metered (the CLI path and every pre-F-33 caller).
    out = _call("research_fetch", {"url": "https://example.com"}, agent_type="competitor_research")
    assert out["ok"] is True


def test_an_unmetered_tool_never_consumes_the_meter(monkeypatch, run_scope, tmp_path):
    monkeypatch.setattr(settings, "code_workspace_dir", str(tmp_path))
    _install(monkeypatch, "research_fetch",
             result={"ok": True, "tool": "research_fetch", "result": {}})
    for _ in range(3):
        out = _call("code_read", {"path": "absent.txt"})
        assert out["error"]["code"] == "E_NOT_FOUND", "a read still runs (and is not metered)"
    out = _call("research_fetch", {"url": "https://example.com"}, agent_type="competitor_research")
    assert out["ok"] is True
